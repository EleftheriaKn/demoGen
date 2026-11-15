"""Stage `understand` (owner: A).

Repo summary + timestamped moments (Gemini).

Reads repo_files.json, recording, transcript.json. Writes summary.json, moments.json.

summary.json is real: Gemini reads the README, key files and transcript, then code keeps only
evidence that names a real README heading or repo file; features left with none are
dropped (logged as an event) and a summary with no features left raises.

moments.json is real: Gemini watches the recording (uploaded once per run, see
``gemini.upload``), then code clamps times to the recording length, sorts the moments, trims
overlaps, merges each action with the result right after it (so the moment ends once the result is on
screen), ends each result moment SETTLE_S after Gemini's settled_at, drops slivers, nulls unknown
feature ids and numbers them m1, m2, ...

Run alone: python -m backend.pipeline.understand runs/<run_id>
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.contracts import Feature, Moment, Moments, RepoFiles, RunConfig, Summary, Transcript
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
    """Keep evidence that resolves to a README heading or tree path; drop features left with none."""
    headings, tree = readme_headings(repo.readme), set(repo.tree)
    features: list[Feature] = []
    for f in summary.features:
        evidence = [e for e in f.evidence if evidence_ok(e, headings, tree)]
        if evidence:
            features.append(f.model_copy(update={"evidence": evidence}))
    if not features:
        raise ValueError("understand: no feature has evidence in the README or repo files")
    return summary.model_copy(update={"features": features})


def summarize(repo: RepoFiles, transcript: Transcript) -> Summary:
    key_files = "\n\n".join(f'<file path="{k.path}">\n{k.content}\n</file>' for k in repo.key_files)
    text = gemini.prompt(
        "understand_summary",
        readme=repo.readme,
        tree="\n".join(repo.tree),
        key_files=key_files,
        transcript=transcript.text,
    )
    raw = gemini.generate(text, Summary)
    summary = check_evidence(raw, repo)
    dropped = [f.name for f in raw.features if f.feature_id not in {k.feature_id for k in summary.features}]
    if dropped:
        events.emit("understand", f"Dropped features with no repo evidence: {', '.join(dropped)}")
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


class _RawMoments(BaseModel):
    moments: list[_RawMoment]


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
                                              "confidence": min(prev.confidence, r.confidence)})
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


def fix_moments(raw: list[_RawMoment], duration: float, feature_ids: set[str]) -> Moments:
    """Merge action+result, end results once settled, clamp to [0, duration], sort, trim each start to
    the previous end, drop slivers, renumber."""
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
            kind=r.kind, confidence=min(max(r.confidence, 0.0), 1.0),
        ))
        prev_end = end
    if not out:
        raise ValueError("understand: Gemini returned no usable moments")
    return Moments(moments=out)


def find_moments(video: Path, summary: Summary) -> Moments:
    duration = ff.duration(video)
    text = gemini.prompt(
        "understand_moments",
        app=summary.name,
        duration=f"{duration:.2f}",
        features="\n".join(f"- {f.feature_id}: {f.name}. {f.description}" for f in summary.features),
    )
    raw = gemini.generate(text, _RawMoments, video=video)
    moments = fix_moments(raw.moments, duration, {f.feature_id for f in summary.features})
    if len(moments.moments) < len(raw.moments):
        events.emit("understand", f"Dropped {len(raw.moments) - len(moments.moments)} moment(s) outside the recording or too short")
    return moments


def run(run_dir: Path, run: RunConfig) -> None:
    repo = RepoFiles.model_validate_json((run_dir / "repo_files.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate_json((run_dir / "transcript.json").read_text(encoding="utf-8"))
    summary = summarize(repo, transcript)
    (run_dir / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    events.emit("understand", f"Summary: {summary.name}, {len(summary.features)} features")

    moments = find_moments(run_dir / run.recording, summary)
    (run_dir / "moments.json").write_text(moments.model_dump_json(indent=2), encoding="utf-8")
    kinds = [m.kind for m in moments.moments]
    events.emit("understand", f"Moments: {len(kinds)} ({kinds.count('result')} result, {kinds.count('dead')} dead)")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("understand")
