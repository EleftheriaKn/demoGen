"""Stage judge_frames (ticket A3): fixed questions, judge 80 / sharpness 20, one frame_pick per segment."""

from __future__ import annotations

import json
import shutil

from PIL import Image, ImageDraw, ImageFilter

from backend import events
from backend.contracts import Candidates, Decisions, FrameScores
from backend.pipeline import judge
from backend.run import FIXTURE_RUN, load_run


def test_questions_are_fixed_per_criterion_with_generic_fallback():
    qs = judge.questions_for(["usefulness", "Craft", "vibes"])
    assert qs[:2] == [judge.QUESTIONS["usefulness"], judge.QUESTIONS["craft"]]
    assert "vibes" in qs[2]
    assert judge.questions_for(["usefulness", " Usefulness "]) == [judge.QUESTIONS["usefulness"]]
    assert len(judge.questions_for(["Vibes", "vibes"])) == 1


def test_total_is_judge_80_sharpness_20():
    # fixture d01: answers .75/.85/.65, sharpness .7 -> .74
    assert judge.combine([0.75, 0.85, 0.65], 0.7) == 0.74


def test_sharpness_ranks_blurred_frame_lower(tmp_path):
    img = Image.new("L", (640, 360), 255)
    d = ImageDraw.Draw(img)
    for x in range(0, 640, 20):
        d.line([(x, 0), (x, 360)], fill=0, width=2)
    sharp, blurry = tmp_path / "sharp.jpg", tmp_path / "blurry.jpg"
    img.save(sharp)
    img.filter(ImageFilter.GaussianBlur(6)).save(blurry)
    s = judge.sharpness([sharp, blurry])
    assert s[0] == 1.0 and s[1] < 0.5


def test_cheap_mode_judges_the_two_sharpest_frames():
    frames = ["frames/c01_1s.jpg", "frames/c01_2s.jpg", "frames/c01_3s.jpg"]
    sharp = {"frames/c01_1s.jpg": 0.2, "frames/c01_2s.jpg": 0.9, "frames/c01_3s.jpg": 0.5}
    assert judge.frames_to_judge(frames, sharp, "cheap") == ["frames/c01_2s.jpg", "frames/c01_3s.jpg"]
    assert judge.frames_to_judge(frames, sharp, "full") == frames


def test_judge_frames_stage(tmp_path):
    """Replays tests/recordings/. Re-running replaces this stage's decisions instead of duplicating."""
    for name in ("run.json", "candidates.json", "summary.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    shutil.copytree(FIXTURE_RUN / "frames", tmp_path / "frames")
    cfg = load_run(tmp_path)
    events.bind(tmp_path, cfg.run_id)

    judge.run_frames(tmp_path, cfg)
    judge.run_frames(tmp_path, cfg)

    cands = Candidates.model_validate_json((tmp_path / "candidates.json").read_text())
    scores = FrameScores.model_validate_json((tmp_path / "frame_scores.json").read_text())
    log = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    assert scores.questions == judge.questions_for(cfg.brief.criteria)
    assert len(scores.scores) == 2 * len(cands.segments)  # cheap mode
    picks = [d for d in log.decisions if d.kind == "frame_pick"]
    assert [d.subject for d in picks] == [s.segment_id for s in cands.segments]
    assert [d.options for d in picks] == [s.frames for s in cands.segments]
    by_frame = {s.frame: s for s in scores.scores}
    for d in picks:
        best = max((f for f in d.options if f in by_frame), key=lambda f: by_frame[f].total)
        assert d.chosen == best and d.judge_score == by_frame[best].total
        assert d.answers == by_frame[best].answers


# --------------------------------------------------------------------------- #
# Rubric (ticket A7 rubric / runbook item 7)
# --------------------------------------------------------------------------- #

from backend import run as orchestrator  # noqa: E402
from backend.contracts import Script, Timeline  # noqa: E402


def _fixture_cut(durations: list[float]) -> tuple[Timeline, Script]:
    """Fixture timeline (s01 hook, s02 problem, s03 demo) with these shot lengths."""
    tl = Timeline.model_validate_json((FIXTURE_RUN / "timeline.json").read_text())
    t, shots = 0.0, []
    for s, d in zip(tl.shots, durations):
        shots.append(s.model_copy(update={"start": t, "duration_s": d}))
        t += d
    tl = Timeline.model_validate({**tl.model_dump(), "shots": [s.model_dump() for s in shots], "duration_s": t})
    return tl, Script.model_validate_json((FIXTURE_RUN / "script.json").read_text())


def test_time_limit_check():
    tl, _ = _fixture_cut([4, 5, 6])
    item, shot = judge.check_time_limit(tl, 15)
    assert item.passed and item.question == "Under the 15 s time limit?" and shot is None
    item, shot = judge.check_time_limit(tl, 14)
    assert not item.passed and shot is None  # too long is the whole video's fault: never a recut


def test_problem_by_20_check():
    item, shot = judge.check_problem_by_20(*_fixture_cut([4, 5, 6]))
    assert item.passed and shot is None and "0:04" in item.reason
    item, shot = judge.check_problem_by_20(*_fixture_cut([20, 5, 6]))
    assert item.passed  # starting exactly at 0:20 is in time
    item, shot = judge.check_problem_by_20(*_fixture_cut([25, 5, 6]))
    assert not item.passed and shot == "s01"  # only the hook is before it: recut the hook
    tl, script = _fixture_cut([4, 5, 6])
    script.shots[1].beat = "demo"
    item, shot = judge.check_problem_by_20(tl, script)
    assert not item.passed and shot is None


def _rubric_dir(tmp_path, mode: str):
    for name in ("script.json", "timeline.json", "decisions.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    cfg = load_run(FIXTURE_RUN).model_copy(update={"mode": mode})
    (tmp_path / "run.json").write_text(cfg.model_dump_json())
    events.bind(tmp_path, cfg.run_id)
    return cfg


def test_rubric_cheap_mode_skips_gemini_checks(tmp_path):
    cfg = _rubric_dir(tmp_path, "cheap")
    judge.run_rubric(tmp_path, cfg)
    log = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    assert [r.question for r in log.rubric] == [
        judge.Q_PROBLEM, "Under the 180 s time limit?", judge.Q_CLAIMS, judge.Q_RESULT]
    assert [r.reason for r in log.rubric[2:]] == [judge.SKIPPED] * 2 and log.recut is None


def test_rubric_full_mode_gemini_checks(tmp_path):
    """Replays tests/recordings/ (`pytest --live` to refresh). Re-running replaces the rubric decision."""
    cfg = _rubric_dir(tmp_path, "full")
    before = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    judge.run_rubric(tmp_path, cfg)
    judge.run_rubric(tmp_path, cfg)
    log = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    assert len(log.rubric) == 4 and all(r.reason != judge.SKIPPED for r in log.rubric)
    rubric = [d for d in log.decisions if d.stage == "rubric"]
    assert len(rubric) == 1 and [a.question for a in rubric[0].answers] == [r.question for r in log.rubric]
    assert len(log.decisions) == len([d for d in before.decisions if d.stage != "rubric"]) + 1
    assert all(r.passed for r in log.rubric) and log.recut is None  # the fixture cut is fine


def test_one_shot_failure_reruns_timeline_once(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    run_dir = orchestrator.new_run_from_fixture()  # cheap mode: the code checks only
    monkeypatch.setenv("DEMOCUT_FAKE", ",".join(s for s in orchestrator.STAGES if s != "rubric"))
    copy = orchestrator.copy_fixture_outputs

    def late_problem(stage, run_dir):  # every timeline (first and recut) has a 25 s hook
        copy(stage, run_dir)
        if stage == "timeline":
            (run_dir / "timeline.json").write_text(_fixture_cut([25, 5, 6])[0].model_dump_json())

    monkeypatch.setattr(orchestrator, "copy_fixture_outputs", late_problem)
    orchestrator.run_pipeline(run_dir)
    msgs = [json.loads(l)["msg"] for l in (run_dir / "events.jsonl").read_text().splitlines()]
    assert msgs.count("timeline started (fake)") == 2
    assert any(m.startswith("Recut s01:") for m in msgs) and msgs[-1] == "Run done"
