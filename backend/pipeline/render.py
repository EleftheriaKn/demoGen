"""Stage `render` (owner: B).

Every shot goes through shot(): same size, frame rate and codecs, so the join never breaks.
The final video joins the shots with the concat filter (classic style), or in the trend style (B29) with
moving transitions: every shot is rendered with a short tail, and each transition starts exactly at the next
shot's timeline start (xfade offset), so shot times, voices and captions never move. The final audio is built from the voice files placed at their exact timeline starts (no drift
against the captions), with the music bed ducked under the voice. Captions are burned in last.

Reads timeline.json, captions.ass, voice/music/cards/recording. Writes shots/, final.mp4.

Run alone: python -m backend.pipeline.render runs/<run_id>
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import events
from backend.contracts import RunConfig, Script, Timeline, TimelineMusic, TimelineShot
from backend.pipeline.cards import font_safe, tag_overlay
from backend.pipeline.timeline import style_of
from backend.pipeline.ff import (AUDIO, FONTS, SAMPLE_RATE, VIDEO, ff, filter_path, hdr_to_sdr, inner_size,
                                 place, video_info)

MAX_ZOOM = 3.0
DRIFT = 0.05          # slow push-in on cards and held frames: +5% over the shot
HOLD_MOTION_S = 1.0   # held frames longer than this get the push-in
UPSCALE = 2           # zoompan works on a 2x picture so slow moves do not jitter
CALLOUT_IN_S = 0.35   # the callout pops in shortly after the shot starts
WORKERS = 4
LOUDNESS = -16  # LUFS, the usual target for web video
# B29 trend style
TRANSITION_S = 0.35   # moving transitions between beats of the story
INTO_STORY_S = 0.45   # montage -> hook
TRANSITIONS = ("smoothleft", "circleopen", "slideup", "hblur", "squeezeh", "radial")
PAN = 0.06            # B30: cards / panels drift sideways this share of the picture while they push in
TREND_DRIFT = 0.08
TAIL_SLACK_S = 0.5    # extra frames after each shot, never shown (the next shot covers them)


def zoom_filter(s: TimelineShot, iw: int, ih: int, fps: int, pan: float = 0.0, tail: float = 0.0) -> str:
    """Camera move for one shot, on the source picture (iw x ih, its own shape) before it is placed in the
    output frame, so the box means the same for portrait, 4:3 or 16:9 takes. Uniform zoom: no stretching.
    - focus box: ease from the full frame into the box over ease_s, then hold
    - a held frame longer than HOLD_MOTION_S, and every card: a slow push-in (DRIFT over the shot), so a
      still reads as a camera move, not a stuck video
    The picture is upscaled first: zoompan rounds to whole pixels and a slow zoom would jitter."""
    z, v = s.visual.zoom, s.visual
    drift = DRIFT if v.kind == "card" or v.freeze_s > HOLD_MOTION_S else 0.0
    if pan and v.kind == "card":  # B30 trend: a parallax drift, not only a push-in
        drift = TREND_DRIFT
    if z is None and not drift:
        return ""
    total = max(1, round((s.duration_s + tail) * fps))
    if pan and v.kind == "card" and z is None:  # start a little in, so there is room to slide sideways
        zexp = f"1.05+{drift:.4f}*on/{total}"
        x = f"max(0\\,min(iw-iw/zoom\\,iw*({0.5 - pan / 2:.4f}+{pan:.4f}*on/{total})-iw/zoom/2))"
        y = "max(0\\,min(ih-ih/zoom\\,ih*0.5-ih/zoom/2))"
        return (f",scale={iw * UPSCALE}:{ih * UPSCALE}:flags=bicubic,"
                f"zoompan=z='{zexp}':x='{x}':y='{y}':d=1:s={iw}x{ih}:fps={fps}")
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


def callout_filter(d: Path, s: TimelineShot, w: int, h: int) -> str:
    """Kinetic callout (B15): the shot's <= 5 words in a dark pill near the top, fading and sliding in at
    CALLOUT_IN_S. Text comes from a file with expansion off, so any characters (Greek, %) are drawn as written."""
    if not s.callout:
        return ""
    txt = d / "shots" / f"{s.shot_id}.callout.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(font_safe(s.callout.strip()), encoding="utf-8")
    t0, ramp = CALLOUT_IN_S, 0.25
    alpha = f"if(lt(t\\,{t0})\\,0\\,min(1\\,(t-{t0})/{ramp}))"
    y = f"h*0.07+{h // 60}*max(0\\,1-(t-{t0})/{ramp})"
    return (f"drawtext=fontfile={filter_path(FONTS / 'NotoSans-Bold.ttf')}:textfile={filter_path(txt.resolve())}:"
            f"expansion=none:fontsize={round(h * 0.045)}:fontcolor=white:box=1:boxcolor=0x111827@0.85:"
            f"boxborderw={round(h * 0.014)}:x=(w-tw)/2:y='{y}':alpha='{alpha}',")


def shot(d: Path, s: TimelineShot, tl: Timeline, out: Path, tail: float = 0.0, pan: float = 0.0) -> None:
    """One timeline shot -> shots/<id>.mp4 (w x h, fps, yuv420p, 48 kHz stereo), whatever the source.
    ``tail``: extra seconds after the shot for the next transition to blend over (trend style)."""
    w, h, fps, v = tl.width, tl.height, tl.fps, s.visual
    dur = f"{s.duration_s + tail:.4f}"
    info = video_info(d / v.path)
    iw, ih = inner_size(info.width, info.height, w, h)
    if v.kind == "card":
        inp = ["-loop", 1, "-framerate", fps, "-t", dur, "-i", v.path]
        src = f"[0:v]scale={iw}:{ih},setsar=1,fps={fps}{zoom_filter(s, iw, ih, fps, pan, tail)}[src]"
    else:  # the freeze comes before the camera move, so held frames move too
        inp = ["-ss", f"{v.source_start:.3f}", "-to", f"{v.source_end:.3f}", "-i", v.path]
        src = (f"[0:v]{hdr_to_sdr(info)}setpts=(PTS-STARTPTS)/{v.speed},fps={fps},scale={iw}:{ih},setsar=1,"
               f"tpad=stop_mode=clone:stop_duration={v.freeze_s + 1 + tail:.3f}{zoom_filter(s, iw, ih, fps, 0.0, tail)}[src]")
    tag, extra = "", []
    if v.kind == "broll":  # generated video: the same "Illustration" pill as the panels (honesty rule)
        extra = ["-i", tag_overlay(d / "shots" / f"{s.shot_id}.tag.png", (w, h)).relative_to(d)]
        tag = "[placed][2:v]overlay=0:0:format=auto,format=yuv420p[tagged];[tagged]"
    else:
        tag = "[placed]"
    graph = ";".join([src, place("src", "placed", iw, ih, w, h),
                      f"{tag}{callout_filter(d, s, w, h)}trim=duration={dur}[v]",
                      f"[1:a]aresample={SAMPLE_RATE},apad[a]"])  # a broll clip's own sound is never mapped
    ff(*inp, "-i", s.voice_path, *extra, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
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


def transitions(d: Path, tl: Timeline) -> list[tuple[str, float]]:
    """(xfade kind, seconds) for the join before each shot after the first (trend style). Hard cuts (one
    frame) inside the montage and between two demo footage shots, so the beat montage stays punchy and a
    demo sequence reads as one take; moving transitions everywhere else, a different one each time."""
    sp = d / "script.json"
    beat = ({s.shot_id: s.beat for s in Script.model_validate_json(sp.read_text(encoding="utf-8")).shots}
            if sp.exists() else {})
    cut = ("fade", 1 / tl.fps)
    out, k = [], 0
    for a, b in zip(tl.shots, tl.shots[1:]):
        if a.shot_id.startswith("m") and b.shot_id.startswith("m"):
            out.append(cut)
        elif a.shot_id.startswith("m"):
            out.append(("zoomin", INTO_STORY_S))
        elif beat.get(a.shot_id) == beat.get(b.shot_id) == "demo" and a.visual.kind == b.visual.kind == "footage":
            out.append(cut)
        else:
            out.append((TRANSITIONS[k % len(TRANSITIONS)], TRANSITION_S))
            k += 1
    fps = tl.fps  # whole frames, and never longer than the shot it blends into
    return [(kind, max(1, min(round(sec * fps), round(b.duration_s * fps) - 1)) / fps)
            for (kind, sec), b in zip(out, tl.shots[1:])]


def final(d: Path, tl: Timeline, music: TimelineMusic | None, trans: list[tuple[str, float]] | None = None) -> None:
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
    if trans:  # B29: each transition starts at the next shot's timeline start; shots carry a matching tail
        parts = [f"[{i}:v]fps={tl.fps},settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[t{i}]" for i in range(n)]
        prev = "t0"
        for i, (kind, sec) in enumerate(trans, 1):
            parts.append(f"[{prev}][t{i}]xfade=transition={kind}:duration={sec:.4f}:"
                         f"offset={tl.shots[i].start:.4f}[x{i}]")
            prev = f"x{i}"
        parts.append(f"[{prev}]null{subs}[vid]")
    else:
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
    trend = style_of(run) == "trend" and len(tl.shots) > 1
    trans = transitions(d, tl) if trend else None
    # +TAIL_SLACK_S: xfade ends the whole output if a shot runs out right at its transition (run-19c98fc7)
    tails = [sec + TAIL_SLACK_S for _, sec in trans] + [0.0] if trans else [0.0] * len(tl.shots)
    pans = [(PAN if i % 2 else -PAN) if trend else 0.0 for i in range(len(tl.shots))]
    with ThreadPoolExecutor(WORKERS) as ex:
        jobs = zip(tl.shots, outs, tails, pans)
        for s, _ in zip(tl.shots, ex.map(lambda j: shot(d, j[0], tl, j[1], j[2], j[3]), jobs)):
            events.emit("render", f"shot {s.shot_id} rendered")
    final(d, tl, music, trans)
    moving = sorted({k for k, sec in trans if sec > 1.5 / tl.fps}) if trans else []
    n_moving = sum(1 for _, sec in trans if sec > 1.5 / tl.fps) if trans else 0
    events.emit("render", f"final.mp4 {tl.duration_s:.1f} s at {tl.width}x{tl.height}, "
                + (f"trend style ({n_moving} moving transitions: {', '.join(moving)})" if trend
                   else "classic style (hard cuts)"))


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("render")
