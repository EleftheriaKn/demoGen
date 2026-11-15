"""append_decision: the one way stages write to decisions.json."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.contracts import Decisions
from backend.decisions import append_decision, drop_stage

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "run" / "decisions.json"
QS = ["Is it sharp?", "Is it on-brief?"]


def _args(**over):
    args = dict(
        stage="judge_frames",
        kind="frame_pick",
        subject="c01",
        options=["frames/a.jpg", "frames/b.jpg"],
        chosen="frames/b.jpg",
        questions=QS,
        answers=[{"question": q, "score": 0.8, "reason": "ok"} for q in QS],
        judge_score=0.8,
        reason="b is sharper",
    )
    args.update(over)
    return args


def _read(run_dir: Path) -> Decisions:
    return Decisions.model_validate_json((run_dir / "decisions.json").read_text())


def test_creates_file_when_missing(tmp_path):
    d = append_decision(tmp_path, **_args())
    assert d.decision_id == "d01"
    out = _read(tmp_path)
    assert [x.decision_id for x in out.decisions] == ["d01"]
    assert out.rubric == [] and out.recut is None


def test_appends_with_next_id_and_keeps_existing(tmp_path):
    shutil.copy(FIXTURE, tmp_path / "decisions.json")
    before = _read(tmp_path)
    d = append_decision(tmp_path, **_args(heuristic_score=0.6))
    assert d.decision_id == "d10"
    after = _read(tmp_path)
    assert after.decisions[:-1] == before.decisions
    assert after.rubric == before.rubric
    assert after.decisions[-1] == d and d.heuristic_score == 0.6


def test_ids_keep_counting_up(tmp_path):
    ids = [append_decision(tmp_path, **_args()).decision_id for _ in range(3)]
    assert ids == ["d01", "d02", "d03"]


def test_rejects_chosen_not_in_options(tmp_path):
    with pytest.raises(ValueError, match="not in options"):
        append_decision(tmp_path, **_args(chosen="frames/zzz.jpg"))
    assert not (tmp_path / "decisions.json").exists()


@pytest.mark.parametrize(
    "answers",
    [
        [{"question": q, "score": 0.5, "reason": "x"} for q in reversed(QS)],  # wrong order
        [{"question": QS[0], "score": 0.5, "reason": "x"}],  # missing one
    ],
)
def test_rejects_answers_out_of_question_order(tmp_path, answers):
    with pytest.raises(ValueError, match="question order"):
        append_decision(tmp_path, **_args(answers=answers))
    assert not (tmp_path / "decisions.json").exists()


def test_failed_append_leaves_file_untouched(tmp_path):
    shutil.copy(FIXTURE, tmp_path / "decisions.json")
    before = (tmp_path / "decisions.json").read_text()
    with pytest.raises(ValueError):
        append_decision(tmp_path, **_args(judge_score=1.5))
    assert (tmp_path / "decisions.json").read_text() == before


def test_drop_stage_removes_only_that_stage(tmp_path):
    drop_stage(tmp_path, "script")  # no file: nothing to do
    assert not (tmp_path / "decisions.json").exists()
    shutil.copy2(FIXTURE, tmp_path / "decisions.json")
    before = _read(tmp_path)
    drop_stage(tmp_path, "script")
    after = _read(tmp_path)
    assert after.decisions == [d for d in before.decisions if d.stage != "script"]
    assert after.rubric == before.rubric and after.recut == before.recut
