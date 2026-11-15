"""Stage `script` (owner: A).

Draft with hooks, then the truth check (Gemini).

Reads summary.json, moments.json, candidates.json, frame_scores.json, repo_files.json,
frames/, run.brief. Writes script.json, a ``hook_pick`` decision and one ``claim_check``
decision per claim.

Length follows the brief: the script aims for TARGET_SHARE of ``time_limit_s`` (at most TARGET_S)
at TARGET_RATE words per second, and the per-beat BUDGET scales to that. A short target drops
beats: up to 30 s it is hook, problem, 1 demo, ask; up to 45 s hook, problem, 1-2 demo, impact,
ask; longer, the full hook, problem, 2-4 demo, how, impact, ask.

Gemini fills a ``Draft`` (``DraftMid``, ``DraftShort`` for short targets) whose shape fixes the
beat order. Without a flow (an old summary.json) only hook and ask are cards. With ``summary.flow``
the prompt also gets the flow, each moment's quality and step and the missing steps (the brief comes
through B's ``{brief_block}``), and
the ``...G`` variants let a middle shot be generated (a card with ``reference_frames``). Code
decides the treatment: footage on a weak moment becomes a generated panel of its own frames; a
generated problem / demo shot on a step a wow / ok moment shows goes back to footage; a missing
step's panel takes frames of the nearest shown step. Generated shots' claims lose footage evidence
and gain their flow step's README / file evidence; a demo claim, or a footage claim with no repo
evidence, carries its own segment. Generated middle shots past MIN_REAL of the planned runtime are
dropped, last first (before and after the truth check). Code then keeps footage inside real
segments, drops evidence that doesn't exist, turns any unlisted factual sentence of a problem,
demo, how or impact line into a claim (evidence: the shot's footage) and numbers shots and claims. A second call
scores the three hooks on HOOK_QUESTIONS and flags hooks that state a fact; the best mean
among the rest wins.

Truth check: each claim is judged in its own call against its evidence alone (README section
text, key-file content, the segment's frames). Footage frames go in time order, with the last
frame before the segment and the first after it, so an action shows as a change between frames.
A generated shot (no footage on screen) is checked against README and files only. Evidence that can't be shown counts as none, and
a claim with none is dropped without a call. A rewrite may only cut words from the claim; one
that adds a word, or that the judge says is still partly unsupported, is dropped. Code then edits the voice line: a rewrite replaces the claim's
words, a drop cuts the sentence holding it. A shot left with no words is removed, except the problem
shot: it comes back with ``summary.problem`` as a fact-free line, so a problem is always stated.

Voice fits footage: a footage shot's line may take about WORDS_PER_S words per second of its
clip plus HOLD_S (the timeline holds the last frame that long). A longer line first gets a longer
clip inside the same segment; what still doesn't fit loses whole sentences from the end (never
below the first sentence, never mid-sentence) before claims are built, so the truth check runs on
what remains. Each trim is logged.

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
    FlowStep,
    FrameScores,
    Hook,
    JudgeAnswer,
    Moment,
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
from backend.pipeline.understand import missing_steps, readme_headings

TARGET_S = 75  # the longest script, whatever the limit
TARGET_WORDS = 190
TARGET_SHARE = 0.85  # of the brief's time limit, so cards and holds still fit under it
TARGET_RATE = 2.5  # spoken words per second a shorter target aims for
# Word budget per beat at TARGET_WORDS; "demo" is shared by all demo shots. Scaled for shorter targets.
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
WORDS_PER_S = 3.0  # voice-over pace; a footage shot's line may not outrun its clip (+ HOLD_S)
HOLD_S = 2.0  # the timeline holds a clip's last frame (slow push-in) up to this long past its end
MIN_REAL = 0.5  # share of the planned runtime that must stay real footage
BREATH_S = 0.4  # ponytail: per-shot gap in the runtime estimate, mirrors timeline's pause; read it there if it drifts


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


class DraftMid(BaseModel):  # target up to 45 s: no how
    hooks: list[str] = Field(min_length=3, max_length=3, description="Three candidate opening lines.")
    hook_card_prompt: str
    problem: FootageDraft
    demos: list[FootageDraft] = Field(min_length=1, max_length=2)
    impact: FootageDraft
    ask: CardDraft


class DraftShort(BaseModel):  # target up to 30 s: no how, no impact
    hooks: list[str] = Field(min_length=3, max_length=3, description="Three candidate opening lines.")
    hook_card_prompt: str
    problem: FootageDraft
    demos: list[FootageDraft] = Field(min_length=1, max_length=1)
    ask: CardDraft


class ShotDraft(FootageDraft):  # a run with a flow: a middle shot may be generated instead of footage
    generated: bool = Field(description="True = a generated illustration instead of real footage.")
    flow_step: str = Field(description='The flow step id this shot covers, e.g. "s2" (not a shot id), or "".')
    card_prompt: str = Field(description="Generated shots: the picture to draw (no text, letters or logos). "
                             "Empty for footage.")


class DraftG(Draft):  # up to 2 more demo shots, for steps the take doesn't show well
    problem: ShotDraft
    demos: list[ShotDraft] = Field(min_length=2, max_length=6)
    how: ShotDraft
    impact: ShotDraft


class DraftMidG(DraftMid):
    problem: ShotDraft
    demos: list[ShotDraft] = Field(min_length=1, max_length=3)
    impact: ShotDraft


class DraftShortG(DraftShort):
    problem: ShotDraft
    demos: list[ShotDraft] = Field(min_length=1, max_length=2)


AnyDraft = Union[Draft, DraftMid, DraftShort]
WITH_FLOW = {Draft: DraftG, DraftMid: DraftMidG, DraftShort: DraftShortG}
DEMO_BEAT = {Draft: "2 to 4 demo shots", DraftMid: "1 to 2 demo shots", DraftShort: "1 demo shot",
             DraftG: "2 to 6 demo shots", DraftMidG: "1 to 3 demo shots", DraftShortG: "1 to 2 demo shots"}
DEMO_COUNT = {Draft: "3 to 4 shots (2 only if the recording has fewer usable moments)",
              DraftMid: "1 to 2 shots (2 only if the take has two strong moments)",
              DraftShort: "exactly 1 shot, the take's strongest moment",
              DraftG: "3 to 4 footage shots (2 only if the take has fewer wow / ok steps), plus 1 to 2 generated "
                      "shots for the most important weak or missing steps",
              DraftMidG: "1 to 2 footage shots, plus 1 generated shot for the most important weak or missing step",
              DraftShortG: "1 footage shot (the take's strongest moment), plus 1 generated shot only if a key "
                           "step is weak or missing"}
BEAT_LINES = {"how": "- how: one shot on how it works under the hood.\n",
              "impact": "- impact: one shot on what changes for the user.\n"}


def target(time_limit_s: int) -> tuple[int, int]:
    """Seconds and spoken words the script aims for under the brief's time limit."""
    s = min(TARGET_S, round(TARGET_SHARE * time_limit_s))
    return s, TARGET_WORDS if s == TARGET_S else round(TARGET_RATE * s)  # the longest keeps its 190 words


def draft_model(target_s: int, flow: bool = False) -> type[AnyDraft]:
    """The draft shape for the target; with a flow, the variant whose middle shots may be generated."""
    model = DraftShort if target_s <= 30 else DraftMid if target_s <= 45 else Draft
    return WITH_FLOW[model] if flow else model


def beats_of(model: type[AnyDraft]) -> list[str]:
    """The beats a draft model holds, in BUDGET order (``hooks`` -> hook, ``demos`` -> demo)."""
    return [b for b in BUDGET if b in model.model_fields or f"{b}s" in model.model_fields]


def budget(beats: list[str], total: int) -> dict[str, int]:
    """BUDGET for ``beats`` scaled to ``total`` words in the same proportions; rounding goes to demo."""
    kept = sum(BUDGET[b] for b in beats)
    out = {b: round(BUDGET[b] * total / kept) for b in beats}
    out["demo"] += total - sum(out.values())
    return out


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
    """Make ``line`` fit its clip: WORDS_PER_S words per second of clip plus HOLD_S. First grow
    the clip inside ``seg`` (end, then start), then drop whole sentences from the end down to
    the cap. Never cuts mid-sentence and always keeps the first full sentence."""
    tokens = line.split()
    need = len(tokens) / WORDS_PER_S - HOLD_S
    if need > end - start:
        end = min(seg.end, start + need)
        start = max(seg.start, end - need)
    cap = round(WORDS_PER_S * (end - start + HOLD_S))
    if len(tokens) <= cap:
        return start, end, line
    ends = [k + 1 for k, t in enumerate(tokens) if _ends_sentence(t)] or [len(tokens)]
    cut = max((k for k in ends if k <= cap), default=ends[0])
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


def _step_id(text: str) -> str:
    """'step s2', 'S2' -> 's2'."""
    return text.strip().lower().removeprefix("step").strip()


def _step_evidence(step: Optional[FlowStep]) -> list[Evidence]:
    return [Evidence(type="readme", ref=e) if e.startswith("README#") else Evidence(type="file", ref=e)
            for e in (step.evidence if step else [])]


def _quality(seg: Optional[Segment], moments: dict[str, Moment]) -> Optional[str]:
    m = moments.get(seg.moment_id or "") if seg else None
    return m.quality if m else None


def reference_frames(seg: Optional[Segment], step: str, segments: list[Segment], moments: dict[str, Moment],
                     flow: list[FlowStep]) -> list[str]:
    """Frames fal redraws for a generated shot: a weak moment's own segment; else a segment of the
    shot's flow step, or for a step the take never shows, of the nearest step it does show."""
    if _quality(seg, moments) == "weak":
        return list(seg.frames)
    shown = {}
    for g in segments:
        m = moments.get(g.moment_id or "")
        if m and m.flow_step and m.kind != "dead":
            shown.setdefault(m.flow_step, g)
    order = [f.step_id for f in flow]
    if step in order:
        near = sorted((t for t in order if t in shown), key=lambda t: abs(order.index(t) - order.index(step)))
        if near:
            return list(shown[near[0]].frames)
    return list(seg.frames) if seg is not None else []


def treatment(d: FootageDraft, beat: str, by_id: dict[str, Segment], moments: dict[str, Moment]
              ) -> tuple[bool, Optional[Segment]]:
    """(generated, segment) for a middle shot. Footage on a weak moment becomes generated; a generated
    problem or demo shot whose flow step a wow / ok moment shows becomes footage on that moment."""
    seg = by_id.get(d.segment_id)
    gen = getattr(d, "generated", False)
    step = _step_id(getattr(d, "flow_step", ""))
    if not gen and _quality(seg, moments) == "weak":
        return True, seg
    if gen and step and beat in ("problem", "demo"):
        good = [g for g in by_id.values() if _quality(g, moments) in ("wow", "ok")
                and moments[g.moment_id].flow_step == step and moments[g.moment_id].kind != "dead"]
        if good:
            return False, seg if seg in good else max(good, key=lambda g: _quality(g, moments) == "wow")
    return gen, seg


def build_shots(draft: AnyDraft, hook_line: str, segments: list[Segment], repo: RepoFiles,
                moments: Optional[Moments] = None, flow: list[FlowStep] = ()) -> list[Shot]:
    """Draft -> shots in beat order, footage inside real segments, claims ``pending``.

    Evidence follows the shot type: every claim of a footage shot carries its own segment; a
    generated shot's claims carry only README / file evidence (plus its flow step's)."""
    by_id = {s.segment_id: s for s in segments}
    by_moment = {m.moment_id: m for m in moments.moments} if moments else {}
    steps = {f.step_id: f for f in flow}
    real = {
        "footage": set(by_id),
        "readme": {f"README#{h}" for h in readme_headings(repo.readme)},
        "file": set(repo.tree),
    }
    beats: list[tuple[Any, Union[FootageDraft, CardDraft]]] = [
        ("hook", CardDraft(voice_line=hook_line, card_prompt=draft.hook_card_prompt, claims=[])),
        ("problem", draft.problem),
        *(("demo", d) for d in draft.demos),
        *((b, getattr(draft, b)) for b in ("how", "impact") if hasattr(draft, b)),
        ("ask", draft.ask),
    ]
    shots, n_claims = [], 0
    for i, (beat, d) in enumerate(beats, 1):
        own: list[Evidence] = []  # evidence every claim of this shot carries
        if isinstance(d, FootageDraft):
            gen, seg = treatment(d, beat, by_id, by_moment)
            step = steps.get(_step_id(getattr(d, "flow_step", "")))
            if gen:
                frames = reference_frames(seg, step.step_id if step else "", segments, by_moment, flow)
                prompt = getattr(d, "card_prompt", "").strip() or d.voice_line
                visual = ShotVisual(kind="card", card_prompt=prompt, reference_frames=frames)
                own = _step_evidence(step)
            else:
                if seg is None:
                    raise ValueError(f"{gemini.INCOMPLETE} [script: {beat} shot uses unknown segment {d.segment_id!r}]")
                if seg.segment_id != d.segment_id:  # moved onto the wow / ok moment of its step
                    d = d.model_copy(update={"segment_id": seg.segment_id, "source_start": seg.start,
                                             "source_end": seg.end})
                start, end, line = fit_voice(d.voice_line, seg, *_span(d, seg))
                if line != d.voice_line:
                    events.emit("script", f"Voice s{i:02d} ({beat}): trimmed {len(d.voice_line.split())} -> "
                                f"{len(line.split())} words to fit {end - start:.1f}s of {seg.segment_id}",
                                data={"before": d.voice_line, "after": line})
                    # a claim cut away (wholly or partly) goes; what's left of it is an unclaimed sentence below
                    d = d.model_copy(update={"voice_line": line,
                                             "claims": [c for c in d.claims if _locate(line.split(), c.text)]})
                visual = ShotVisual(kind="footage", segment_id=seg.segment_id, source_start=start, source_end=end)
                own = [Evidence(type="footage", ref=seg.segment_id)]
        else:
            visual = ShotVisual(kind="card", card_prompt=d.card_prompt)
        generated = visual.kind != "footage"
        drafts = list(d.claims)
        if beat in CHECKED_BEATS:
            # a factual sentence the draft didn't list still gets checked, against the shot's own evidence
            drafts += [ClaimDraft(text=t, evidence=[])
                       for t in unclaimed_sentences(d.voice_line, [c.text for c in d.claims])]
        claims = []
        for c in drafts:
            n_claims += 1
            evidence = [e for e in c.evidence if not (generated and e.type == "footage") and e.ref in real[e.type]]
            # footage: a demo claim (or one with no repo evidence) is about the screen, so it carries its
            # own segment; a README-backed problem / how / impact claim isn't judged on frames.
            if generated or beat == "demo" or not evidence:
                evidence += [e for e in own if e not in evidence and e.ref in real[e.type]]
            claims.append(Claim(claim_id=f"cl{n_claims}", text=c.text, evidence=evidence, status="pending"))
        shots.append(Shot(shot_id=f"s{i:02d}", beat=beat, voice_line=d.voice_line, visual=visual,
                          claims=claims, generated=generated))
    return shots


def planned_s(shot: Shot) -> float:
    """Rough runtime of a shot: the timeline plays each shot as long as its voice."""
    return len(shot.voice_line.split()) / TARGET_RATE + BREATH_S


def real_share(shots: list[Shot]) -> float:
    total = sum(map(planned_s, shots))
    return sum(planned_s(s) for s in shots if not s.generated) / total if total else 1.0


def cap_generated(shots: list[Shot]) -> list[Shot]:
    """Drop generated middle shots, last first, until real footage is at least MIN_REAL of the
    planned runtime. Hook and ask cards stay (they count as generated)."""
    out = list(shots)
    while real_share(out) < MIN_REAL:
        k = next((k for k in range(len(out) - 1, -1, -1)
                  if out[k].generated and out[k].beat not in ("hook", "ask")), None)
        if k is None:
            break
        events.emit("script", f"Dropped generated {out[k].shot_id} ({out[k].beat}): real footage "
                              f"{real_share(out):.0%} < {MIN_REAL:.0%}")
        out.pop(k)
    return out


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
            raise ValueError(f"{gemini.INCOMPLETE} [script: hook judge gave no full verdict for h{i}]")
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
    old = next((s for s in drafted if s.beat == "problem"), None)
    if old is None or any(s.beat == "problem" for s in shots):
        return shots
    shot = old.model_copy(update={"voice_line": problem, "claims": []})
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


def ordered_frames(seg: Segment, segments: dict[str, Segment], run_dir: Path) -> list[tuple[str, str]]:
    """``(label, frame)`` in time order: the last frame of the nearest segment ending at or before
    ``seg`` starts ("before"), ``seg``'s own frames ("during"), the first frame of the nearest
    segment starting at or after it ends ("after"). Segments overlap, so a neighbour must not.
    Only frames on disk; no own frames means nothing (the neighbours alone show no claim)."""
    def on_disk(s: Segment) -> list[str]:
        return [f for f in s.frames if (run_dir / f).exists()]

    during = on_disk(seg)
    if not during:
        return []
    before = [s for s in segments.values() if s.end <= seg.start and on_disk(s)]
    after = [s for s in segments.values() if s.start >= seg.end and on_disk(s)]
    out = [("before", on_disk(max(before, key=lambda s: s.end))[-1])] if before else []
    out += [("during", f) for f in during]
    out += [("after", on_disk(min(after, key=lambda s: s.start))[0])] if after else []
    return out


def evidence_shown(evidence: list[Evidence], run_dir: Path, segments: dict[str, Segment],
                   repo: RepoFiles, generated: bool = False) -> tuple[list[str], list[Path]]:
    """What the judge sees for a claim's evidence: text blocks and frame images.

    A file whose content ingest didn't keep, or a segment with no frames on disk, shows
    nothing and counts as no evidence. A generated shot shows no footage, so its claims are
    checked against README and files only.
    """
    files = {k.path: k.content for k in repo.key_files}
    blocks: list[str] = []
    images: list[Path] = []
    for e in evidence:
        if generated and e.type == "footage":
            continue
        if e.type == "readme":
            body = readme_section(repo.readme, e.ref.removeprefix("README#"))
            if body:
                blocks.append(f"README section:\n{body}")
        elif e.type == "file":
            if files.get(e.ref, "").strip():
                blocks.append(f"File {e.ref}:\n{files[e.ref]}")
        else:
            seg = segments.get(e.ref)
            frames = ordered_frames(seg, segments, run_dir) if seg else []
            if frames:
                listed = ", ".join(f"{label} {f}" for label, f in frames)
                blocks.append(f"Footage {seg.segment_id}, {seg.start:g}-{seg.end:g} s of the recording: "
                              f"{len(frames)} frames attached in time order ({listed})")
                images += [run_dir / f for _, f in frames]
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
        if m and m.quality:  # a run with a flow (A13)
            what += f", quality {m.quality}" + (f", shows step {m.flow_step}" if m.flow_step else "")
        cap = round(WORDS_PER_S * (s.end - s.start + HOLD_S))  # what fit_voice keeps at most
        lines.append(f"{s.segment_id} {s.start:g}-{s.end:g}s, {what}, score {best.get(s.segment_id, 0):.2f}, "
                     f"voice up to {cap} words")
    return "\n".join(lines)


def flow_block(summary: Summary, moments: Moments) -> str:
    """The flow and the steps the take never shows, for a run with a flow ("" without). The brief
    reaches the prompt through B's {brief_block}."""
    if not summary.flow:
        return ""
    gone = {s.step_id for s in missing_steps(summary, moments)}
    steps = "\n".join(f"- step {s.step_id}: {s.text}" + (" (MISSING: the take never shows it)" if s.step_id in gone else "")
                      + f" [evidence: {', '.join(s.evidence) or 'none'}]" for s in summary.flow)
    return f"User flow from the repo, in the order a user meets it (step ids, not shot ids):\n<flow>\n{steps}\n</flow>"


def draft_script(run: RunConfig, summary: Summary, segment_lines: str, repo: RepoFiles,
                 flow: str = "") -> AnyDraft:
    target_s, total = target(run.brief.time_limit_s)
    model = draft_model(target_s, bool(flow))
    beats = beats_of(model)
    footage = [b for b in beats if b not in ("hook", "ask")]
    text = gemini.prompt(
        "script_draft",
        language=run.brief.language,
        beats=", ".join(DEMO_BEAT[model] if b == "demo" else b for b in beats),
        target_s=str(target_s),
        words=str(total),
        footage=", ".join("demos" if b == "demo" else b for b in footage),
        demo_count=DEMO_COUNT[model],
        flow=flow or "No user flow is given: every problem, demo, how and impact shot is footage.",
        beat_lines="".join(BEAT_LINES.get(b, "") for b in footage),
        budget="\n".join(f"- {b}: {w} words" + (" across all demo shots" if b == "demo" else "")
                         for b, w in budget(beats, total).items()),
        criteria=", ".join(run.brief.criteria),
        summary=summary.model_dump_json(indent=2),
        segments=segment_lines,
        headings=json.dumps(sorted(readme_headings(repo.readme))),
        tree=json.dumps([k.path for k in repo.key_files]),  # only files the truth check can read
    )
    return gemini.generate(text, model, thinking="low")  # default or medium thinking took 130-160 s on a 1-min take


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
    if not segments:
        raise ValueError("Nothing in the recording could be used as footage. Record a longer take that "
                         "shows the app's main results clearly.")
    moments = load("moments.json", Moments)
    lines = _segment_lines(segments, moments, load("frame_scores.json", FrameScores))

    draft = draft_script(run, summary, lines, repo, flow_block(summary, moments))
    judgement = judge_hooks(summary, draft.hooks)
    hooks, verdict = score_hooks(draft.hooks, judgement, summary.name)
    chosen = next(h for h in hooks if h.hook_id == verdict.hook_id) if verdict else hooks[-1]
    drafted = cap_generated(build_shots(draft, chosen.text, segments, repo, moments, summary.flow))
    events.emit("script", "Shots: " + ", ".join(
        f"{s.shot_id} {s.beat} {'generated' if s.generated else 'footage'}" for s in drafted)
        + f"; real footage {real_share(drafted):.0%} of the plan")

    by_id = {s.segment_id: s for s in segments}
    claims = [(c, s.generated) for s in drafted for c in s.claims]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda cg: check_claim(cg[0], *evidence_shown(cg[0].evidence, run_dir, by_id, repo, cg[1])),
                                claims))
    shots = cap_generated(keep_problem(apply_checks(drafted, {c.claim_id: c for c, _ in results}), drafted,
                                       summary.problem))
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
    target_s, total = target(run.brief.time_limit_s)
    plan = budget(beats_of(draft_model(target_s, bool(summary.flow))), total)
    words_per = {b: sum(len(s.voice_line.split()) for s in shots if s.beat == b) for b in plan}
    over = [f"{b} {w}/{plan[b]}" for b, w in words_per.items() if w > plan[b]]
    events.emit("script", f"Script: {len(shots)} shots, {sum(words_per.values())}/{total} words (~{target_s} s)"
                + (f"; over budget: {', '.join(over)}" if over else ""), data={"words": words_per})


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("script")
