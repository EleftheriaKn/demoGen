"""Stage `render` (owner: B).

Every shot goes through shot(): same size, frame rate and codecs, so the join never breaks.
The final video joins the shots with the concat filter. The final audio is built from the voice files placed at their exact timeline starts (no drift
against the captions), with the music bed ducked under the voice. Captions are burned in last.

Reads timeline.json, captions.ass, voice/music/cards/recording. Writes shots/, final.mp4.

Run alone: python -m backend.pipeline.render runs/<run_id>
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import events
from backend.contracts import RunConfig, Timeline, TimelineShot
from backend.pipeline.ff import (AUDIO, FONTS, SAMPLE_RATE, VIDEO, ff, filter_path, hdr_to_sdr, inner_size,
                                 place, video_info)

MAX_ZOOM = 3.0
WORKERS = 4
LOUDNESS = -16  # LUFS, the usual target for web video


def zoom_filter(s: TimelineShot, iw: int, ih: int, fps: int) -> str:
    """Ease from the full frame into the focus box over ease_s, then hold.
    Runs on the source picture (iw x ih, its own shape) before it is placed in the output frame,
    so the box means the same thing for portrait, 4:3 or 16:9 takes. Uniform zoom: no stretching."""
    z = s.visual.zoom
    if z is None:
        return ""
    bx, by, bw, bh = z.box
    target = min(1 / max(bw, bh), MAX_ZOOM)
    ease = max(1, round(z.ease_s * fps))
    zexp = f"1+{target - 1:.4f}*min(on/{ease}\\,1)"
    cx, cy = bx + bw / 2, by + bh / 2
    x = f"max(0\\,min(iw-iw/zoom\\,iw*{cx:.4f}-iw/zoom/2))"
    y = f"max(0\\,min(ih-ih/zoom\\,ih*{cy:.4f}-ih/zoom/2))"
    return f",zoompan=z='{zexp}':x='{x}':y='{y}':d=1:s={iw}x{ih}:fps={fps}"


def shot(d: Path, s: TimelineShot, tl: Timeline, out: Path) -> None:
    """One timeline shot -> shots/<id>.mp4 (w x h, fps, yuv420p, 48 kHz stereo), whatever the source."""
    w, h, fps, v = tl.width, tl.height, tl.fps, s.visual
    dur = f"{s.duration_s:.4f}"
    info = video_info(d / v.path)
    iw, ih = inner_size(info.width, info.height, w, h)
    if v.kind == "card":
        inp = ["-loop", 1, "-framerate", fps, "-t", dur, "-i", v.path]
        src = f"[0:v]scale={iw}:{ih},setsar=1,fps={fps}[src]"
        tail = ""
    else:
        inp = ["-ss", f"{v.source_start:.3f}", "-to", f"{v.source_end:.3f}", "-i", v.path]
        src = (f"[0:v]{hdr_to_sdr(info)}setpts=(PTS-STARTPTS)/{v.speed},fps={fps},"
               f"scale={iw}:{ih},setsar=1{zoom_filter(s, iw, ih, fps)}[src]")
        tail = f"tpad=stop_mode=clone:stop_duration={v.freeze_s + 1:.3f},"
    graph = ";".join([src, place("src", "placed", iw, ih, w, h),
                      f"[placed]{tail}trim=duration={dur}[v]",
                      f"[1:a]aresample={SAMPLE_RATE},apad[a]"])
    ff(*inp, "-i", s.voice_path, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
       "-t", dur, "-r", fps, *VIDEO, *AUDIO, out.relative_to(d), cwd=d)


def final(d: Path, tl: Timeline) -> None:
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
                 + ("[vo]" if tl.music else "[a]"))
    if tl.music:
        args += ["-stream_loop", "-1", "-i", tl.music.path]
        parts.append(f"[{2 * n}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"volume={tl.music.gain_db}dB,atrim=duration={total}[m]")
        if tl.music.duck:
            parts.append("[vo]asplit=2[vo1][key]")
            parts.append("[m][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300[md]")
            parts.append("[vo1][md]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            parts.append("[vo][m]amix=inputs=2:duration=first:normalize=0[a]")
    ff(*args, "-filter_complex", ";".join(parts), "-map", "[vid]", "-map", "[a]", "-t", total,
       *VIDEO, *AUDIO, "-movflags", "+faststart", "final.mp4", cwd=d)


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir).resolve()
    tl = Timeline.model_validate_json((d / "timeline.json").read_text(encoding="utf-8"))
    (d / "shots").mkdir(exist_ok=True)
    outs = [d / "shots" / f"{s.shot_id}.mp4" for s in tl.shots]
    with ThreadPoolExecutor(WORKERS) as ex:
        for s, _ in zip(tl.shots, ex.map(lambda so: shot(d, so[0], tl, so[1]), zip(tl.shots, outs))):
            events.emit("render", f"shot {s.shot_id} rendered")
    final(d, tl)
    events.emit("render", f"final.mp4 {tl.duration_s:.1f} s at {tl.width}x{tl.height}")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("render")
