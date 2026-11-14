"""Stage `script` (owner: A).

Draft with hooks (Gemini). The truth check (ticket 5) is not built yet: every claim stays
``pending``.

Reads summary.json, moments.json, candidates.json, frame_scores.json, repo_files.json,
run.brief. Writes script.json and a ``hook_pick`` decision.

Gemini fills a ``Draft`` whose shape fixes the beat order (hook, problem, 2-4 demo, how,
impact, ask), so only hook and ask can be cards. Code then keeps footage inside real
segments, drops evidence that doesn't exist and numbers shots and claims. A second call
scores the three hooks on HOOK_QUESTIONS; the best mean wins.

Run alone: python -m backend.run runs/<run_id> --only script
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any, Union

from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.contracts import (
    Candidates,
    Claim,
    Decisions,
    Evidence,
    FrameScores,
    Hook,
    JudgeAnswer,
    Moments,
    RepoFiles,
    RunConfig,
    Score,
    Script,
    Segment,
    Shot,
    ShotVisual,
    Summary,
)
from backend.decisions import append_decision
from backend.pipeline.understand import readme_headings

TARGET_S = 75
TARGET_WORDS = 190
# Word budget per beat; "demo" is shared by all demo shots. Sums to TARGET_WORDS.
BUDGET = {"hook": 12, "problem": 28, "demo": 100, "how": 25, "impact": 15, "ask": 10}
assert sum(BUDGET.values()) == TARGET_WORDS

HOOK_QUESTIONS = [
    "Does the hook make a judge curious in under 5 seconds?",
    "Is it specific to this app?",
]


# --------------------------------------------------------------------------- #
# What Gemini returns
# --------------------------------------------------------------------------- #


class ClaimDraft(BaseModel):
    text: str = Field(description="The factual statement, worded as in the voice line.")
    evidence: list[Evidence] = Field(description="Proof for it; empty if none.")


class FootageDraft(BaseModel):
    voice_line: str
    segment_id: str = Field(description="A segment id from the list.")
    source_start: float = Field(description="Clip start in recording seconds, inside the segment.")
    source_end: float = Field(description="Clip end in recording seconds, inside the segment.")
    claims: list[ClaimDraft]


class CardDraft(BaseModel):
    voice_line: str
    card_prompt: str
    claims: list[ClaimDraft]


class Draft(BaseModel):
    hooks: list[str] = Field(min_length=3, max_length=3, description="Three candidate opening lines.")
    hook_card_prompt: str
    problem: FootageDraft
    demos: list[FootageDraft] = Field(min_length=2, max_length=4)
    how: FootageDraft
    impact: FootageDraft
    ask: CardDraft


class Answer(BaseModel):
    score: Score
    reason: str = Field(description="One-line reason.")


class HookVerdict(BaseModel):
    hook_id: str
    answers: list[Answer] = Field(description="One answer per question, in question order.")


class HookJudgement(BaseModel):
    verdicts: list[HookVerdict]


# --------------------------------------------------------------------------- #
# Code checks
# --------------------------------------------------------------------------- #


def _span(d: FootageDraft, seg: Segment) -> tuple[float, float]:
    """Clamp the clip into its segment; a clip left empty becomes the whole segment."""
    start, end = max(d.source_start, seg.start), min(d.source_end, seg.end)
    return (start, end) if end > start else (seg.start, seg.end)


def build_shots(draft: Draft, hook_line: str, segments: list[Segment], repo: RepoFiles) -> list[Shot]:
    """Draft -> shots in beat order, footage inside real segments, claims ``pending``."""
    by_id = {s.segment_id: s for s in segments}
    real = {
        "footage": set(by_id),
        "readme": {f"README#{h}" for h in readme_headings(repo.readme)},
        "file": set(repo.tree),
    }
    beats: list[tuple[Any, Union[FootageDraft, CardDraft]]] = [
        ("hook", CardDraft(voice_line=hook_line, card_prompt=draft.hook_card_prompt, claims=[])),
        ("problem", draft.problem),
        *(("demo", d) for d in draft.demos),
        ("how", draft.how),
        ("impact", draft.impact),
        ("ask", draft.ask),
    ]
    shots, n_claims = [], 0
    for i, (beat, d) in enumerate(beats, 1):
        if isinstance(d, FootageDraft):
            seg = by_id.get(d.segment_id)
            if seg is None:
                raise ValueError(f"script: {beat} shot uses unknown segment {d.segment_id!r}")
            start, end = _span(d, seg)
            visual = ShotVisual(kind="footage", segment_id=seg.segment_id, source_start=start, source_end=end)
        else:
            visual = ShotVisual(kind="card", card_prompt=d.card_prompt)
        claims = []
        for c in d.claims:
            n_claims += 1
            evidence = [e for e in c.evidence if e.ref in real[e.type]]
            claims.append(Claim(claim_id=f"cl{n_claims}", text=c.text, evidence=evidence, status="pending"))
        shots.append(Shot(shot_id=f"s{i:02d}", beat=beat, voice_line=d.voice_line, visual=visual,
                          claims=claims, generated=visual.kind != "footage"))
    return shots


def score_hooks(texts: list[str], judgement: HookJudgement) -> tuple[list[Hook], HookVerdict]:
    """Hook score = mean answer score. Returns the hooks and the winning verdict (first on ties)."""
    verdicts = {v.hook_id: v for v in judgement.verdicts}
    hooks = []
    for i, text in enumerate(texts, 1):
        v = verdicts.get(f"h{i}")
        if v is None or len(v.answers) != len(HOOK_QUESTIONS):
            raise ValueError(f"script: hook judge gave no full verdict for h{i}")
        hooks.append(Hook(hook_id=f"h{i}", text=text, score=round(mean(a.score for a in v.answers), 2)))
    best = max(hooks, key=lambda h: h.score)
    return hooks, verdicts[best.hook_id]


# --------------------------------------------------------------------------- #
# Stage
# --------------------------------------------------------------------------- #


def _segment_lines(segments: list[Segment], moments: Moments, scores: FrameScores) -> str:
    kinds = {m.moment_id: m for m in moments.moments}
    best: dict[str, float] = {}
    for fs in scores.scores:
        best[fs.segment_id] = max(best.get(fs.segment_id, 0.0), fs.total)
    lines = []
    for s in segments:
        m = kinds.get(s.moment_id or "")
        what = f"{m.kind}: {m.description}" if m else "scene cut"
        lines.append(f"{s.segment_id} {s.start:g}-{s.end:g}s, {what}, score {best.get(s.segment_id, 0):.2f}")
    return "\n".join(lines)


def draft_script(run: RunConfig, summary: Summary, segment_lines: str, repo: RepoFiles) -> Draft:
    text = gemini.prompt(
        "script_draft",
        language=run.brief.language,
        budget="\n".join(f"- {b}: {w} words" + (" across all demo shots" if b == "demo" else "")
                         for b, w in BUDGET.items()),
        criteria=", ".join(run.brief.criteria),
        summary=summary.model_dump_json(indent=2),
        segments=segment_lines,
        headings=json.dumps(sorted(readme_headings(repo.readme))),
        tree=json.dumps(repo.tree),
    )
    return gemini.generate(text, Draft)


def judge_hooks(summary: Summary, texts: list[str]) -> HookJudgement:
    text = gemini.prompt(
        "script_hooks",
        app=summary.name,
        one_liner=summary.one_liner,
        questions="\n".join(f"{i}. {q}" for i, q in enumerate(HOOK_QUESTIONS, 1)),
        hooks="\n".join(f"h{i}: {t}" for i, t in enumerate(texts, 1)),
    )
    return gemini.generate(text, HookJudgement)


def _drop_own_decisions(run_dir: Path) -> None:
    """A re-run replaces this stage's decisions instead of adding a second set."""
    path = run_dir / "decisions.json"
    if path.exists():
        log = Decisions.model_validate_json(path.read_text())
        log.decisions = [d for d in log.decisions if d.stage != "script"]
        path.write_text(log.model_dump_json(indent=2))


def run(run_dir: Path, run: RunConfig) -> None:
    def load(name, model):
        return model.model_validate_json((run_dir / name).read_text(encoding="utf-8"))

    summary, repo = load("summary.json", Summary), load("repo_files.json", RepoFiles)
    segments = load("candidates.json", Candidates).segments
    lines = _segment_lines(segments, load("moments.json", Moments), load("frame_scores.json", FrameScores))

    draft = draft_script(run, summary, lines, repo)
    hooks, verdict = score_hooks(draft.hooks, judge_hooks(summary, draft.hooks))
    chosen = next(h for h in hooks if h.hook_id == verdict.hook_id)
    shots = build_shots(draft, chosen.text, segments, repo)
    script = Script(language=run.brief.language, hooks=hooks, chosen_hook_id=chosen.hook_id, shots=shots)
    (run_dir / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

    _drop_own_decisions(run_dir)
    others = ", ".join(f"{h.hook_id} {h.score:.2f}" for h in hooks if h is not chosen)
    append_decision(
        run_dir,
        stage="script",
        kind="hook_pick",
        subject="hook",
        options=[h.hook_id for h in hooks],
        chosen=chosen.hook_id,
        questions=HOOK_QUESTIONS,
        answers=[JudgeAnswer(question=q, score=a.score, reason=a.reason)
                 for q, a in zip(HOOK_QUESTIONS, verdict.answers)],
        judge_score=chosen.score,
        reason=f"{chosen.hook_id} scored {chosen.score:.2f} (vs {others}): {verdict.answers[0].reason}",
    )

    words = {b: sum(len(s.voice_line.split()) for s in shots if s.beat == b) for b in BUDGET}
    over = [f"{b} {w}/{BUDGET[b]}" for b, w in words.items() if w > BUDGET[b]]
    events.emit("script", f"Script: {len(shots)} shots, {sum(words.values())}/{TARGET_WORDS} words (~{TARGET_S} s)"
                + (f"; over budget: {', '.join(over)}" if over else ""), data={"words": words})


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("script")
