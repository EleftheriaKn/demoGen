"""Stage understand: summary.json (ticket 1), moments.json (ticket 2), plus the Gemini client's replay."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from backend import events
from backend.clients import gemini
from backend.contracts import Feature, Moments, RepoFiles, Summary
from backend.pipeline import understand
from backend.run import FIXTURE_RUN, load_run

TAKE = Path(__file__).resolve().parents[1] / "samples" / "todomvc" / "take_1min.mp4"
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


def _raw(start, end, kind="action", feature_id=None, settled_at=None):
    return understand._RawMoment(start=start, end=end, description="d", feature_id=feature_id,
                                 kind=kind, settled_at=settled_at, confidence=1.2)


def test_fix_moments_clamps_sorts_trims_and_renumbers():
    raw = [_raw(20, 70, "dead"), _raw(-1, 5, feature_id="f9"), _raw(4, 12, feature_id="f1"),
           _raw(11.8, 12.1), _raw(80, 90)]
    out = understand.fix_moments(raw, duration=60.0, feature_ids={"f1"}).moments
    assert [(m.moment_id, m.start, m.end) for m in out] == [("m1", 0, 5), ("m2", 5, 12), ("m3", 20, 60)]
    assert [m.feature_id for m in out] == [None, "f1", None]
    assert out[0].confidence == 1.0
    with pytest.raises(ValueError, match="no usable"):
        understand.fix_moments([_raw(80, 90)], 60.0, set())


def test_fix_moments_ends_an_action_when_its_result_is_on_screen():
    raw = [_raw(12, 14, "action", "f1"), _raw(14.2, 15, "result", "f1"), _raw(15, 20, "action", "f1"),
           _raw(22, 23, "result", "f1")]  # last result is 2 s after its action: not merged
    out = understand.fix_moments(raw, 60.0, {"f1"}).moments
    assert [(m.start, m.end, m.kind) for m in out] == [(12, 14.9, "result"), (15, 20, "action"), (22, 23, "result")]


def test_fix_moments_ends_a_result_just_after_it_settles():
    raw = [_raw(12, 14.5, "result", "f1", settled_at=14.8),  # Gemini's end is early: pushed out
           _raw(14.5, 22, "result", "f1", settled_at=20),    # late end: pulled in
           _raw(22, 24, "result", "f1"), _raw(24, 26, "action", settled_at=25)]  # no settled_at / not a result
    out = understand.fix_moments(raw, 60.0, {"f1"}).moments
    assert [(m.start, m.end) for m in out] == [(12, 15.5), (15.5, 20.7), (22, 24), (24, 26)]


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


def test_understand_writes_checked_summary_and_moments(tmp_path):
    for name in ("run.json", "repo_files.json", "transcript.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    shutil.copy2(TAKE, tmp_path / "recording.mp4")
    cfg = load_run(tmp_path)
    events.bind(tmp_path, cfg.run_id)
    understand.run(tmp_path, cfg)

    summary = Summary.model_validate_json((tmp_path / "summary.json").read_text(encoding="utf-8"))
    moments = Moments.model_validate_json((tmp_path / "moments.json").read_text(encoding="utf-8")).moments
    assert summary.features
    headings, tree = understand.readme_headings(REPO.readme), set(REPO.tree)
    for f in summary.features:
        assert f.evidence
        for e in f.evidence:
            assert understand.evidence_ok(e, headings, tree), e

    # TodoMVC take: typing todos ~12-30 s (samples/todomvc/notes.md); on-screen text is copied.
    assert moments and moments[-1].end <= 60.1
    assert all(a.end <= b.start for a, b in zip(moments, moments[1:]))
    assert any(m.kind in ("action", "result") and m.start < 30 and m.end > 12 for m in moments)
    assert any("Buy milk" in m.description for m in moments)
