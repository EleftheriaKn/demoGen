"""Stage `assets` (owner: A).

Voice: one fal TTS take per shot, cached, 6 at a time. Music and cards still come from the fixture.

Reads script.json. Writes assets.json, voice/, music.wav, cards/ (decisions.json is left as is).

Run alone: python -m backend.pipeline.assets runs/<run_id>
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import cache, events
from backend.clients import fal
from backend.contracts import Assets, CardAsset, MusicAsset, RunConfig, Script, VoiceAsset
from backend.pipeline import ff

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_RUN = ROOT / "fixtures" / "run"
PARALLEL = 6


def _voice(run_dir: Path, shot_id: str, text: str, lang: str) -> VoiceAsset:
    rel = f"voice/{shot_id}.mp3"
    request = {"text": text, "lang": lang, "endpoint": fal.TTS_ENDPOINT}
    cache.file_call("voice", request, run_dir / rel, lambda tmp: cache.retry(lambda: fal.tts(text, lang, tmp)))
    v = VoiceAsset(shot_id=shot_id, path=rel, duration_s=ff.duration(run_dir / rel), language=lang)
    events.emit("assets", f"Voice {shot_id}: {v.duration_s:.2f}s", data={"path": rel})
    return v


def run(run_dir: Path, run: RunConfig) -> None:
    script = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(PARALLEL) as pool:
        voice = list(pool.map(lambda s: _voice(run_dir, s.shot_id, s.voice_line, script.language), script.shots))

    # ponytail: music and cards are copied from the fixture; A7 generates them for real.
    fixture = Assets.model_validate_json((FIXTURE_RUN / "assets.json").read_text(encoding="utf-8"))
    shutil.copy2(FIXTURE_RUN / "music.wav", run_dir / "music.wav")
    events.emit("assets", "Music: music.wav (fixture)", data={"path": "music.wav"})
    shutil.copytree(FIXTURE_RUN / "cards", run_dir / "cards", dirs_exist_ok=True)
    for c in fixture.cards:
        events.emit("assets", f"Card {c.shot_id}: {c.path} (fixture)", data={"path": c.path})
    music = MusicAsset(path="music.wav", duration_s=ff.duration(run_dir / "music.wav"))
    cards: list[CardAsset] = fixture.cards

    assets = Assets(voice=voice, music=music, cards=cards, broll=[])
    (run_dir / "assets.json").write_text(assets.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("assets")
