"""Stage `understand` (owner: A).

Repo summary + timestamped moments (Gemini).

Reads repo_files.json, recording, transcript.json. Writes summary.json, moments.json.

summary.json is real: Gemini reads the README, key files, transcript and the pasted brief, and writes the
features plus the flow (the steps a user goes through, in order). Code keeps only evidence that names a
real README heading or repo file; features and flow steps left with none are dropped (logged as an event)
and a summary with no features left raises.

moments.json is real: Gemini watches the recording (uploaded once per run, see
``gemini.upload``), then code clamps times to the recording length, sorts the moments, trims
overlaps, merges each action with the result right after it (so the moment ends once the result is on
screen), ends each result moment SETTLE_S after Gemini's settled_at, drops slivers, nulls unknown
feature ids and numbers them m1, m2, ... Each moment has a quality (wow / ok / weak) and the flow step it
shows (unknown ids become null); flow steps no moment shows are "missing", listed in one event.

Run alone: python -m backend.pipeline.understand runs/<run_id>
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.contracts import Feature, FlowStep, Moment, Moments, RepoFiles, RunConfig, Summary, Transcript
from backend.pipeline import ff

MIN_MOMENT_S = 0.5  # shorter after clamping/trimming = dropped
MERGE_GAP_S = 0.5  # an action and a result this close are one moment
SETTLE_S = 0.7  # a result moment ends this long after its result appears (candidates' last frame: end - 0.2 s)

HEADING = re.compile(r"^#+\s+(.+?)(?:\s+#+)?\s*$")


def readme_headings(readme: str) -> set[str]:
    """Markdown ATX headings, ignoring lines inside ``` fences."""
    headings, fenced = set(), False
    for line in readme.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)):
            headings.add(m.group(1))
    return headings


def evidence_ok(ref: str, headings: set[str], tree: set[str]) -> bool:
    """'README#<heading>' must name a real heading; anything else must be a repo path."""
    return ref.removeprefix("README#") in headings if ref.startswith("README#") else ref in tree


def check_evidence(summary: Summary, repo: RepoFiles) -> Summary:
    """Keep evidence that resolves to a README heading or tree path; drop features and flow steps left
    with none. A flow step naming a dropped or unknown feature gets feature_id null."""
    headings, tree = readme_headings(repo.readme), set(repo.tree)

    def backed(items: list) -> list:
        out = []
        for x in items:
            evidence = [e for e in x.evidence if evidence_ok(e, headings, tree)]
            if evidence:
                out.append(x.model_copy(update={"evidence": evidence}))
        return out

    features: list[Feature] = backed(summary.features)
    if not features:
        raise ValueError("Couldn't match what the app does to its repo. Check the repo URL is the app in the "
                         "recording, and that its README describes the features.")
    ids = {f.feature_id for f in features}
    flow = [s.model_copy(update={"feature_id": s.feature_id if s.feature_id in ids else None})
            for s in backed(summary.flow)]
    return summary.model_copy(update={"features": features, "flow": flow})


class _RawFlowStep(BaseModel):
    """A flow step as Gemini returns it: every field required, so none is left out."""

    step_id: str = Field(description="'s1', 's2', ... in the order the user meets them.")
    text: str = Field(description="What the user does or sees at this step, in one short sentence.")
    feature_id: Optional[str] = Field(description="Feature this step uses, or null.")
    evidence: list[str] = Field(description="'README#<heading>' or a repo path from the file tree.")


class _RawSummary(BaseModel):
    """Summary as Gemini returns it; flow is required here (optional in the contract, for old runs)."""

    name: str
    one_liner: str
    problem: str
    audience: str
    features: list[Feature]
    flow: list[_RawFlowStep]


def brief_text(run_dir: Path, run: RunConfig) -> str:
    """The pasted brief: run.brief.text once C adds it (C14), else brief.txt, else ''."""
    if text := getattr(run.brief, "text", None):
        return text
    path = run_dir / "brief.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def summarize(repo: RepoFiles, transcript: Transcript, brief: str = "") -> Summary:
    key_files = "\n\n".join(f'<file path="{k.path}">\n{k.content}\n</file>' for k in repo.key_files)
    text = gemini.prompt(
        "understand_summary",
        readme=repo.readme,
        tree="\n".join(repo.tree),
        key_files=key_files,
        transcript=transcript.text,
        brief=brief or "(none)",
    )
    raw = Summary.model_validate(gemini.generate(text, _RawSummary).model_dump())
    summary = check_evidence(raw, repo)
    dropped = [f.name for f in raw.features if f.feature_id not in {k.feature_id for k in summary.features}]
    if dropped:
        events.emit("understand", f"Dropped features with no repo evidence: {', '.join(dropped)}")
    kept = {s.step_id for s in summary.flow}
    if dropped_steps := [s.text for s in raw.flow if s.step_id not in kept]:
        events.emit("understand", f"Dropped flow steps with no repo evidence: {'; '.join(dropped_steps)}")
    return summary


class _RawMoment(BaseModel):
    """What Gemini returns; code checks it into a Moment (ids, spans) in fix_moments."""

    start: float = Field(description="Start, seconds from the start of the recording.")
    end: float = Field(description="End, seconds from the start of the recording.")
    description: str = Field(description="What is on screen at the end time; on-screen text copied word for word.")
    feature_id: Optional[str] = Field(description="Feature shown, or null.")
    kind: Literal["result", "action", "setup", "dead"]
    settled_at: Optional[float] = Field(
        description="result only: seconds when the outcome is fully on screen and settled; null otherwise.")
    confidence: float = Field(description="0-1.")
    quality: Literal["wow", "ok", "weak"] = Field(
        description="wow = the payoff is clear on screen and settled; ok; weak = too fast, messy or cut off.")
    flow_step: Optional[str] = Field(description="Flow step shown (step_id), or null.")


class _WireMoment(BaseModel):
    """A moment exactly as Gemini writes it. Times are MM:SS strings: asked for seconds as a number,
    Gemini wrote video time 1:13 as 113 (Coach Platform take, Nov 15), so every moment past the first
    minute landed late and the ones past ~3:00 fell off the end. ``raw()`` turns it into a _RawMoment."""

    start: str = Field(description='Start as MM:SS.s from the start of the recording, e.g. "01:13.5".')
    end: str = Field(description='End as MM:SS.s from the start of the recording, e.g. "01:20.0".')
    description: str = Field(description="What is on screen at the end time; on-screen text copied word for word.")
    feature_id: Optional[str] = Field(description="Feature shown, or null.")
    kind: Literal["result", "action", "setup", "dead"]
    settled_at: Optional[str] = Field(
        description="result only: MM:SS.s when the outcome is fully on screen and settled; null otherwise.")
    confidence: float = Field(description="0-1.")
    quality: Literal["wow", "ok", "weak"] = Field(
        description="wow = the payoff is clear on screen and settled; ok; weak = too fast, messy or cut off.")
    flow_step: Optional[str] = Field(description="Flow step shown (step_id), or null.")

    def raw(self) -> Optional[_RawMoment]:
        """Times in seconds; None if a time can't be read."""
        try:
            times = {k: seconds(getattr(self, k)) for k in ("start", "end")}
            times["settled_at"] = seconds(self.settled_at) if self.settled_at else None
        except ValueError:
            return None
        return _RawMoment(**{**self.model_dump(), **times})


class _RawMoments(BaseModel):
    moments: list[_WireMoment]


def seconds(stamp: str) -> float:
    """'MM:SS(.s)' or 'H:MM:SS(.s)' -> seconds. A bare number is read as seconds. Bad input raises ValueError."""
    parts = stamp.strip().split(":")
    if len(parts) > 3 or not all(p.strip() for p in parts):
        raise ValueError(f"not a time: {stamp!r}")
    return sum(float(p) * 60 ** k for k, p in enumerate(reversed(parts)))


def mmss(t: float) -> str:
    """Seconds -> 'MM:SS.s' (how the moments prompt states times)."""
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def merge_results(raw: list[_RawMoment]) -> list[_RawMoment]:
    """An action followed by its result becomes one result moment ending when the result is on screen.

    Candidate frames sit inside the moment (up to 75% of it), so a moment that stops when the action
    does would show no result.
    """
    out: list[_RawMoment] = []
    for r in sorted(raw, key=lambda r: (r.start, r.end)):
        prev = out[-1] if out else None
        if (prev and prev.kind == "action" and r.kind == "result" and r.start - prev.end <= MERGE_GAP_S
                and r.feature_id in (prev.feature_id, None)):
            out[-1] = prev.model_copy(update={"end": max(prev.end, r.end), "kind": "result",
                                              "settled_at": r.settled_at if r.settled_at is not None else r.start,
                                              "description": f"{prev.description} {r.description}",
                                              "confidence": min(prev.confidence, r.confidence),
                                              "quality": r.quality,  # the payoff's quality
                                              "flow_step": r.flow_step or prev.flow_step})
        else:
            out.append(r)
    return out


def result_end(r: _RawMoment) -> float:
    """A result moment ends SETTLE_S after its result appears (settled_at), so its last frame shows the
    settled screen its description names. Gemini's own end is often a beat early (TodoMVC: text still
    being typed, the previous filter still highlighted)."""
    if r.kind == "result" and r.settled_at is not None and r.settled_at > r.start:
        return r.settled_at + SETTLE_S
    return r.end


def fix_moments(raw: list[_RawMoment], duration: float, feature_ids: set[str],
                flow_ids: set[str] = frozenset()) -> Moments:
    """Merge action+result, end results once settled, clamp to [0, duration], sort, trim each start to
    the previous end, drop slivers, null unknown feature / flow step ids, renumber."""
    out: list[Moment] = []
    prev_end = 0.0
    for r in merge_results(raw):
        start = round(max(r.start, prev_end, 0.0), 2)
        end = round(min(result_end(r), duration), 2)
        if end - start < MIN_MOMENT_S:
            continue
        out.append(Moment(
            moment_id=f"m{len(out) + 1}", start=start, end=end, description=r.description,
            feature_id=r.feature_id if r.feature_id in feature_ids else None,
            kind=r.kind, confidence=min(max(r.confidence, 0.0), 1.0), quality=r.quality,
            flow_step=r.flow_step if r.flow_step in flow_ids else None,
        ))
        prev_end = end
    if not out:
        raise ValueError("The recording doesn't show clear steps in the app. Record a 1–4 minute take of the "
                         "app in use, pausing about a second after each result.")
    return Moments(moments=out)


def missing_steps(summary: Summary, moments: Moments) -> list[FlowStep]:
    """Flow steps no moment shows (derived, not stored)."""
    shown = {m.flow_step for m in moments.moments}
    return [s for s in summary.flow if s.step_id not in shown]


def find_moments(video: Path, summary: Summary) -> Moments:
    duration = ff.duration(video)
    text = gemini.prompt(
        "understand_moments",
        app=summary.name,
        duration=mmss(duration),
        features="\n".join(f"- {f.feature_id}: {f.name}. {f.description}" for f in summary.features),
        flow="\n".join(f"- {s.step_id}: {s.text}" for s in summary.flow) or "(none)",
    )
    wire = gemini.generate(text, _RawMoments, video=video).moments
    raw = [r for r in (w.raw() for w in wire) if r is not None]
    if len(raw) < len(wire):
        events.emit("understand", f"Dropped {len(wire) - len(raw)} moment(s) with a time that isn't MM:SS")
    moments = fix_moments(raw, duration, {f.feature_id for f in summary.features},
                          {s.step_id for s in summary.flow})
    if len(moments.moments) < len(raw):
        events.emit("understand", f"Dropped {len(raw) - len(moments.moments)} moment(s) outside the recording or too short")
    if missing := missing_steps(summary, moments):
        events.emit("understand", "Flow steps the take never shows: "
                                  + "; ".join(f"{s.step_id} {s.text}" for s in missing))
    return moments


def run(run_dir: Path, run: RunConfig) -> None:
    repo = RepoFiles.model_validate_json((run_dir / "repo_files.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate_json((run_dir / "transcript.json").read_text(encoding="utf-8"))
    summary = summarize(repo, transcript, brief_text(run_dir, run))
    (run_dir / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    events.emit("understand", f"Summary: {summary.name}, {len(summary.features)} features, "
                              f"{len(summary.flow)} flow steps")

    moments = find_moments(run_dir / run.recording, summary)
    (run_dir / "moments.json").write_text(moments.model_dump_json(indent=2), encoding="utf-8")
    kinds, quality = [m.kind for m in moments.moments], [m.quality for m in moments.moments]
    events.emit("understand", f"Moments: {len(kinds)} ({kinds.count('result')} result, {kinds.count('dead')} dead; "
                              f"{quality.count('wow')} wow, {quality.count('weak')} weak)")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("understand")
