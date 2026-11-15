"""B2: timeline -> captions -> render on the fixtures gives a valid final.mp4."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from backend import run as orchestrator
from backend.contracts import Timeline
from backend.pipeline import captions, timeline

REAL = {"timeline", "captions", "render"}


def test_fit_source_speeds_up_or_freezes():
    assert timeline.fit_source(0, 6, 5, None) == (0, 6.0, 1.2, 0.0)
    assert timeline.fit_source(0, 10, 5, None) == (0, 7.5, 1.5, 0.0)       # capped at 1.5x, end trimmed
    assert timeline.fit_source(5, 9.5, 5, None) == (5, 9.5, 1.0, 0.5)      # hold last frame
    assert timeline.fit_source(25, 40, 4, 30.0)[1] <= 30.0                 # never past the recording


@pytest.mark.parametrize("text", [
    "Every night it's the same question. With PantryPal, you just snap one photo of your fridge.",
    "Κάθε βράδυ η ίδια ερώτηση· με το PantryPal βγάζεις μια φωτογραφία του ψυγείου σου και παίρνεις τρεις συνταγές;",
])
def test_chunks_keep_every_word_and_stay_short(text):
    parts = captions.chunks(text)
    assert " ".join(parts) == " ".join(text.split())
    assert all(len(p) <= captions.MAX_CHARS + captions.ORPHAN for p in parts)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    d = orchestrator.new_run_from_fixture()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_render_half_on_fixtures(run_dir, monkeypatch):
    from backend.contracts import STAGES

    monkeypatch.setenv("DEMOCUT_FAKE", ",".join(s for s in STAGES if s not in REAL))
    final = orchestrator.run_pipeline(run_dir)

    tl = Timeline.model_validate_json((run_dir / "timeline.json").read_text())
    voice = {v["shot_id"]: v["duration_s"] for v in json.loads((run_dir / "assets.json").read_text())["voice"]}
    assert all(s.duration_s >= voice[s.shot_id] for s in tl.shots)       # never cut the voice

    srt = (run_dir / "captions.srt").read_text()
    assert "PantryPal" in srt and (run_dir / "captions.ass").exists()

    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,width,height,nb_read_frames:format=duration", "-of", "json",
                          str(final)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (tl.width, tl.height)
    assert int(v["nb_read_frames"]) == round(tl.duration_s * tl.fps)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - tl.duration_s) < 0.05


def test_long_voice_plays_more_footage_not_a_long_freeze():
    # 2 s clip, 12 s voice, inside a 10 s segment of a 60 s take: the whole segment plays, never past it
    start, end, speed, freeze = timeline.fit_footage(20.0, 22.0, 12.0, (18.0, 28.0), 60.0)
    assert speed == 1.0 and (start, end) == (18.0, 28.0) and freeze == pytest.approx(2.0, abs=0.01)
    # a short take can't give more: freeze is what's left
    assert timeline.fit_footage(1.0, 2.0, 6.0, None, 3.0)[3] == pytest.approx(5.0, abs=0.01)  # no segment: no growth


def test_growing_never_replays_a_neighbour():
    spans = [None, (46.3, 49.2), (54.3, 56.3), None]
    rooms = timeline.rooms(spans)
    assert rooms[1] == (0.0, 54.3) and rooms[2] == (49.2, float("inf"))
    s3 = timeline.fit_footage(46.3, 49.2, 12.0, (44.0, 50.0), 180.0, rooms[1])
    assert s3[1] <= 54.3


def test_missing_card_becomes_a_plain_card(tmp_path):
    rel = timeline.plain_card(tmp_path, "s08", "TodoMVC", (640, 360))
    assert rel == "cards/plain/s08.png" and (tmp_path / rel).stat().st_size > 0
    assert not list((tmp_path / "cards" / "plain").glob("*.txt"))
