"""Stage `timeline` (owner: B).

Fit footage to voice durations: one shot per script shot, as long as its voice line plus a breath.

Reads script.json, assets.json, the recording. Writes timeline.json.

Run alone: python -m backend.pipeline.timeline runs/<run_id>
"""

from __future__ import annotations

import math
from pathlib import Path

from backend import events
from backend.contracts import (Assets, RunConfig, Script, Timeline, TimelineMusic, TimelineShot,
                               TimelineVisual, Zoom)
from backend.pipeline.ff import duration, size_for

FPS = 30
BREATH_S = 0.25      # footage keeps running this long after the voice ends
MAX_SPEED = 1.5      # contract limit
ZOOM_EASE_S = 0.8
MUSIC_GAIN_DB = -18.0


def frames(t: float) -> float:
    """Round a duration up to a whole frame, so every shot is an exact number of frames."""
    return math.ceil(round(t * FPS, 6)) / FPS


def fit_source(start: float, end: float, target: float, limit: float | None) -> tuple[float, float, float, float]:
    """Fit a source clip into `target` seconds: speed up (max 1.5x) and trim, or hold the last frame.
    Returns (start, end, speed, freeze_s)."""
    if limit is not None:
        end = min(end, limit)
        if end - start < 0.1:
            start = max(0.0, end - 0.1)
    length = end - start
    if length >= target:
        speed = min(length / target, MAX_SPEED)
        return start, round(start + target * speed, 3), round(speed, 4), 0.0
    return start, end, 1.0, round(target - length, 3)


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir)
    script = Script.model_validate_json((d / "script.json").read_text())
    assets = Assets.model_validate_json((d / "assets.json").read_text())
    rec_len = duration(d / run.recording) if (d / run.recording).exists() else None

    voices: dict[str, tuple[str, float]] = {}
    for v in assets.voice:  # prefer the script's language; take any take as a fallback
        if v.shot_id not in voices or v.language == script.language:
            voices[v.shot_id] = (v.path, v.duration_s)
    cards = {c.shot_id: c.path for c in assets.cards}
    brolls = {b.shot_id: b for b in assets.broll}

    shots, t = [], 0.0
    for s in script.shots:
        if s.shot_id not in voices:
            raise RuntimeError(f"timeline: no voice take for shot {s.shot_id} in assets.json")
        voice_path, voice_s = voices[s.shot_id]
        dur = frames(voice_s + BREATH_S)
        vis = s.visual
        if vis.kind == "footage":
            start, end, speed, freeze = fit_source(vis.source_start, vis.source_end, dur, rec_len)
            zoom = Zoom(box=vis.focus_box, ease_s=ZOOM_EASE_S) if vis.focus_box else None
            visual = TimelineVisual(kind="footage", path=run.recording, source_start=start, source_end=end,
                                    speed=speed, freeze_s=freeze, zoom=zoom)
        elif vis.kind == "broll" and s.shot_id in brolls:
            b = brolls[s.shot_id]
            start, end, speed, freeze = fit_source(0.0, b.duration_s, dur, b.duration_s)
            visual = TimelineVisual(kind="broll", path=b.path, source_start=start, source_end=end,
                                    speed=speed, freeze_s=freeze)
        elif s.shot_id in cards:
            visual = TimelineVisual(kind="card", path=cards[s.shot_id])
        else:
            raise RuntimeError(f"timeline: shot {s.shot_id} is a {vis.kind} but assets.json has no file for it")
        shots.append(TimelineShot(shot_id=s.shot_id, start=round(t, 3), duration_s=dur,
                                  voice_path=voice_path, visual=visual, caption=s.voice_line))
        t = round(t + dur, 6)

    music = TimelineMusic(path=assets.music.path, gain_db=MUSIC_GAIN_DB, duck=True) if assets.music else None
    w, h = size_for(run.mode)
    tl = Timeline(fps=FPS, width=w, height=h, duration_s=round(t, 3), shots=shots, music=music)
    (d / "timeline.json").write_text(tl.model_dump_json(indent=2))

    limit = run.brief.time_limit_s
    events.emit("timeline", f"{len(shots)} shots, {tl.duration_s:.1f} s at {w}x{h}"
                + (f" (over the {limit} s limit!)" if tl.duration_s > limit else ""))


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("timeline")
