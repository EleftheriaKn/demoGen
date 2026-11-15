"""Judge (owner: A).

Two stages plus one function other tracks call:

- ``run_frames``  stage ``judge_frames``: reads candidates.json + frames/ (+ summary.json for
  the app's one-liner), writes
  frame_scores.json and one ``frame_pick`` decision per segment. Each judged frame gets the
  fixed question per criterion (generic one for an unplanned criterion), and
  total = 80% mean judge score + 20% sharpness. Cheap mode judges only the 2 sharpest frames
  of each segment. Re-running replaces this stage's earlier decisions.
- ``run_rubric``  stage ``rubric``: reads timeline.json, script.json, brief; writes the
  rubric (and an optional ``recut``) into decisions.json. Seven fixed checks: time limit,
  "problem by 0:20", at least 60% of the script's target length, at least half real footage and every
  generated middle shot backed by README/files in code; claims backed and core result shown by Gemini
  (skipped in cheap mode). A recut is asked only when a failure is pinned on one shot that is in the timeline.
- ``pick_best``   called by the assets stage to choose among N generated drafts (images judged; audio/video stubbed).

Run alone: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, TypedDict, Union

from PIL import Image, ImageFilter, ImageStat
from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.contracts import (
    Candidates, Decisions, FrameScore, FrameScores, JudgeAnswer, Recut, RubricItem, RunConfig, Script, Summary, Timeline,
)
from backend.decisions import append_decision, drop_stage
from backend.pipeline.script import target

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
    raise ValueError(f"{gemini.INCOMPLETE} [judge_frames: Gemini answered the wrong number of frames/questions for {frames}]")


class PickResult(TypedDict):
    """What ``pick_best`` returns. Enough for the caller to build a ``take_pick`` Decision."""

    index: int  # index into ``candidates`` of the winner
    scores: list[float]  # one 0-1 score per candidate, same order as ``candidates``
    answers: list[dict]  # JudgeAnswer dicts for the winner, one per question, same order
    judge_score: float  # 0-1, == scores[index]
    reason: str  # one line, shown in the UI


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PASS = 0.5  # a candidate under this on the ``must_pass`` question can't win


def pick_best(
    candidates: list[Union[str, Path]], questions: list[str], context: str = "", must_pass: Optional[str] = None
) -> PickResult:
    """Pick the best of N candidate files against ``questions``; ``context`` says what they are for.

    Images: Gemini sees every draft and answers each question per draft; the winner has the
    highest mean score among drafts scoring at least PASS on ``must_pass`` (one of ``questions``).
    If none pass, the best mean wins anyway, so the caller sees the winner fail ``must_pass``.
    Audio and video are still a stub (index 0, neutral scores).
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
        raise ValueError(f"{gemini.INCOMPLETE} [pick_best: Gemini answered the wrong number of drafts/questions for {paths}]")

    scores = [round(sum(a.score for a in f.answers) / len(f.answers), 3) for f in verdict.frames]
    gate = questions.index(must_pass) if must_pass is not None else None
    passes = [gate is None or f.answers[gate].score >= PASS for f in verdict.frames]
    i = max(range(len(scores)), key=lambda k: (passes[k], scores[k]))
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


PROBLEM_BY_S = 20.0
MIN_LENGTH_SHARE = 0.6  # of the script's target length
MIN_FOOTAGE_SHARE = 0.5  # of the runtime
Q_PROBLEM = "Problem stated by 0:20?"
Q_GENERATED = "Does every generated middle shot rest on a claim backed by the repo?"
MIDDLE_BEATS = ("problem", "demo", "how", "impact")  # hook and ask cards state no facts
Q_CLAIMS = "Is every remaining claim backed by footage or the repo?"
Q_RESULT = "Is the app's core result shown on screen?"
GEMINI_QUESTIONS = [Q_CLAIMS, Q_RESULT]
SKIPPED = "Skipped in cheap mode (not judged)."

Check = tuple[RubricItem, Optional[str]]  # item, shot to recut if the failure is that one shot's


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def check_time_limit(timeline: Timeline, limit_s: int) -> Check:
    """Too long is the whole video's fault, so it never names a shot."""
    ok = timeline.duration_s <= limit_s
    item = RubricItem(question=f"Under the {limit_s} s time limit?", passed=ok,
                      reason=f"Final cut is {timeline.duration_s:.0f} s" + ("." if ok else f", over by {timeline.duration_s - limit_s:.0f} s."))
    return item, None


def check_min_length(timeline: Timeline, limit_s: int) -> Check:
    """Too short is the whole video's fault, so it never names a shot."""
    target_s = target(limit_s)[0]
    ok = timeline.duration_s >= MIN_LENGTH_SHARE * target_s
    reason = f"Final cut is {timeline.duration_s:.0f} s, " + ("at least" if ok else "under") + f" {MIN_LENGTH_SHARE:.0%} of the {target_s} s target."
    return RubricItem(question=f"At least {MIN_LENGTH_SHARE:.0%} of the {target_s} s target?", passed=ok, reason=reason), None


def check_footage_share(timeline: Timeline) -> Check:
    """Too little real footage is the whole cut's fault, so it never names a shot."""
    share = sum(s.duration_s for s in timeline.shots if s.visual.kind == "footage") / timeline.duration_s
    ok = share >= MIN_FOOTAGE_SHARE
    return RubricItem(question=f"At least {MIN_FOOTAGE_SHARE:.0%} real footage?", passed=ok,
                      reason=f"Real footage is {int(share * 100)}% of the runtime."), None


def check_generated_backed(timeline: Timeline, script: Script) -> Check:
    """Every generated middle shot in the cut has a kept claim backed by README or a file. One fails: recut it."""
    in_cut = {s.shot_id for s in timeline.shots}
    bad = [
        s.shot_id for s in script.shots
        if s.generated and s.beat in MIDDLE_BEATS and s.shot_id in in_cut
        and not any(c.status in ("backed", "rewritten") and any(e.type in ("readme", "file") for e in c.evidence)
                    for c in s.claims)
    ]
    if not bad:
        return RubricItem(question=Q_GENERATED, passed=True, reason="Every generated middle shot has a claim backed by the repo."), None
    return RubricItem(question=Q_GENERATED, passed=False,
                      reason=f"No claim backed by README or a file in {', '.join(bad)}."), (bad[0] if len(bad) == 1 else None)


def check_problem_by_20(timeline: Timeline, script: Script) -> Check:
    """The first problem shot starts by 0:20. Late with one shot before it: that shot is to blame."""
    beats = {s.shot_id: s.beat for s in script.shots}
    first = next((s for s in timeline.shots if beats.get(s.shot_id) == "problem"), None)
    if first is None:
        return RubricItem(question=Q_PROBLEM, passed=False, reason="No problem shot in the cut."), None
    if first.start <= PROBLEM_BY_S:
        return RubricItem(question=Q_PROBLEM, passed=True, reason=f"Problem beat ({first.shot_id}) starts at {_mmss(first.start)}."), None
    before = [s for s in timeline.shots if s.start < first.start]
    # ponytail: several shots before it = an ordering problem, no single shot to redo.
    shot = before[0].shot_id if len(before) == 1 else None
    reason = f"Problem beat ({first.shot_id}) starts at {_mmss(first.start)}"
    reason += f", after {shot} ({before[0].duration_s:.0f} s)." if shot else "."
    return RubricItem(question=Q_PROBLEM, passed=False, reason=reason), shot


class _Check(BaseModel):
    passed: bool = Field(description="Whether the cut passes this question.")
    reason: str = Field(description="One short line naming the shots that decide it.")
    shot_id: str = Field(description="If it fails because of exactly one timeline shot, that shot's id; else ''.")


class _Rubric(BaseModel):
    checks: list[_Check] = Field(description="One per question, same order.")


def gemini_checks(script: Script, timeline: Timeline, run: RunConfig) -> list[Check]:
    text = gemini.prompt(
        "rubric",
        criteria=", ".join(run.brief.criteria),
        time_limit=str(run.brief.time_limit_s),
        questions="\n".join(f"{i}. {q}" for i, q in enumerate(GEMINI_QUESTIONS, 1)),
        script=script.model_dump_json(indent=1),
        timeline=timeline.model_dump_json(indent=1),
    )
    for _ in range(gemini.RETRIES + 1):  # a schema-valid answer can still miscount questions
        verdict = gemini.generate(text, _Rubric)
        if len(verdict.checks) == len(GEMINI_QUESTIONS):
            break
    else:
        raise ValueError(f"{gemini.INCOMPLETE} [rubric: Gemini answered the wrong number of questions]")
    in_cut = {s.shot_id for s in timeline.shots}
    return [
        (RubricItem(question=q, passed=c.passed, reason=c.reason),
         c.shot_id if not c.passed and c.shot_id in in_cut else None)
        for q, c in zip(GEMINI_QUESTIONS, verdict.checks)
    ]


def run_rubric(run_dir: Path, run: RunConfig) -> None:
    script = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8"))
    timeline = Timeline.model_validate_json((run_dir / "timeline.json").read_text(encoding="utf-8"))
    limit = run.brief.time_limit_s
    checks = [check_problem_by_20(timeline, script), check_time_limit(timeline, limit),
              check_min_length(timeline, limit), check_footage_share(timeline), check_generated_backed(timeline, script)]
    if run.mode == "cheap":
        # The contract has no "skipped" flag: a skipped check passes (it can't block or recut) and says so.
        checks += [(RubricItem(question=q, passed=True, reason=SKIPPED), None) for q in GEMINI_QUESTIONS]
    else:
        checks += gemini_checks(script, timeline, run)

    items = [i for i, _ in checks]
    blame = next(((i, shot) for i, shot in checks if shot is not None), None)
    recut = Recut(shot_id=blame[1], reason=f"{blame[0].question} {blame[0].reason}") if blame else None
    judged = [i for i in items if i.reason != SKIPPED]
    n_pass = sum(i.passed for i in judged)
    reason = f"{n_pass}/{len(judged)} rubric checks pass"
    reason += f" ({len(items) - len(judged)} skipped)" if len(judged) < len(items) else ""
    if recut:
        reason += f"; recut {recut.shot_id}."
    elif n_pass < len(judged):
        reason += "; failures are not one shot's, no recut."
    else:
        reason += "; no recut needed."

    drop_stage(run_dir, "rubric")
    append_decision(
        run_dir, stage="rubric", kind="rubric_check", subject="final", options=["pass", "recut"],
        chosen="recut" if recut else "pass", questions=[i.question for i in items],
        answers=[JudgeAnswer(question=i.question, score=0.5 if i.reason == SKIPPED else float(i.passed), reason=i.reason) for i in items],
        judge_score=round(n_pass / len(judged), 3), reason=reason,
    )
    path = run_dir / "decisions.json"
    log = Decisions.model_validate_json(path.read_text(encoding="utf-8"))
    log = Decisions.model_validate({**log.model_dump(), "rubric": [i.model_dump() for i in items], "recut": recut and recut.model_dump()})
    path.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    events.emit("rubric", reason)


if __name__ == "__main__":
    from backend.run import stage_main

    which = {"frames": "judge_frames", "rubric": "rubric"}
    if len(sys.argv) != 3 or sys.argv[1] not in which:
        raise SystemExit("usage: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>")
    stage = which[sys.argv.pop(1)]
    stage_main(stage)
