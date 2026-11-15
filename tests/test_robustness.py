"""B8: B's stages keep going when the inputs are degraded, and a run that stops mid-render resumes."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from backend import events
from backend import run as orchestrator
from backend.contracts import STAGES, Assets, Event, Timeline
from backend.pipeline import captions, render, timeline

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
REAL = {"timeline", "captions", "render"}


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", ",".join(s for s in STAGES if s not in REAL))
    d = orchestrator.new_run_from_fixture()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run_until(d, stage):
    """Run the faked stages up to (not including) `stage`, so a test can degrade their outputs."""
    cfg = orchestrator.load_run(d)
    events.bind(d, cfg.run_id)
    for s in STAGES[:STAGES.index(stage)]:
        orchestrator.run_stage(s, d, cfg, s not in REAL, 0.0)
    return cfg


def frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def test_fal_fallbacks_render(run_dir):
    """What A's assets writes when fal fails: a plain card of another size and a silent music bed."""
    cfg = run_until(run_dir, "timeline")
    ffmpeg("-f", "lavfi", "-i", "color=c=0x334455:s=1000x1000:d=1", "-frames:v", "1", run_dir / "cards/s01.png")
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "15", run_dir / "music.wav")
    for s in ("timeline", "captions", "render"):
        orchestrator.run_stage(s, run_dir, cfg, False, 1.0)
    tl = Timeline.model_validate_json((run_dir / "timeline.json").read_text(encoding="utf-8"))
    assert frames(run_dir / "final.mp4") == round(tl.duration_s * tl.fps)


def test_missing_card_and_music_still_render(run_dir):
    cfg = run_until(run_dir, "timeline")
    (run_dir / "cards/s01.png").unlink()
    (run_dir / "music.wav").unlink()
    for s in ("timeline", "captions", "render"):
        orchestrator.run_stage(s, run_dir, cfg, False, 1.0)
    tl = Timeline.model_validate_json((run_dir / "timeline.json").read_text(encoding="utf-8"))
    assert tl.shots[0].visual.path == "cards/plain/s01.png"
    msgs = [json.loads(line)["msg"] for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any("rendering without music" in m for m in msgs)
    assert (run_dir / "final.mp4").stat().st_size > 0


def test_missing_voice_names_the_shot(run_dir):
    cfg = run_until(run_dir, "render")
    (run_dir / "voice/s02.mp3").unlink()
    with pytest.raises(RuntimeError, match="voice for s02 is missing.*re-run assets"):
        render.run(run_dir, cfg)
    assert not (run_dir / "final.mp4").exists()


def test_crash_mid_render_leaves_no_final_and_resumes(run_dir, monkeypatch):
    real_final = render.final

    def crash(d, tl, music):
        (d / "final.part.mp4").write_bytes(b"half a video")
        raise KeyboardInterrupt("killed mid-encode")

    monkeypatch.setattr(render, "final", crash)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_pipeline(run_dir)
    assert not (run_dir / "final.mp4").exists()

    monkeypatch.setattr(render, "final", real_final)
    final = orchestrator.run_pipeline(run_dir)  # same folder: resumes
    evs = [Event.model_validate_json(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    skipped = {e.stage for e in evs if e.msg.endswith("skipped (already done)")}
    assert {"timeline", "captions"} <= skipped and "render" not in skipped
    assert final.exists() and not (run_dir / "final.part.mp4").exists()


@pytest.mark.parametrize("span,target,limit", [
    ((25.0, 40.0), 4.0, 30.0),   # moment runs past the end of the take
    ((31.0, 33.0), 4.0, 30.0),   # moment starts after the end
    ((12.0, 12.05), 3.0, 30.0),  # tiny clip
    ((10.0, 100.0), 5.0, 120.0),  # 90 s clip for a 5 s line: 1.5x, then trimmed
])
def test_fit_footage_edges(span, target, limit):
    start, end, speed, freeze = timeline.fit_footage(*span, target, None, limit)
    assert 0 <= start < end <= limit and 0 < speed <= 1.5 and freeze >= 0
    assert (end - start) / speed + freeze == pytest.approx(target, abs=0.01)


def test_long_url_in_a_caption_is_cut():
    parts = captions.chunks("Try it at https://example.com/" + "a" * 80 + " today")
    assert all(len(p) <= captions.MAX_CHARS + captions.ORPHAN for p in parts)
    assert "".join(parts).replace(" ", "") == ("Tryitathttps://example.com/" + "a" * 80 + "today")


def test_assets_with_missing_voice_entry_fails_clearly(run_dir):
    """A failed voice take stops the run (team decision); timeline names the shot."""
    cfg = run_until(run_dir, "timeline")
    a = Assets.model_validate_json((run_dir / "assets.json").read_text(encoding="utf-8"))
    a.voice = [v for v in a.voice if v.shot_id != "s03"]
    (run_dir / "assets.json").write_text(a.model_dump_json(), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no voice take for shot s03"):
        timeline.run(run_dir, cfg)
