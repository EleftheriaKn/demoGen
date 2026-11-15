"""Stage `assets` (owner: A).

Voice: one fal TTS take per shot, cached, 6 at a time.
Cards: for every card shot, 4 FLUX 2 drafts (no text: "fal draws, we write"), judged by
``pick_best``, one ``take_pick`` decision each. Cheap mode: one plain colour card, no judge.
Music: one instrumental bed about as long as the voice track (silence in cheap mode).

Reads script.json. Writes assets.json, voice/, music.wav, cards/, and this stage's decisions.

Run alone: python -m backend.pipeline.assets runs/<run_id>
"""

from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image

from backend import cache, events
from backend.clients import fal
from backend.contracts import Assets, CardAsset, MusicAsset, RunConfig, Script, Shot, VoiceAsset
from backend.decisions import append_decision, drop_stage
from backend.pipeline import ff, judge

PARALLEL = 6
DRAFTS = 4
NO_TEXT = (
    "Background artwork only, with no typography: no words, letters, numbers, logos, buttons or UI anywhere. "
    "Leave calm empty space in the centre for a title we add later."
)
QUOTED = re.compile(r"(?<!\w)['\"‘“][^'\"’”]*['\"’”](?!\w)")  # 'Name', not app's
CARD_QUESTIONS = [
    "Is the image free of any text, letters, numbers or logos?",
    "Would a title written on top of it stay easy to read?",
    "Does it look polished and fit the app's style?",
]
CHEAP_CARD_RGB = (24, 24, 32)
MUSIC_PROMPT = "Calm, upbeat instrumental background music for a short tech product demo; light and unobtrusive under a voice-over."


def _voice(run_dir: Path, shot_id: str, text: str, lang: str) -> VoiceAsset:
    rel = f"voice/{shot_id}.mp3"
    request = {"text": text, "lang": lang, "endpoint": fal.TTS_ENDPOINT}
    cache.file_call("voice", request, run_dir / rel, lambda tmp: cache.retry(lambda: fal.tts(text, lang, tmp)))
    v = VoiceAsset(shot_id=shot_id, path=rel, duration_s=ff.duration(run_dir / rel), language=lang)
    events.emit("assets", f"Voice {shot_id}: {v.duration_s:.2f}s", data={"path": rel})
    return v


def _draft(run_dir: Path, rel: str, prompt: str, n: int) -> None:
    request = {"prompt": prompt, "n": n, "endpoint": fal.CARD_ENDPOINT}
    cache.file_call("card", request, run_dir / rel, lambda tmp: cache.retry(lambda: fal.card_image(prompt, tmp)))


def card_prompt(prompt: str) -> str:
    """fal draws, we write: drop quoted titles from the script's prompt and keep only its look."""
    look = " ".join(QUOTED.sub("", prompt).split())
    return f"Background for a video title card, in this style: {look} {NO_TEXT}"


def _cheap_card(run_dir: Path, shot: Shot, mode: str) -> CardAsset:
    rel, draft = f"cards/{shot.shot_id}.png", f"cards/drafts/{shot.shot_id}_0.png"
    (run_dir / draft).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", ff.size_for(mode), CHEAP_CARD_RGB).save(run_dir / draft)
    shutil.copyfile(run_dir / draft, run_dir / rel)
    events.emit("assets", f"Card {shot.shot_id}: plain card (cheap)", data={"path": rel})
    return CardAsset(shot_id=shot.shot_id, path=rel, drafts=[draft], chosen_index=0)


def _judge(run_dir: Path, shot: Shot, drafts: list[str]) -> judge.PickResult:
    context = f"a title card for a product demo video, generated from: {shot.visual.card_prompt}"
    paths = [run_dir / d for d in drafts]
    request = {"drafts": paths, "questions": CARD_QUESTIONS, "context": context}
    return cache.json_call("pick", request, lambda: judge.pick_best(paths, CARD_QUESTIONS, context))


def _cards(run_dir: Path, shots: list[Shot]) -> list[CardAsset]:
    drafts = {s.shot_id: [f"cards/drafts/{s.shot_id}_{n}.png" for n in range(DRAFTS)] for s in shots}
    jobs = [(rel, card_prompt(s.visual.card_prompt), n) for s in shots for n, rel in enumerate(drafts[s.shot_id])]
    with ThreadPoolExecutor(PARALLEL) as pool:
        list(pool.map(lambda j: _draft(run_dir, *j), jobs))
        picks = list(pool.map(lambda s: _judge(run_dir, s, drafts[s.shot_id]), shots))

    cards = []
    for shot, pick in zip(shots, picks):  # decisions.json is written one card at a time, in shot order
        options, rel = drafts[shot.shot_id], f"cards/{shot.shot_id}.png"
        shutil.copyfile(run_dir / options[pick["index"]], run_dir / rel)
        append_decision(
            run_dir, stage="assets", kind="take_pick", subject=shot.shot_id, options=options,
            chosen=options[pick["index"]], questions=CARD_QUESTIONS, answers=pick["answers"],
            judge_score=pick["judge_score"], reason=pick["reason"],
        )
        events.emit("assets", f"Card {shot.shot_id}: draft {pick['index']} of {len(options)}", data={"path": rel})
        cards.append(CardAsset(shot_id=shot.shot_id, path=rel, drafts=options, chosen_index=pick["index"]))
    return cards


def _music(run_dir: Path, seconds: float) -> MusicAsset:
    seconds = round(min(max(seconds, 3.0), 600.0))  # endpoint accepts 3..600 s

    def make(tmp: Path) -> None:
        mp3 = tmp.with_suffix(".mp3")
        try:
            cache.retry(lambda: fal.music(MUSIC_PROMPT, seconds, mp3))
            ff.ff("-i", mp3, "-ar", str(ff.SAMPLE_RATE), "-ac", "2", "-f", "wav", tmp)
        finally:
            mp3.unlink(missing_ok=True)

    request = {"prompt": MUSIC_PROMPT, "seconds": seconds, "endpoint": fal.MUSIC_ENDPOINT}
    cache.file_call("music", request, run_dir / "music.wav", make)
    m = MusicAsset(path="music.wav", duration_s=ff.duration(run_dir / "music.wav"))
    events.emit("assets", f"Music: {m.duration_s:.1f}s", data={"path": m.path})
    return m


def _silent_music(run_dir: Path, seconds: float) -> MusicAsset:
    # ponytail: STAGE_OUTPUTS requires music.wav, so cheap mode writes silence instead of music=null.
    ff.ff("-f", "lavfi", "-i", f"anullsrc=r={ff.SAMPLE_RATE}:cl=stereo", "-t", f"{max(seconds, 1.0):.3f}", run_dir / "music.wav")
    m = MusicAsset(path="music.wav", duration_s=ff.duration(run_dir / "music.wav"))
    events.emit("assets", f"Music: {m.duration_s:.1f}s of silence (cheap)", data={"path": m.path})
    return m


def run(run_dir: Path, run: RunConfig) -> None:
    script = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(PARALLEL) as pool:
        voice = list(pool.map(lambda s: _voice(run_dir, s.shot_id, s.voice_line, script.language), script.shots))

    card_shots = [s for s in script.shots if s.visual.kind == "card"]
    drop_stage(run_dir, "assets")
    shutil.rmtree(run_dir / "cards", ignore_errors=True)  # a re-run (or mode switch) leaves no stale drafts
    if run.mode == "cheap":
        cards = [_cheap_card(run_dir, s, run.mode) for s in card_shots]
        music = _silent_music(run_dir, sum(v.duration_s for v in voice))
    else:
        cards = _cards(run_dir, card_shots)
        music = _music(run_dir, sum(v.duration_s for v in voice))

    assets = Assets(voice=voice, music=music, cards=cards, broll=[])
    (run_dir / "assets.json").write_text(assets.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("assets")
