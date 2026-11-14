"""B5: candidates. Scene cuts, moment clips snapped to cuts, 3 frames each."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from backend import events
from backend import run as orchestrator
from backend.contracts import Candidates, Moment, Moments, frame_time
from backend.pipeline import candidates as c
from backend.pipeline.ff import video_info


def m(mid, start, end, kind="action"):
    return Moment(moment_id=mid, start=start, end=end, description="x", feature_id=None, kind=kind, confidence=0.9)


def test_scenes_merge_short_split_long_and_cover_everything():
    sc = c.scenes_from_cuts([0.2, 4.0, 4.5, 9.0], 40.0)  # 0.2: edge cut; 4.0-4.5: too short
    assert sc[0] == (0.0, 4.5) and sc[1] == (4.5, 9.0)  # the 0.5 s scene joins the one before
    assert all(b - a <= c.MAX_SCENE_S + 1e-6 for a, b in sc)
    assert sc[-1][1] == 40.0 and all(sc[i][1] == sc[i + 1][0] for i in range(len(sc) - 1))


def test_scenes_capped_on_busy_takes():
    sc = c.scenes_from_cuts([7.0 * k for k in range(1, 60)], 420.0)
    assert len(sc) == c.MAX_SCENES and sc[0][0] == 0.0 and sc[-1][1] == 420.0


def test_moment_snaps_to_cuts_within_1_5_s():
    edges = [0.0, 4.0, 9.0, 15.0, 20.0]
    assert c.moment_span(m("m1", 4.6, 14.2), edges, 20.0) == (4.0, 15.0)
    assert c.moment_span(m("m2", 5.0, 7.0), edges, 20.0) == (4.0, 7.0)   # 7.0 is 2 s from any cut
    assert c.moment_span(m("m3", 18.0, 99.0), edges, 20.0) == (18.0, 20.0)  # clamped to the take
    assert c.moment_span(m("m4", 25.0, 30.0), edges, 20.0) is None          # past the end


def test_frame_names_round_trip():
    assert c.frame_name("c02", 6.25) == "frames/c02_6.25s.jpg" and c.frame_name("c01", 1.0) == "frames/c01_1s.jpg"
    for t in c.frame_times(5.0, 10.0):
        assert 5.0 <= frame_time(c.frame_name("c01", t)) <= 10.0


def test_build_segments_drops_dead_and_duplicates():
    segs = c.build_segments([4.0, 9.0, 15.0], 20.0,
                            [m("m1", 0.1, 3.9), m("m2", 9.5, 14.6, "result"), m("m3", 15.0, 20.0, "dead")])
    Candidates(segments=segs)  # contract: ids unique, frames inside, moment ids present
    assert [s.segment_id for s in segs] == [f"c{i:02d}" for i in range(1, len(segs) + 1)]
    assert [s.start for s in segs] == sorted(s.start for s in segs)
    spans = [(s.start, s.end, s.source) for s in segs]
    assert (0.0, 4.0, "moment") in spans and (0.0, 4.0, "scene") not in spans   # duplicate scene dropped
    assert (9.0, 15.0, "moment") in spans
    assert not any(s.source == "scene" and s.start >= 15.0 for s in segs)      # inside the dead moment
    assert all(s.moment_id != "m3" for s in segs)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_stage_on_a_take_with_known_cuts(tmp_path, monkeypatch):
    take = tmp_path / "take.mp4"
    parts = ["color=c=0x223344:s=320x180:r=30:d=4", "testsrc2=s=320x180:r=30:d=5",
             "color=c=0xeeeeee:s=320x180:r=30:d=6", "smptebars=s=320x180:r=30:d=5"]
    args = [a for p in parts for a in ("-f", "lavfi", "-i", p)]
    subprocess.run(["ffmpeg", "-loglevel", "error", *args, "-filter_complex", "concat=n=4:v=1",
                    "-c:v", "libx264", "-preset", "ultrafast", str(take)], check=True)
    assert c.scene_cuts(take) == pytest.approx([4.0, 9.0, 15.0], abs=0.05)

    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    d = orchestrator.new_run_from_fixture(recording="take.mp4")
    shutil.copy(take, d / "take.mp4")
    (d / "moments.json").write_text(Moments(moments=[m("m1", 4.7, 14.0, "result")]).model_dump_json())
    cfg = orchestrator.load_run(d)
    events.bind(d, cfg.run_id)
    c.run(d, cfg)
    orchestrator.check_outputs("candidates", d)

    segs = Candidates.model_validate_json((d / "candidates.json").read_text()).segments
    mom = next(s for s in segs if s.source == "moment")
    assert (mom.start, mom.end, mom.moment_id) == (4.0, 15.0, "m1")
    for s in segs:
        for f in s.frames:
            assert video_info(d / f).width == c.FRAME_WIDTH
