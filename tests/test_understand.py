"""Stage understand: summary.json from the repo (ticket A1), plus the Gemini client's replay."""

from __future__ import annotations

import shutil

import pytest
from pydantic import BaseModel

from backend import events
from backend.clients import gemini
from backend.contracts import Feature, Moments, RepoFiles, Summary
from backend.pipeline import understand
from backend.run import FIXTURE_RUN, load_run

REPO = RepoFiles.model_validate_json((FIXTURE_RUN / "repo_files.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Evidence check (pure)
# --------------------------------------------------------------------------- #


def test_readme_headings_skip_code_fences():
    readme = "# App\n\n## How it works ##\n\n```\n# not a heading\n```\n\n### Run it  \n"
    assert understand.readme_headings(readme) == {"App", "How it works", "Run it"}


def _summary(*features: Feature) -> Summary:
    return Summary(name="X", one_liner="x", problem="x", audience="x", features=list(features))


def test_check_evidence_drops_unknown_refs_and_unbacked_features():
    s = _summary(
        Feature(feature_id="a", name="Scan", description="d",
                evidence=["README#How it works", "README#Nope", "backend/vision.py", "backend/ghost.py"]),
        Feature(feature_id="b", name="Ghost", description="d", evidence=["README#Pricing", "src/x.py"]),
        Feature(feature_id="c", name="List", description="d", evidence=["frontend/src/ShoppingList.tsx"]),
    )
    out = understand.check_evidence(s, REPO)
    assert [f.feature_id for f in out.features] == ["a", "c"]
    assert out.features[0].evidence == ["README#How it works", "backend/vision.py"]
    with pytest.raises(ValueError, match="no feature"):
        understand.check_evidence(_summary(s.features[1]), REPO)


# --------------------------------------------------------------------------- #
# Gemini client replay
# --------------------------------------------------------------------------- #


class Echo(BaseModel):
    text: str


def test_replay_hit_and_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini, "RECORDINGS", tmp_path)
    monkeypatch.setattr(gemini, "LIVE", False)
    with pytest.raises(gemini.MissingRecording, match="--live"):
        gemini.generate("say hi", Echo)
    gemini.recording_path("say hi", Echo).write_text('{"text": "hi"}')
    assert gemini.generate("say hi", Echo) == Echo(text="hi")


# --------------------------------------------------------------------------- #
# Stage (replays tests/recordings/)
# --------------------------------------------------------------------------- #


def test_understand_writes_checked_summary(tmp_path):
    for name in ("run.json", "repo_files.json", "transcript.json", "recording.mp4"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    cfg = load_run(tmp_path)
    events.bind(tmp_path, cfg.run_id)
    understand.run(tmp_path, cfg)

    summary = Summary.model_validate_json((tmp_path / "summary.json").read_text(encoding="utf-8"))
    Moments.model_validate_json((tmp_path / "moments.json").read_text(encoding="utf-8"))
    assert summary.features
    headings, tree = understand.readme_headings(REPO.readme), set(REPO.tree)
    for f in summary.features:
        assert f.evidence
        for e in f.evidence:
            assert understand.evidence_ok(e, headings, tree), e
