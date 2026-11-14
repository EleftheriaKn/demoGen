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
