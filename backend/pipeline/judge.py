"""Judge (owner: A).

Two stages plus one function other tracks call:

- ``run_frames``  stage ``judge_frames``: reads candidates.json + frames/ (+ summary.json for
  the app's one-liner), writes
  frame_scores.json and one ``frame_pick`` decision per segment. Each judged frame gets the
  fixed question per criterion (generic one for an unplanned criterion), and
  total = 80% mean judge score + 20% sharpness. Cheap mode judges only the 2 sharpest frames
  of each segment. Re-running replaces this stage's earlier decisions.
- ``run_rubric``  stage ``rubric``: reads timeline.json, script.json, brief; writes the
  rubric (and an optional ``recut``) into decisions.json.
- ``pick_best``   called by the assets stage to choose among N generated drafts (images judged; audio/video stubbed).

Run alone: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict, Union

from PIL import Image, ImageFilter, ImageStat
from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.contracts import Candidates, FrameScore, FrameScores, JudgeAnswer, RunConfig, Summary
from backend.decisions import append_decision, drop_stage

# Judge questions: fixed per criterion, asked every run (CONTEXT.md "Judge question").
QUESTIONS = {
    "usefulness": "Does the frame show the app doing something useful for the user?",
    "craft": "Is the UI clean and legible (no dev tools, no clutter)?",
    "aura": "Would this frame make a judge want to keep watching?",
}
JUDGE_WEIGHT = 0.8  # the rest is sharpness
CHEAP_FRAMES = 2
WORKERS = 6  # parallel Gemini calls, one per segment


def questions_for(criteria: list[str]) -> list[str]:
    """One fixed question per criterion, in brief order; unplanned criteria get a generic one."""
    names = [c.strip().lower() for c in criteria]
    qs = [QUESTIONS.get(c, f"Does the frame show the app at its best for '{c}'?") for c in names]
    return list(dict.fromkeys(qs))


def combine(judge_scores: list[float], heuristic: float) -> float:
    mean = sum(judge_scores) / len(judge_scores)
    return round(JUDGE_WEIGHT * mean + (1 - JUDGE_WEIGHT) * heuristic, 3)


def sharpness(paths: list[Path]) -> list[float]:
    """Edge variance per image, scaled so the sharpest of ``paths`` is 1.0."""
    # ponytail: relative to this run, so scores don't compare across runs; use a fixed scale if they must.
    raw = []
    for p in paths:
        with Image.open(p) as img:
            raw.append(ImageStat.Stat(img.convert("L").filter(ImageFilter.FIND_EDGES)).var[0])
    top = max(raw, default=0.0) or 1.0
    return [round(r / top, 3) for r in raw]


def frames_to_judge(frames: list[str], sharp: dict[str, float], mode: str) -> list[str]:
    """Full mode judges every frame; cheap mode the sharpest few, kept in time order."""
    if mode != "cheap":
        return frames
    keep = set(sorted(frames, key=lambda f: -sharp[f])[:CHEAP_FRAMES])
    return [f for f in frames if f in keep]


class _Answer(BaseModel):
    score: float = Field(ge=0, le=1, description="0 = no, 1 = clearly yes.")
    reason: str = Field(description="One short line on why.")


class _Frame(BaseModel):
    note: str = Field(description="One short line on what the frame shows.")
    answers: list[_Answer] = Field(description="One per question, same order.")


class _Verdict(BaseModel):
    frames: list[_Frame] = Field(description="One per frame shown, same order.")


def judge_segment(run_dir: Path, frames: list[str], questions: list[str], app: str) -> list[_Frame]:
    text = gemini.prompt(
        "judge_frames",
        app=app,
        n=str(len(frames)),
        frames=", ".join(frames),
        questions="\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)),
    )
    for _ in range(gemini.RETRIES + 1):  # a schema-valid answer can still miscount frames/questions
        verdict = gemini.generate(text, _Verdict, images=[run_dir / f for f in frames])
        if len(verdict.frames) == len(frames) and all(len(f.answers) == len(questions) for f in verdict.frames):
            return verdict.frames
    raise ValueError(f"judge_frames: Gemini answered the wrong number of frames/questions for {frames}")


class PickResult(TypedDict):
    """What ``pick_best`` returns. Enough for the caller to build a ``take_pick`` Decision."""

    index: int  # index into ``candidates`` of the winner
    scores: list[float]  # one 0-1 score per candidate, same order as ``candidates``
    answers: list[dict]  # JudgeAnswer dicts for the winner, one per question, same order
    judge_score: float  # 0-1, == scores[index]
    reason: str  # one line, shown in the UI


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def pick_best(candidates: list[Union[str, Path]], questions: list[str], context: str = "") -> PickResult:
    """Pick the best of N candidate files against ``questions``; ``context`` says what they are for.

    Images: Gemini sees every draft and answers each question per draft; the winner has the
    highest mean score. Audio and video are still a stub (index 0, neutral scores).
    """
    if not candidates:
        raise ValueError("pick_best needs at least one candidate")
    paths = [Path(c) for c in candidates]
    if not all(p.suffix.lower() in IMAGE_SUFFIXES for p in paths):
        # ponytail: audio/video takes aren't judged yet (ticket A8); first take wins.
        answers = [JudgeAnswer(question=q, score=0.5, reason="stub judge").model_dump() for q in questions]
        return PickResult(index=0, scores=[0.5] * len(paths), answers=answers, judge_score=0.5,
                          reason="stub judge: picked the first candidate")

    text = gemini.prompt(
        "judge_takes",
        context=context or "a generated asset",
        n=str(len(paths)),
        takes=", ".join(f"draft {i}" for i in range(len(paths))),
        questions="\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)),
    )
    for _ in range(gemini.RETRIES + 1):  # a schema-valid answer can still miscount drafts/questions
        verdict = gemini.generate(text, _Verdict, images=paths)
        if len(verdict.frames) == len(paths) and all(len(f.answers) == len(questions) for f in verdict.frames):
            break
    else:
        raise ValueError(f"pick_best: Gemini answered the wrong number of drafts/questions for {paths}")

    scores = [round(sum(a.score for a in f.answers) / len(f.answers), 3) for f in verdict.frames]
    i = max(range(len(scores)), key=lambda k: scores[k])
    win = verdict.frames[i]
    return PickResult(
        index=i,
        scores=scores,
        answers=[JudgeAnswer(question=q, score=a.score, reason=a.reason).model_dump() for q, a in zip(questions, win.answers)],
        judge_score=scores[i],
        reason=f"Best of {len(paths)} drafts ({scores[i]:.2f}): {win.note}",
    )


def run_frames(run_dir: Path, run: RunConfig) -> None:
    cands = Candidates.model_validate_json((run_dir / "candidates.json").read_text(encoding="utf-8"))
    summary = Summary.model_validate_json((run_dir / "summary.json").read_text(encoding="utf-8"))
    questions = questions_for(run.brief.criteria)
    all_frames = [f for s in cands.segments for f in s.frames]
    sharp = dict(zip(all_frames, sharpness([run_dir / f for f in all_frames])))
    to_judge = [frames_to_judge(s.frames, sharp, run.mode) for s in cands.segments]

    app = f"{summary.name}: {summary.one_liner}"
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        verdicts = list(pool.map(lambda fs: judge_segment(run_dir, fs, questions, app), to_judge))

    drop_stage(run_dir, "judge_frames")

    scores: list[FrameScore] = []
    for seg, frames, verdict in zip(cands.segments, to_judge, verdicts):
        rows = [
            FrameScore(
                segment_id=seg.segment_id, frame=f, heuristic_score=sharp[f],
                answers=[JudgeAnswer(question=q, score=a.score, reason=a.reason) for q, a in zip(questions, v.answers)],
                total=combine([a.score for a in v.answers], sharp[f]),
            )
            for f, v in zip(frames, verdict)
        ]
        scores += rows
        i = max(range(len(rows)), key=lambda k: rows[k].total)
        best = rows[i]
        skipped = len(seg.frames) - len(frames)
        reason = f"Top of {len(frames)} judged frames ({best.total:.2f}"
        reason += f", {skipped} blurrier skipped): " if skipped else "): "
        append_decision(
            run_dir, stage="judge_frames", kind="frame_pick", subject=seg.segment_id,
            options=seg.frames, chosen=best.frame, questions=questions, answers=[*best.answers],
            judge_score=best.total, heuristic_score=best.heuristic_score, reason=reason + verdict[i].note,
        )

    out = FrameScores(questions=questions, scores=scores)
    (run_dir / "frame_scores.json").write_text(out.model_dump_json(indent=2), encoding="utf-8")
    events.emit("judge_frames", f"Judged {len(scores)} frames in {len(cands.segments)} segments ({run.mode})")


def run_rubric(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage rubric is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    which = {"frames": "judge_frames", "rubric": "rubric"}
    if len(sys.argv) != 3 or sys.argv[1] not in which:
        raise SystemExit("usage: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>")
    stage = which[sys.argv.pop(1)]
    stage_main(stage)
