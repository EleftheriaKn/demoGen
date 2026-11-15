"""Stage `assets` (owner: A).

Voice: one fal TTS take per shot, cached, 6 at a time.
Cards: for every card shot, 4 FLUX 2 drafts (no text: "fal draws, we write"), judged by
``pick_best``, one ``take_pick`` decision each. A draft with
drawn text can't win; if all 4 have text the card is plain. Cheap mode: one plain colour card, no judge.
Panels: a card shot with ``reference_frames`` is an illustrated panel instead (Ideogram Vector):
one frame is redrawn flat (image-to-vector, 1 draft), several frames guide a scene (text-to-vector,
4 drafts in one call). fal returns SVG; we rasterize to PNG at the output size, then judge as cards.
Music: one instrumental bed about as long as the voice track (silence in cheap mode).

fal failures still give a video: after ``cache.retry`` gives up, a voice take is left out of
assets.voice, a card whose drafts all failed is the plain card (no take_pick: nothing was
judged), and failed music is silence. Each fallback emits an event; none raises.

Reads script.json. Writes assets.json, voice/, music.wav, cards/, and this stage's decisions.

Run alone: python -m backend.pipeline.assets runs/<run_id>
"""

from __future__ import annotations

import io
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import resvg_py
from PIL import Image

from backend import cache, events
from backend.clients import fal
from backend.contracts import Assets, CardAsset, MusicAsset, RunConfig, Script, Shot, VoiceAsset
from backend.decisions import append_decision, drop_stage
from backend.pipeline import ff, judge

PARALLEL = 6
DRAFTS = 4
# Taste brief ("generated shots look designed, not fake"): every fal image prompt ends with this.
FAL_STYLE = (
    "Flat vector art that looks designed, not fake: only 2 to 3 colours taken from the app's own screenshots, "
    "generous empty space, soft gradients. No text, letters or logos anywhere."
)
PANEL_SEED = 20261115  # one seed for every panel call: one consistent look, and cache hits across runs
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
NO_TEXT_Q = CARD_QUESTIONS[0]  # a draft under judge.PASS here can't win
CHEAP_CARD_RGB = (24, 24, 32)
NO_FAL_KEY = ("FAL_KEY isn't set, so this video has no voice, generated cards or music. "
              "Add it in the board's Settings and run again.")
MUSIC_PROMPT = "Calm, upbeat instrumental background music for a short tech product demo; light and unobtrusive under a voice-over."


def _voice(run_dir: Path, shot_id: str, text: str, lang: str) -> Optional[VoiceAsset]:
    rel = f"voice/{shot_id}.mp3"
    request = {"text": text, "lang": lang, "endpoint": fal.TTS_ENDPOINT}
    try:
        cache.file_call("voice", request, run_dir / rel, lambda tmp: cache.retry(lambda: fal.tts(text, lang, tmp)))
    except Exception as e:
        events.emit("assets", f"Voice {shot_id}: fal failed, shot has no voice ({e})", data={"shot_id": shot_id})
        return None
    v = VoiceAsset(shot_id=shot_id, path=rel, duration_s=ff.duration(run_dir / rel), language=lang)
    events.emit("assets", f"Voice {shot_id}: {v.duration_s:.2f}s", data={"path": rel})
    return v


def _draft(run_dir: Path, rel: str, prompt: str, n: int) -> bool:
    """True if the draft was made; False (with an event) if fal failed after its retries."""
    request = {"prompt": prompt, "n": n, "endpoint": fal.CARD_ENDPOINT}
    try:
        cache.file_call("card", request, run_dir / rel, lambda tmp: cache.retry(lambda: fal.card_image(prompt, tmp)))
        return True
    except Exception as e:
        events.emit("assets", f"Card draft {rel}: fal failed ({e})", data={"path": rel})
        return False


def card_prompt(prompt: str) -> str:
    """fal draws, we write: drop quoted titles from the script's prompt and keep only its look."""
    look = " ".join(QUOTED.sub("", prompt).split())
    return f"Background for a video title card, in this style: {look} {FAL_STYLE} {NO_TEXT}"


def panel_prompt(prompt: str) -> str:
    """Prompt for a text-to-vector panel: the script's scene, without quoted titles, in our style."""
    scene = " ".join(QUOTED.sub("", prompt).split())
    return (f"An illustrated panel for a product demo video showing: {scene} Use the reference screenshots for the "
            f"app's screens and colours, redrawn as simplified UI cards and icons. {FAL_STYLE}")


def _rasterize(svg: str, out: Path) -> None:
    """SVG fitted inside the full output size, centred on the plain card colour, saved as PNG."""
    w, h = ff.size_for("full")
    art = Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_string=svg, width=w, height=h))).convert("RGBA")
    canvas = Image.new("RGBA", (w, h), CHEAP_CARD_RGB)
    canvas.alpha_composite(art, ((w - art.width) // 2, (h - art.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out)


def _panel(run_dir: Path, shot: Shot, frames: list[Path]) -> list[str]:
    """PNG drafts of an illustrated panel from real frames; [] (with an event) if fal or the SVG failed."""
    redraw = len(frames) == 1
    n, prompt = (1, None) if redraw else (DRAFTS, panel_prompt(shot.visual.card_prompt or ""))
    endpoint = fal.VECTOR_REDRAW_ENDPOINT if redraw else fal.VECTOR_SCENE_ENDPOINT
    request = {"endpoint": endpoint, "frames": frames, "prompt": prompt, "n": n, "seed": PANEL_SEED}

    def make(tmp: Path) -> None:  # one fal call, its SVGs stored together as one JSON list
        svgs = [tmp.with_name(f"{tmp.stem}_{i}.svg") for i in range(n)]
        try:
            if redraw:
                cache.retry(lambda: fal.vector_redraw(frames[0], svgs[0]))
            else:
                cache.retry(lambda: fal.vector_scene(prompt, frames, svgs, PANEL_SEED))
            tmp.write_text(json.dumps([p.read_text(encoding="utf-8") for p in svgs if p.exists()]), encoding="utf-8")
        finally:
            for p in svgs:
                p.unlink(missing_ok=True)

    try:
        out = cache.file_call("panel", request, run_dir / f"cards/drafts/{shot.shot_id}_svg.json", make)
        rels = []
        for i, svg in enumerate(json.loads(out.read_text(encoding="utf-8"))):
            rels.append(f"cards/drafts/{shot.shot_id}_{i}.png")
            _rasterize(svg, run_dir / rels[-1])
        return rels
    except Exception as e:
        events.emit("assets", f"Card {shot.shot_id}: fal panel failed ({e})", data={"shot_id": shot.shot_id})
        return []


def _plain(run_dir: Path, rel: str, mode: str) -> None:
    (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", ff.size_for(mode), CHEAP_CARD_RGB).save(run_dir / rel)


def _cheap_card(run_dir: Path, shot: Shot, mode: str, why: str = "cheap") -> CardAsset:
    rel, draft = f"cards/{shot.shot_id}.png", f"cards/drafts/{shot.shot_id}_0.png"
    _plain(run_dir, draft, mode)
    shutil.copyfile(run_dir / draft, run_dir / rel)
    events.emit("assets", f"Card {shot.shot_id}: plain card ({why})", data={"path": rel})
    return CardAsset(shot_id=shot.shot_id, path=rel, drafts=[draft], chosen_index=0)


def _judge(run_dir: Path, shot: Shot, drafts: list[str]) -> judge.PickResult:
    what = "an illustrated panel" if shot.visual.reference_frames else "a title card"
    context = f"{what} for a product demo video, generated from: {shot.visual.card_prompt}"
    paths = [run_dir / d for d in drafts]
    request = {"drafts": paths, "questions": CARD_QUESTIONS, "context": context, "must_pass": NO_TEXT_Q}
    return cache.json_call("pick", request, lambda: judge.pick_best(paths, CARD_QUESTIONS, context, NO_TEXT_Q))


def _cards(run_dir: Path, shots: list[Shot]) -> list[CardAsset]:
    # frames the script named but that aren't on disk are skipped; none left means a plain FLUX card
    refs = {s.shot_id: [run_dir / f for f in s.visual.reference_frames if (run_dir / f).is_file()] for s in shots}
    panels, flux = [s for s in shots if refs[s.shot_id]], [s for s in shots if not refs[s.shot_id]]
    drafts = {s.shot_id: [f"cards/drafts/{s.shot_id}_{n}.png" for n in range(DRAFTS)] for s in flux}
    jobs = [(rel, card_prompt(s.visual.card_prompt), n) for s in flux for n, rel in enumerate(drafts[s.shot_id])]
    with ThreadPoolExecutor(PARALLEL) as pool:
        made = {j[0] for j, ok in zip(jobs, pool.map(lambda j: _draft(run_dir, *j), jobs)) if ok}
        drafts = {k: [d for d in v if d in made] for k, v in drafts.items()}  # judge only the drafts fal made
        drafts.update(zip([s.shot_id for s in panels], pool.map(lambda s: _panel(run_dir, s, refs[s.shot_id]), panels)))
        judged = [s for s in shots if drafts[s.shot_id]]
        picks = dict(zip([s.shot_id for s in judged], pool.map(lambda s: _judge(run_dir, s, drafts[s.shot_id]), judged)))

    cards = []
    for shot in shots:  # decisions.json is written one card at a time, in shot order
        if shot.shot_id not in picks:
            rel = f"cards/{shot.shot_id}.png"
            plain = f"cards/drafts/{shot.shot_id}_plain.png"
            _plain(run_dir, plain, "full")
            shutil.copyfile(run_dir / plain, run_dir / rel)
            events.emit("assets", f"Card {shot.shot_id}: every fal draft failed, plain card", data={"path": rel})
            cards.append(CardAsset(shot_id=shot.shot_id, path=rel, drafts=[plain], chosen_index=0))
            continue
        pick = picks[shot.shot_id]
        options, rel, index, reason = drafts[shot.shot_id], f"cards/{shot.shot_id}.png", pick["index"], pick["reason"]
        if pick["answers"][0]["score"] < judge.PASS:  # even the winner has drawn text, so all 4 do
            options = [*options, f"cards/drafts/{shot.shot_id}_plain.png"]
            index = len(options) - 1
            _plain(run_dir, options[index], "full")
            reason = f"All {len(options) - 1} drafts show text (best: {pick['answers'][0]['reason']}), so a plain card instead"
        shutil.copyfile(run_dir / options[index], run_dir / rel)
        append_decision(
            run_dir, stage="assets", kind="take_pick", subject=shot.shot_id, options=options,
            chosen=options[index], questions=CARD_QUESTIONS, answers=pick["answers"],
            judge_score=pick["judge_score"], reason=reason,
        )
        events.emit("assets", f"Card {shot.shot_id}: {Path(options[index]).stem} of {len(options)}", data={"path": rel})
        cards.append(CardAsset(shot_id=shot.shot_id, path=rel, drafts=options, chosen_index=index))
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
    try:
        cache.file_call("music", request, run_dir / "music.wav", make)
    except Exception as e:
        return _silent_music(run_dir, seconds, f"fal failed ({e})")
    m = MusicAsset(path="music.wav", duration_s=ff.duration(run_dir / "music.wav"))
    events.emit("assets", f"Music: {m.duration_s:.1f}s", data={"path": m.path})
    return m


def _silent_music(run_dir: Path, seconds: float, why: str = "cheap") -> MusicAsset:
    # ponytail: STAGE_OUTPUTS requires music.wav, so cheap mode writes silence instead of music=null.
    ff.ff("-f", "lavfi", "-i", f"anullsrc=r={ff.SAMPLE_RATE}:cl=stereo", "-t", f"{max(seconds, 1.0):.3f}", run_dir / "music.wav")
    m = MusicAsset(path="music.wav", duration_s=ff.duration(run_dir / "music.wav"))
    events.emit("assets", f"Music: {m.duration_s:.1f}s of silence ({why})", data={"path": m.path})
    return m


def run(run_dir: Path, run: RunConfig) -> None:
    script = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8"))
    has_fal = fal.has_key()  # checked once: no key skips every fal call (and its retries) for the B8 fallbacks
    voice: list[VoiceAsset] = []
    if has_fal:
        with ThreadPoolExecutor(PARALLEL) as pool:
            voice = [v for v in pool.map(lambda s: _voice(run_dir, s.shot_id, s.voice_line, script.language), script.shots) if v]
    else:
        events.emit("assets", NO_FAL_KEY)

    card_shots = [s for s in script.shots if s.visual.kind == "card"]
    drop_stage(run_dir, "assets")
    shutil.rmtree(run_dir / "cards", ignore_errors=True)  # a re-run (or mode switch) leaves no stale drafts
    if run.mode == "cheap" or not has_fal:
        why = "cheap" if run.mode == "cheap" else "no FAL_KEY"
        cards = [_cheap_card(run_dir, s, run.mode, why) for s in card_shots]
        music = _silent_music(run_dir, sum(v.duration_s for v in voice), why)
    else:
        cards = _cards(run_dir, card_shots)
        music = _music(run_dir, sum(v.duration_s for v in voice))

    assets = Assets(voice=voice, music=music, cards=cards, broll=[])
    (run_dir / "assets.json").write_text(assets.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("assets")
