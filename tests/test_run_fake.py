"""Hour-0 smoke tests: the fake pipeline runs end to end, and the pick_best stub keeps its shape."""

from __future__ import annotations

import shutil

import pytest

from backend import run as orchestrator
from backend.contracts import STAGES, Decision, Event
from backend.pipeline.judge import pick_best


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    d = orchestrator.new_run_from_fixture()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_fake_all_end_to_end(run_dir, monkeypatch):
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    final = orchestrator.run_pipeline(run_dir)
    assert final.exists() and final.stat().st_size > 0
    evs = [Event.model_validate_json(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
    assert evs[0].msg == "Run started" and evs[-1].progress == 1.0
    assert [e.progress for e in evs] == sorted(e.progress for e in evs)
    assert {e.stage for e in evs} == set(STAGES) | {"run"}


def test_unfaked_stub_fails_loudly(run_dir, monkeypatch):
    monkeypatch.setenv("DEMOCUT_FAKE", "ingest")  # ingest is real now (B4) and would clone; the next stub must fail
    with pytest.raises(NotImplementedError, match="DEMOCUT_FAKE"):
        orchestrator.run_pipeline(run_dir)


def test_unknown_fake_stage_rejected(monkeypatch):
    monkeypatch.setenv("DEMOCUT_FAKE", "understnad")
    with pytest.raises(SystemExit):
        orchestrator.fake_stages()


def test_pick_best_stub_shape():
    drafts = [f"cards/drafts/s01_{i}.png" for i in range(4)]
    qs = ["Is the title legible at phone size?", "Does it match the app's look?"]
    r = pick_best(drafts, qs)
    assert r["index"] == 0 and len(r["scores"]) == 4 and r["judge_score"] == r["scores"][r["index"]]
    # The caller (assets) can build a take_pick Decision straight from it.
    Decision(
        decision_id="d99", stage="assets", kind="take_pick", subject="s01", options=drafts,
        chosen=drafts[r["index"]], answers=r["answers"], judge_score=r["judge_score"], reason=r["reason"],
    )
