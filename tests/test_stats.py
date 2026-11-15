"""C9: pitch numbers come out of a run's own files."""

from __future__ import annotations

import json

import pytest

from backend import run as orchestrator
from backend import stats


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    d = orchestrator.new_run_from_fixture()
    orchestrator.run_pipeline(d)
    return d


def test_numbers_from_fixture_run(run_dir):
    n = stats.collect(run_dir)
    assert n["status"] == "done" and n["wall_clock_s"] >= 0 and not n["resumed"]
    assert n["faked_stages"]                                     # fake run: flagged, never pitched
    assert n["claims"] == {"made": 4, "backed": 2, "rewritten": 1, "dropped": 1, "pending": 0}
    assert {e["claim"] for e in n["rejected_examples"]} == {"in seconds", "works fully offline"}
    assert any("offline" in e["why"] for e in n["rejected_examples"])
    assert n["real_footage_share"] == round(11 / 15, 3) and n["shots"] == 3
    assert n["hook"] == "What if your fridge could tell you what's for dinner?" and n["hooks_considered"] == 3
    assert set(n["stage_s"]) == set(orchestrator.STAGES)


def test_markdown_and_cli(run_dir, capsys):
    stats.main([str(run_dir)])
    md = capsys.readouterr().out
    assert "| Claims made / backed / rewritten / rejected | 4 / 2 / 1 / 1 |" in md
    assert "FAKE stages" in md and "works fully offline" in md and "73%" in md
    stats.main([str(run_dir), "--json"])
    assert json.loads(capsys.readouterr().out)["claims"]["made"] == 4


def test_resumed_run_is_flagged(run_dir):
    (run_dir / "timeline.json").unlink()
    orchestrator.run_pipeline(run_dir)
    assert stats.collect(run_dir)["resumed"] is True


def test_not_a_run(tmp_path):
    with pytest.raises(SystemExit):
        stats.main([str(tmp_path)])
