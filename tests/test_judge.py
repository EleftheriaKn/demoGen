"""Stage judge_frames (ticket A3): fixed questions, judge 80 / sharpness 20, one frame_pick per segment."""

from __future__ import annotations

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
