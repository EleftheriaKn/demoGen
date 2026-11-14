"""Stage `understand` (owner: A).

Repo summary + timestamped moments (Gemini).

Reads repo_files.json, recording, transcript.json. Writes summary.json, moments.json.

summary.json is real: Gemini reads the README, key files and transcript, then code keeps only
evidence that names a real README heading or repo file; features left with none are
dropped (logged as an event) and a summary with no features left raises.
moments.json is still copied from fixtures/run/ until ticket 2.

Run alone: python -m backend.pipeline.understand runs/<run_id>
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from backend import events
from backend.clients import gemini
from backend.contracts import Feature, RepoFiles, RunConfig, Summary, Transcript

FIXTURE_RUN = Path(__file__).resolve().parents[2] / "fixtures" / "run"

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


def run(run_dir: Path, run: RunConfig) -> None:
    repo = RepoFiles.model_validate_json((run_dir / "repo_files.json").read_text(encoding="utf-8"))
    transcript = Transcript.model_validate_json((run_dir / "transcript.json").read_text(encoding="utf-8"))
    summary = summarize(repo, transcript)
    (run_dir / "summary.json").write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    events.emit("understand", f"Summary: {summary.name}, {len(summary.features)} features")

    # ponytail: fixture moments until ticket 2 watches the recording.
    shutil.copy2(FIXTURE_RUN / "moments.json", run_dir / "moments.json")
    events.emit("understand", "moments.json copied from fixtures (not built yet)")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("understand")
