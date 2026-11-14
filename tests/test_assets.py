"""Stage assets: one voice take per shot (ticket A4). fal is faked with ffmpeg silence."""

from __future__ import annotations

import shutil

from backend import events
from backend.clients import fal
from backend.contracts import Assets, Script
from backend.pipeline import assets, ff
from backend.run import FIXTURE_RUN, load_run


def test_one_voice_per_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMOCUT_CACHE", str(tmp_path / "cache"))
    calls = []

    def fake_tts(text, language, out_path):
        calls.append((text, language))
        ff.ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.5", "-c:a", "libmp3lame", "-f", "mp3", out_path)

    monkeypatch.setattr(fal, "tts", fake_tts)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for f in ("run.json", "script.json"):
        shutil.copy2(FIXTURE_RUN / f, run_dir / f)
    events.bind(run_dir, "test")

    assets.run(run_dir, load_run(run_dir))

    shots = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8")).shots
    out = Assets.model_validate_json((run_dir / "assets.json").read_text(encoding="utf-8"))
    assert [v.shot_id for v in out.voice] == [s.shot_id for s in shots]
    assert all(v.duration_s > 0 and (run_dir / v.path).exists() for v in out.voice)
    assert len(calls) == len(shots)

    assets.run(run_dir, load_run(run_dir))  # second run: every take comes from the cache
    assert len(calls) == len(shots)
