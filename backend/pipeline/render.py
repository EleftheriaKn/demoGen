"""Stage `render` (owner: B).

Every shot goes through shot(): same size, frame rate and codecs, so the join never breaks.
The final video joins the shots with the concat filter. The final audio is built from the voice files placed at their exact timeline starts (no drift
against the captions), with the music bed ducked under the voice. Captions are burned in last.

Reads timeline.json, captions.ass, voice/music/cards/recording. Writes shots/, final.mp4.

Run alone: python -m backend.pipeline.render runs/<run_id>
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import events
from backend.contracts import RunConfig, Timeline, TimelineMusic, TimelineShot
from backend.pipeline.ff import (AUDIO, FONTS, SAMPLE_RATE, VIDEO, ff, filter_path, hdr_to_sdr, inner_size,
                                 place, video_info)

MAX_ZOOM = 3.0
DRIFT = 0.05          # slow push-in on cards and held frames: +5% over the shot
HOLD_MOTION_S = 1.0   # held frames longer than this get the push-in
UPSCALE = 2           # zoompan works on a 2x picture so slow moves do not jitter
WORKERS = 4
LOUDNESS = -16  # LUFS, the usual target for web video


def zoom_filter(s: TimelineShot, iw: int, ih: int, fps: int) -> str:
    """Camera move for one shot, on the source picture (iw x ih, its own shape) before it is placed in the
    output frame, so the box means the same for portrait, 4:3 or 16:9 takes. Uniform zoom: no stretching.
    - focus box: ease from the full frame into the box over ease_s, then hold
    - a held frame longer than HOLD_MOTION_S, and every card: a slow push-in (DRIFT over the shot), so a
      still reads as a camera move, not a stuck video
    The picture is upscaled first: zoompan rounds to whole pixels and a slow zoom would jitter."""
    z, v = s.visual.zoom, s.visual
    drift = DRIFT if v.kind == "card" or v.freeze_s > HOLD_MOTION_S else 0.0
    if z is None and not drift:
        return ""
    total = max(1, round(s.duration_s * fps))
    if z is None:
        target, ease, cx, cy = 1.0, 1, 0.5, 0.5
    else:
        bx, by, bw, bh = z.box
        target, ease, cx, cy = min(1 / max(bw, bh), MAX_ZOOM), max(1, round(z.ease_s * fps)), bx + bw / 2, by + bh / 2
    zexp = f"1+{target - 1:.4f}*min(on/{ease}\\,1)+{drift:.4f}*on/{total}"
    x = f"max(0\\,min(iw-iw/zoom\\,iw*{cx:.4f}-iw/zoom/2))"
    y = f"max(0\\,min(ih-ih/zoom\\,ih*{cy:.4f}-ih/zoom/2))"
    return (f",scale={iw * UPSCALE}:{ih * UPSCALE}:flags=bicubic,"
            f"zoompan=z='{zexp}':x='{x}':y='{y}':d=1:s={iw}x{ih}:fps={fps}")


def shot(d: Path, s: TimelineShot, tl: Timeline, out: Path) -> None:
    """One timeline shot -> shots/<id>.mp4 (w x h, fps, yuv420p, 48 kHz stereo), whatever the source."""
    w, h, fps, v = tl.width, tl.height, tl.fps, s.visual
    dur = f"{s.duration_s:.4f}"
    info = video_info(d / v.path)
    iw, ih = inner_size(info.width, info.height, w, h)
    if v.kind == "card":
        inp = ["-loop", 1, "-framerate", fps, "-t", dur, "-i", v.path]
        src = f"[0:v]scale={iw}:{ih},setsar=1,fps={fps}{zoom_filter(s, iw, ih, fps)}[src]"
    else:  # the freeze comes before the camera move, so held frames move too
        inp = ["-ss", f"{v.source_start:.3f}", "-to", f"{v.source_end:.3f}", "-i", v.path]
        src = (f"[0:v]{hdr_to_sdr(info)}setpts=(PTS-STARTPTS)/{v.speed},fps={fps},scale={iw}:{ih},setsar=1,"
               f"tpad=stop_mode=clone:stop_duration={v.freeze_s + 1:.3f}{zoom_filter(s, iw, ih, fps)}[src]")
    graph = ";".join([src, place("src", "placed", iw, ih, w, h),
                      f"[placed]trim=duration={dur}[v]",
                      f"[1:a]aresample={SAMPLE_RATE},apad[a]"])
    ff(*inp, "-i", s.voice_path, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
       "-t", dur, "-r", fps, *VIDEO, *AUDIO, out.relative_to(d), cwd=d)


def check_inputs(d: Path, tl: Timeline) -> TimelineMusic | None:
    """Fail early, naming the shot, when a voice or picture is missing; a missing music bed only drops the
    music (it is decoration). Returns the music to use."""
    for s in tl.shots:
        if not (d / s.voice_path).exists():
            raise RuntimeError(f"render: voice for {s.shot_id} is missing ({s.voice_path}); re-run assets")
        if not (d / s.visual.path).exists():
            raise RuntimeError(f"render: {s.visual.kind} for {s.shot_id} is missing ({s.visual.path}); "
                               "re-run timeline")
    if tl.music and not (d / tl.music.path).exists():
        events.emit("render", f"music {tl.music.path} is missing: rendering without music")
        return None
    return tl.music


def final(d: Path, tl: Timeline, music: TimelineMusic | None) -> None:
    """Join the shots, lay the voices and music, burn the captions. Written to final.part.mp4 and renamed
    when complete, so a crash or Ctrl-C never leaves a half-written final.mp4."""
    n = len(tl.shots)
    args = []
    for s in tl.shots:
        args += ["-i", f"shots/{s.shot_id}.mp4"]
    for s in tl.shots:
        args += ["-i", s.voice_path]
    total = f"{tl.duration_s:.4f}"
    subs = f",ass=captions.ass:fontsdir={filter_path(FONTS)}" if (d / "captions.ass").exists() else ""
    parts = [f"{''.join(f'[{i}:v]' for i in range(n))}concat=n={n}:v=1:a=0{subs}[vid]"]
    parts += [f"[{n + i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
              f"adelay={round(s.start * 1000)}:all=1[v{i}]" for i, s in enumerate(tl.shots)]
    parts.append(f"{''.join(f'[v{i}]' for i in range(n))}amix=inputs={n}:duration=longest:normalize=0,"
                 f"loudnorm=I={LOUDNESS}:TP=-1.5:LRA=11,aresample={SAMPLE_RATE},apad,atrim=duration={total}"
                 + ("[vo]" if music else "[a]"))
    if music:
        args += ["-stream_loop", "-1", "-i", music.path]
        parts.append(f"[{2 * n}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"volume={music.gain_db}dB,atrim=duration={total}[m]")
        if music.duck:
            parts.append("[vo]asplit=2[vo1][key]")
            parts.append("[m][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300[md]")
            parts.append("[vo1][md]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            parts.append("[vo][m]amix=inputs=2:duration=first:normalize=0[a]")
    part = d / "final.part.mp4"
    try:
        ff(*args, "-filter_complex", ";".join(parts), "-map", "[vid]", "-map", "[a]", "-t", total,
           *VIDEO, *AUDIO, "-movflags", "+faststart", part.name, cwd=d)
        os.replace(part, d / "final.mp4")
    finally:
        part.unlink(missing_ok=True)


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir).resolve()
    tl = Timeline.model_validate_json((d / "timeline.json").read_text(encoding="utf-8"))
    music = check_inputs(d, tl)
    (d / "shots").mkdir(exist_ok=True)
    outs = [d / "shots" / f"{s.shot_id}.mp4" for s in tl.shots]
    with ThreadPoolExecutor(WORKERS) as ex:
        for s, _ in zip(tl.shots, ex.map(lambda so: shot(d, so[0], tl, so[1]), zip(tl.shots, outs))):
            events.emit("render", f"shot {s.shot_id} rendered")
    final(d, tl, music)
    events.emit("render", f"final.mp4 {tl.duration_s:.1f} s at {tl.width}x{tl.height}")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("render")
