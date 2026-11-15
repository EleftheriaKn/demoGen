"""Stage `script` (owner: A).

Draft with hooks, then the truth check (Gemini).

Reads summary.json, moments.json, candidates.json, frame_scores.json, repo_files.json,
frames/, run.brief. Writes script.json, a ``hook_pick`` decision and one ``claim_check``
decision per claim.

Gemini fills a ``Draft`` whose shape fixes the beat order (hook, problem, 2-4 demo, how,
impact, ask), so only hook and ask can be cards. Code then keeps footage inside real
segments, drops evidence that doesn't exist, turns any unlisted factual sentence of a problem,
demo, how or impact line into a claim (evidence: the shot's footage) and numbers shots and claims. A second call
scores the three hooks on HOOK_QUESTIONS and flags hooks that state a fact; the best mean
among the rest wins.

Truth check: each claim is judged in its own call against its evidence alone (README section
text, key-file content, the segment's frames). Evidence that can't be shown counts as none, and
a claim with none is dropped without a call. A rewrite may only cut words from the claim; one
that adds a word, or that the judge says is still partly unsupported, is dropped. Code then edits the voice line: a rewrite replaces the claim's
words, a drop cuts the sentence holding it. A shot left with no words is removed, except the problem
shot: it comes back with ``summary.problem`` as a fact-free line, so a problem is always stated.

Voice fits footage: a footage shot's line may take about WORDS_PER_S words per second of its
clip. A longer line first gets a longer clip inside the same segment; what still doesn't fit
is trimmed (whole sentences from the end, then words) before claims are built, so the truth
check runs on what remains. Each trim is logged.

Zoom boxes (full mode only): each demo and impact shot takes its segment's ``frame_pick``
winner from decisions.json; Gemini is asked once per chosen frame for the region to zoom into.
Code keeps the box only if it lies inside the frame and is at least MIN_ZOOM_W wide, else
``focus_box`` stays null.

Run alone: python -m backend.run runs/<run_id> --only script
"""

from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean
from typing import Any, Literal, Optional, Union

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
from backend.decisions import append_decision, drop_stage
from backend.pipeline.understand import readme_headings

TARGET_S = 75
TARGET_WORDS = 190
# Word budget per beat; "demo" is shared by all demo shots. Sums to TARGET_WORDS.
BUDGET = {"hook": 12, "problem": 28, "demo": 100, "how": 25, "impact": 15, "ask": 10}
assert sum(BUDGET.values()) == TARGET_WORDS

HOOK_QUESTIONS = [
    "Does the hook make a judge curious in under 5 seconds?",
    "Is it specific to this app?",
    "Is this about what the app does for its user?",
]
HOOK_PASS = 0.5  # a hook must score at least this on the last question to win
CLAIM_QUESTION = "Does the evidence alone support the claim?"
CLAIM_OPTIONS = ["keep", "rewrite", "drop"]
STATUS = {"keep": "backed", "rewrite": "rewritten", "drop": "dropped"}
WORKERS = 6  # parallel claim checks
CHECKED_BEATS = {"problem", "demo", "how", "impact"}  # every factual sentence here must be a claim
ZOOM_BEATS = {"demo", "impact"}
MIN_ZOOM_W = 0.2  # narrower boxes zoom in too far to read
WORDS_PER_S = 2.5  # calm voice-over pace; a footage shot's line may not outrun its clip


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
    states_fact: bool = Field(
        description="True if the hook states a fact about the app (what it does, a number, a speed). "
        "A question or the viewer's problem is not a fact."
    )


class HookJudgement(BaseModel):
    verdicts: list[HookVerdict]


class ClaimVerdict(BaseModel):
    score: Score = Field(description="0 = the evidence does not show it, 1 = the evidence clearly shows all of it.")
    reason: str = Field(description="One line naming what in the evidence backs or fails it.")
    action: Literal["keep", "rewrite", "drop"] = Field(
        description="keep = fully shown; rewrite = only part is shown; drop = not shown."
    )
    rewrite: str = Field(
        description="For rewrite: the claim with the unsupported words cut, using only words already in "
        "the claim. Empty otherwise."
    )
    rewrite_supported: bool = Field(
        description="For rewrite: true only if the evidence shows every part of the rewrite. False otherwise."
    )


class FocusRegion(BaseModel):
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax], each 0-1000, relative to the frame.")
    reason: str = Field(description="One short line naming what the region shows.")


# --------------------------------------------------------------------------- #
# Code checks
# --------------------------------------------------------------------------- #


def _span(d: FootageDraft, seg: Segment) -> tuple[float, float]:
    """Clamp the clip into its segment; a clip left empty becomes the whole segment."""
    start, end = max(d.source_start, seg.start), min(d.source_end, seg.end)
    return (start, end) if end > start else (seg.start, seg.end)


def fit_voice(line: str, seg: Segment, start: float, end: float) -> tuple[float, float, str]:
    """Make ``line`` fit its clip at WORDS_PER_S: first grow the clip inside ``seg`` (end, then
    start), then trim the line to the cap, dropping whole sentences from the end and cutting
    words only if the first sentence alone is too long."""
    tokens = line.split()
    need = len(tokens) / WORDS_PER_S
    if need > end - start:
        end = min(seg.end, start + need)
        start = max(seg.start, end - need)
    cap = max(1, round(WORDS_PER_S * (end - start)))
    if len(tokens) <= cap:
        return start, end, line
    cut = max((k + 1 for k in range(cap) if _ends_sentence(tokens[k])), default=0)
    if cut == 0:  # the first sentence alone is too long: keep its first ``cap`` words
        kept = tokens[:cap]
        kept[-1] = kept[-1].rstrip(",;:—-") + ("" if _ends_sentence(kept[-1]) else ".")
        return start, end, " ".join(kept)
    return start, end, " ".join(tokens[:cut])


def unclaimed_sentences(line: str, claims: list[str]) -> list[str]:
    """Sentences of ``line`` that hold none of ``claims`` and aren't questions: facts that would skip the truth check."""
    tokens, out, sentence = line.split(), [], []
    for k, tok in enumerate(tokens):
        sentence.append(tok)
        if _ends_sentence(tok) or k == len(tokens) - 1:
            text = " ".join(sentence)
            if words(text) and not text.rstrip("\"')»”").endswith("?") and not any(
                _locate(sentence, c) for c in claims
            ):
                out.append(text)
            sentence = []
    return out


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
            start, end, line = fit_voice(d.voice_line, seg, *_span(d, seg))
            if line != d.voice_line:
                events.emit("script", f"Voice s{i:02d} ({beat}): trimmed {len(d.voice_line.split())} -> "
                            f"{len(line.split())} words to fit {end - start:.1f}s of {seg.segment_id}",
                            data={"before": d.voice_line, "after": line})
                # a claim cut away (wholly or partly) goes; what's left of it is an unclaimed sentence below
                d = d.model_copy(update={"voice_line": line,
                                         "claims": [c for c in d.claims if _locate(line.split(), c.text)]})
            visual = ShotVisual(kind="footage", segment_id=seg.segment_id, source_start=start, source_end=end)
        else:
            visual = ShotVisual(kind="card", card_prompt=d.card_prompt)
        drafts = list(d.claims)
        if beat in CHECKED_BEATS:
            # a factual sentence the draft didn't list still gets checked, against the shot's own footage
            drafts += [ClaimDraft(text=t, evidence=[Evidence(type="footage", ref=seg.segment_id)])
                       for t in unclaimed_sentences(d.voice_line, [c.text for c in d.claims])]
        claims = []
        for c in drafts:
            n_claims += 1
            evidence = [e for e in c.evidence if e.ref in real[e.type]]
            claims.append(Claim(claim_id=f"cl{n_claims}", text=c.text, evidence=evidence, status="pending"))
        shots.append(Shot(shot_id=f"s{i:02d}", beat=beat, voice_line=d.voice_line, visual=visual,
                          claims=claims, generated=visual.kind != "footage"))
    return shots


def score_hooks(texts: list[str], judgement: HookJudgement, app: str) -> tuple[list[Hook], Optional[HookVerdict]]:
    """Hook score = mean answer score; the best hook that states no fact and passes the last
    question (about what the app does for its user) wins (first on ties).

    If no hook qualifies, the app's name is added as a last, unjudged hook and wins:
    the result's verdict is then None.
    """
    verdicts = {v.hook_id: v for v in judgement.verdicts}
    hooks = []
    for i, text in enumerate(texts, 1):
        v = verdicts.get(f"h{i}")
        if v is None or len(v.answers) != len(HOOK_QUESTIONS):
            raise ValueError(f"script: hook judge gave no full verdict for h{i}")
        hooks.append(Hook(hook_id=f"h{i}", text=text, score=round(mean(a.score for a in v.answers), 2)))
    clean = [h for h in hooks if not verdicts[h.hook_id].states_fact
             and verdicts[h.hook_id].answers[-1].score >= HOOK_PASS]
    if not clean:
        return [*hooks, Hook(hook_id=f"h{len(hooks) + 1}", text=app, score=0)], None
    best = max(clean, key=lambda h: h.score)
    return hooks, verdicts[best.hook_id]


# --------------------------------------------------------------------------- #
# Truth check
# --------------------------------------------------------------------------- #


def _norm(token: str) -> str:
    return re.sub(r"\W", "", token.lower())


def words(text: str) -> list[str]:
    """Lower-case words without punctuation: how claims and voice lines are compared."""
    return [w for w in map(_norm, text.split()) if w]


def only_cuts(rewrite: str, claim: str) -> bool:
    """True if ``rewrite`` has words and each is already in ``claim`` (as often), so it adds nothing."""
    return bool(words(rewrite)) and not Counter(words(rewrite)) - Counter(words(claim))


def _locate(tokens: list[str], claim: str) -> Optional[tuple[int, int]]:
    """Token span [i, j) whose words are exactly the claim's, or None."""
    idx = [k for k, t in enumerate(tokens) if _norm(t)]
    have, want = [_norm(tokens[k]) for k in idx], words(claim)
    for s in range(len(have) - len(want) + 1) if want else ():
        if have[s:s + len(want)] == want:
            return idx[s], idx[s + len(want) - 1] + 1
    return None


def _ends_sentence(token: str) -> bool:
    return token.rstrip("\"')»”").endswith((".", "!", "?", "。", "！", "？"))


def edit_line(line: str, claim: str, new: str) -> Optional[str]:
    """Put ``new`` where ``claim``'s words are in ``line``; an empty ``new`` cuts the claim's sentence.

    Returns None if the claim's words aren't in the line.
    """
    tokens = line.split()
    span = _locate(tokens, claim)
    if span is None:
        return None
    i, j = span
    if new:
        rep = new.split()
        rep[-1] = rep[-1].rstrip(".,;:!?") + re.search(r"\W*$", tokens[j - 1]).group()  # the line's punctuation
        if tokens[i][:1].isupper():
            rep[0] = rep[0][:1].upper() + rep[0][1:]
        tokens[i:j] = rep
    else:
        start = next((k + 1 for k in range(i - 1, -1, -1) if _ends_sentence(tokens[k])), 0)
        end = next((k + 1 for k in range(j - 1, len(tokens)) if _ends_sentence(tokens[k])), len(tokens))
        tokens[start:end] = []
    return " ".join(tokens)


def apply_checks(shots: list[Shot], checked: dict[str, Claim]) -> list[Shot]:
    """Swap in the checked claims and edit each voice line to match; drop shots left with no words."""
    out = []
    for shot in shots:
        line, claims = shot.voice_line, [checked[c.claim_id] for c in shot.claims]
        for old, new in zip(shot.claims, claims):
            if new.status == "backed":
                continue
            edited = edit_line(line, old.text, new.text if new.status == "rewritten" else "")
            if edited is not None:
                line = edited
            elif _locate(shot.voice_line.split(), old.text) is None:
                # ponytail: the claim isn't worded as in the line, so its words can't be found and
                # the whole line goes. Finding it by meaning would need another call.
                line = ""
            # else: already cut with an earlier claim's sentence
        if words(line):
            out.append(shot.model_copy(update={"voice_line": line, "claims": claims}))
    return out


def keep_problem(shots: list[Shot], drafted: list[Shot], problem: str) -> list[Shot]:
    """If the truth check emptied the problem shot, put it back with ``summary.problem`` as a
    fact-free line (no claim to check) on the same footage, right after the hook."""
    if any(s.beat == "problem" for s in shots):
        return shots
    shot = next(s for s in drafted if s.beat == "problem").model_copy(update={"voice_line": problem, "claims": []})
    i = next((k + 1 for k, s in enumerate(shots) if s.beat == "hook"), 0)
    return [*shots[:i], shot, *shots[i:]]


def readme_section(readme: str, heading: str) -> str:
    """The heading's line and its text up to the next heading of the same or a higher level."""
    level, out, fenced = None, [], False
    for line in readme.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#+)\s+(.+?)(?:\s+#+)?\s*$", line)
        if level is None:
            if m and m.group(2) == heading:
                level = len(m.group(1))
                out.append(line)
        elif m and len(m.group(1)) <= level:
            break
        else:
            out.append(line)
    return "\n".join(out).strip()


def evidence_shown(evidence: list[Evidence], run_dir: Path, segments: dict[str, Segment],
                   repo: RepoFiles) -> tuple[list[str], list[Path]]:
    """What the judge sees for a claim's evidence: text blocks and frame images.

    A file whose content ingest didn't keep, or a segment with no frames on disk, shows
    nothing and counts as no evidence.
    """
    files = {k.path: k.content for k in repo.key_files}
    blocks: list[str] = []
    images: list[Path] = []
    for e in evidence:
        if e.type == "readme":
            body = readme_section(repo.readme, e.ref.removeprefix("README#"))
            if body:
                blocks.append(f"README section:\n{body}")
        elif e.type == "file":
            if files.get(e.ref, "").strip():
                blocks.append(f"File {e.ref}:\n{files[e.ref]}")
        else:
            seg = segments.get(e.ref)
            frames = [f for f in (seg.frames if seg else []) if (run_dir / f).exists()]
            if seg and frames:
                blocks.append(f"Footage {seg.segment_id}, {seg.start:g}-{seg.end:g} s of the recording: "
                              f"{len(frames)} frames attached in order ({', '.join(frames)})")
                images += [run_dir / f for f in frames]
    return blocks, images


def check_claim(claim: Claim, blocks: list[str], images: list[Path]) -> tuple[Claim, ClaimVerdict]:
    """Judge one claim against what its evidence shows. Returns the checked claim and the verdict."""
    if not blocks:
        verdict = ClaimVerdict(score=0, reason="No evidence to check it against.", action="drop", rewrite="",
                               rewrite_supported=False)
    else:
        text = gemini.prompt("script_claim", claim=claim.text, evidence="\n\n".join(blocks))
        verdict = gemini.generate(text, ClaimVerdict, images=images)
    action, new = verdict.action, claim.text
    if action == "rewrite":
        if not only_cuts(verdict.rewrite, claim.text):
            action = "drop"
            verdict = verdict.model_copy(update={"reason": f"{verdict.reason} (the rewrite added words)"})
        elif not verdict.rewrite_supported:
            action = "drop"
            verdict = verdict.model_copy(update={"reason": f"{verdict.reason} (the rewrite is still partly unsupported)"})
        elif words(verdict.rewrite) == words(claim.text):
            action = "keep"
        else:
            new = verdict.rewrite.strip()
    return claim.model_copy(update={"status": STATUS[action], "text": new}), verdict


# --------------------------------------------------------------------------- #
# Zoom boxes
# --------------------------------------------------------------------------- #


def focus_box(box_2d: list[int]) -> Optional[list[float]]:
    """Gemini's [ymin, xmin, ymax, xmax] (0-1000) -> [x, y, w, h] in 0-1, or None if it leaves
    the frame, is empty or is narrower than MIN_ZOOM_W."""
    if len(box_2d) != 4:
        return None
    y0, x0, y1, x1 = (v / 1000 for v in box_2d)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1) or x1 - x0 < MIN_ZOOM_W:
        return None
    return [x0, y0, x1 - x0, y1 - y0]


def ask_focus(frame: Path, lines: list[str]) -> FocusRegion:
    text = gemini.prompt("script_zoom", lines="\n".join(f'"{line}"' for line in lines))
    return gemini.generate(text, FocusRegion, images=[frame])


def add_zoom(run_dir: Path, shots: list[Shot]) -> list[Shot]:
    """Fill ``visual.focus_box`` of demo/impact footage shots from their segment's chosen frame."""
    path = run_dir / "decisions.json"
    log = Decisions.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
    picks = {d.subject: d.chosen for d in (log.decisions if log else []) if d.kind == "frame_pick"}
    frame_of = {s.shot_id: picks.get(s.visual.segment_id or "") for s in shots
                if s.beat in ZOOM_BEATS and s.visual.kind == "footage"}
    frame_of = {k: f for k, f in frame_of.items() if f and (run_dir / f).exists()}
    lines: dict[str, list[str]] = {}
    for s in shots:
        if s.shot_id in frame_of:
            lines.setdefault(frame_of[s.shot_id], []).append(s.voice_line)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:  # one call per chosen frame
        regions = dict(zip(lines, pool.map(lambda f: ask_focus(run_dir / f, lines[f]), lines)))

    out = []
    for s in shots:
        if s.shot_id in frame_of:
            region = regions[frame_of[s.shot_id]]
            box = focus_box(region.box_2d)
            note = f"{[round(v, 3) for v in box]} ({region.reason})" if box else f"none, {region.box_2d} rejected"
            events.emit("script", f"Zoom {s.shot_id} from {frame_of[s.shot_id]}: {note}", data={"focus_box": box})
            s = s.model_copy(update={"visual": s.visual.model_copy(update={"focus_box": box})})
        out.append(s)
    return out


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
        tree=json.dumps([k.path for k in repo.key_files]),  # only files the truth check can read
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


def run(run_dir: Path, run: RunConfig) -> None:
    def load(name, model):
        return model.model_validate_json((run_dir / name).read_text(encoding="utf-8"))

    summary, repo = load("summary.json", Summary), load("repo_files.json", RepoFiles)
    segments = load("candidates.json", Candidates).segments
    lines = _segment_lines(segments, load("moments.json", Moments), load("frame_scores.json", FrameScores))

    draft = draft_script(run, summary, lines, repo)
    judgement = judge_hooks(summary, draft.hooks)
    hooks, verdict = score_hooks(draft.hooks, judgement, summary.name)
    chosen = next(h for h in hooks if h.hook_id == verdict.hook_id) if verdict else hooks[-1]
    drafted = build_shots(draft, chosen.text, segments, repo)

    by_id = {s.segment_id: s for s in segments}
    claims = [c for s in drafted for c in s.claims]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda c: check_claim(c, *evidence_shown(c.evidence, run_dir, by_id, repo)), claims))
    shots = keep_problem(apply_checks(drafted, {c.claim_id: c for c, _ in results}), drafted, summary.problem)
    if run.mode != "cheap":
        shots = add_zoom(run_dir, shots)
    script = Script(language=run.brief.language, hooks=hooks, chosen_hook_id=chosen.hook_id, shots=shots)
    (run_dir / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

    drop_stage(run_dir, "script")
    factual = [v.hook_id for v in judgement.verdicts if v.states_fact]
    skipped = f"; {', '.join(factual)} state a fact" if factual else ""
    others = ", ".join(f"{h.hook_id} {h.score:.2f}" for h in hooks if h is not chosen)
    if verdict:
        answers = [JudgeAnswer(question=q, score=a.score, reason=a.reason) for q, a in zip(HOOK_QUESTIONS, verdict.answers)]
        reason = f"{chosen.hook_id} scored {chosen.score:.2f} (vs {others}{skipped}): {verdict.answers[0].reason}"
    else:
        answers = [JudgeAnswer(question=q, score=0, reason="Not judged: fallback hook.") for q in HOOK_QUESTIONS]
        reason = f"Every drafted hook states a fact, so the hook is the app's name ({others})"
    append_decision(
        run_dir,
        stage="script",
        kind="hook_pick",
        subject="hook",
        options=[h.hook_id for h in hooks],
        chosen=chosen.hook_id,
        questions=HOOK_QUESTIONS,
        answers=answers,
        judge_score=chosen.score,
        reason=reason,
    )
    action_of = {v: k for k, v in STATUS.items()}
    for checked, v in results:
        action = action_of[checked.status]
        append_decision(
            run_dir,
            stage="script",
            kind="claim_check",
            subject=checked.claim_id,
            options=CLAIM_OPTIONS,
            chosen=action,
            questions=[CLAIM_QUESTION],
            answers=[JudgeAnswer(question=CLAIM_QUESTION, score=v.score, reason=v.reason)],
            judge_score=v.score,
            reason=(f'Rewritten to "{checked.text}": ' if action == "rewrite" else "") + v.reason,
        )

    tally = Counter(c.status for c, _ in results)
    events.emit("script", f"Truth check: {tally['backed']} backed, {tally['rewritten']} rewritten, "
                f"{tally['dropped']} dropped; {len(drafted) - len(shots)} shots left with no words removed")
    words_per = {b: sum(len(s.voice_line.split()) for s in shots if s.beat == b) for b in BUDGET}
    over = [f"{b} {w}/{BUDGET[b]}" for b, w in words_per.items() if w > BUDGET[b]]
    events.emit("script", f"Script: {len(shots)} shots, {sum(words_per.values())}/{TARGET_WORDS} words (~{TARGET_S} s)"
                + (f"; over budget: {', '.join(over)}" if over else ""), data={"words": words_per})


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("script")
