"""Stage `timeline` (owner: B).

Fit footage to voice durations: one shot per script shot, as long as its voice line plus a breath.

Reads script.json, assets.json, candidates.json (segment bounds), summary.json (app name for plain
cards), the recording. Writes timeline.json.

Run alone: python -m backend.pipeline.timeline runs/<run_id>
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from backend import events
from backend.contracts import (Assets, BrollAsset, Candidates, FrameScores, Moments, RepoFiles, RunConfig, Script, Summary, Timeline, TimelineMusic,
                               TimelineShot, TimelineVisual, Zoom)
from backend.pipeline.beats import bar_loop, beat_times, snap
from backend.pipeline.cards import best_frames, grab_frame, hero_card, panel_card, rasterize
from backend.pipeline.ff import duration, ff, size_for, take_path

FPS = 30
BREATH_S = 0.25      # footage keeps running this long after the voice ends
MAX_SPEED = 1.5      # contract limit
MIN_BROLL_SPEED = 0.75  # generated clips may play this slow before their last frame is held
HERO_BEATS = {"hook", "ask"}  # these cards get the hero look; other generated shots are illustration panels
MAX_FREEZE_S = 1.5   # longer gaps play more real footage instead of a held frame...
MAX_BEYOND_S = 0.0   # ...but never from outside the segment: the next moment can contradict the voice line
                     # (B9: "Active filter" line over the Completed view). Render moves held frames instead.
                     # A shot with no claims (the fact-free problem line) states nothing footage can contradict:
                     # it plays on past its segment (inside its room) rather than freeze (Coach Platform: 15.7 s hold).
ZOOM_EASE_S = 0.8
MUSIC_GAIN_DB = -6.0  # was -18: the bed (~-16 dB) ended up at ~-36 dB, inaudible (run-19c98fc7)
MAX_BEAT_SHIFT_S = 0.25  # B18: a cut moves at most this far (earlier or later) to land on a beat
MIN_TAIL_S = 0.1     # ...and never lands sooner than this after the voice ends
LOOP_FILE = "music_loop.wav"
MONTAGE_MAX = 6       # B28 (trend style): up to this many context clips open the video, cut on the beat
MONTAGE_MIN = 3       # fewer context clips than this: no montage
MONTAGE_BEATS = 2     # each montage clip lasts this many beats (1 s at 120 BPM)
MONTAGE_CLIP_S = 1.0  # ...or this long when the music has no beat
MONTAGE_SPEEDS = (1.0, 1.3)  # alternate: a little speed ramp feel without leaving the contract limit  # the bed cut to whole bars (render loops it)
READ_WPS = 2.6       # a shot whose voice take failed stays up as long as its caption takes to read
MIN_SILENT_S = 2.0


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


def fit_broll(length: float, target: float) -> tuple[float, float, float, float]:
    """A generated clip (A17: 4 s) under a longer voice line plays slower first (gentle motion stays
    natural down to MIN_BROLL_SPEED), and only holds its last frame for what is still missing."""
    if length >= target:
        return fit_source(0.0, length, target, length)
    speed = max(MIN_BROLL_SPEED, length / target)
    return 0.0, length, round(speed, 4), round(max(0.0, target - length / speed), 3)


def grow(start: float, end: float, need: float, lo: float, hi: float) -> tuple[float, float]:
    """Widen [start, end] by up to `need` seconds inside [lo, hi]: forward first (what happens next), then back."""
    fwd = min(need, max(0.0, hi - end))
    back = min(need - fwd, max(0.0, start - lo))
    return start - back, end + fwd


def fit_footage(start: float, end: float, target: float, segment: tuple[float, float] | None,
                limit: float | None, room: tuple[float, float] = (0.0, float("inf")),
                beyond: float = MAX_BEYOND_S) -> tuple[float, float, float, float]:
    """Like fit_source, but a voice line longer than its clip plays more real footage instead of a long
    freeze: first the rest of the candidate segment (same content), then at most ``beyond`` seconds of the
    footage around it (it may already show something the voice line doesn't say), never past `room`
    (where the previous footage shot ended / the next one starts, so nothing plays twice). What is still
    missing is a held last frame: the matching screen stays up while the voice finishes."""
    lo, hi = max(0.0, room[0]), min(limit if limit is not None else float("inf"), room[1])
    end = min(end, limit) if limit is not None else end
    if target > end - start and segment is not None:
        start, end = grow(start, end, target - (end - start), max(lo, segment[0]), min(hi, segment[1]))
    if target - (end - start) > MAX_FREEZE_S:  # a little footage from around the segment, never much:
        need = min(target - (end - start) - MAX_FREEZE_S / 2, beyond)  # it may show something else
        start, end = grow(start, end, need, lo, hi)
    return fit_source(round(start, 3), round(end, 3), target, limit)


def rooms(spans: list[tuple[float, float] | None]) -> list[tuple[float, float]]:
    """For each footage span (None = not footage): how far it may grow without reaching the original span
    of the footage shot played just before or after it. Overlaps the script already has are left alone."""
    out = []
    for i, sp in enumerate(spans):
        if sp is None:
            out.append((0.0, float("inf")))
            continue
        prev = next((spans[j] for j in range(i - 1, -1, -1) if spans[j]), None)
        nxt = next((spans[j] for j in range(i + 1, len(spans)) if spans[j]), None)
        lo = prev[1] if prev and prev[1] <= sp[0] else 0.0
        hi = nxt[0] if nxt and nxt[0] >= sp[1] else float("inf")
        out.append((lo, hi))
    return out


def silent_voice(d: Path, shot_id: str, line: str) -> tuple[str, float]:
    """Stand-in for a voice take that failed: silence as long as the line takes to read."""
    seconds = round(max(MIN_SILENT_S, len(line.split()) / READ_WPS), 2)
    rel = f"voice/silent/{shot_id}.wav"
    (d / rel).parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", f"{seconds:.2f}", d / rel)
    return rel, seconds


def usable_broll(d: Path, b: BrollAsset | None) -> BrollAsset | None:
    """The broll clip for a shot if its file is really there, else None (the shot becomes a panel / card)."""
    if b is None:
        return None
    if not (d / b.path).is_file() or (d / b.path).stat().st_size == 0:
        events.emit("timeline", f"{b.shot_id}: broll {b.path} is missing, using the panel instead")
        return None
    return b


def app_name(d: Path) -> str:
    """The app's name for a plain card, from summary.json; empty when understand hasn't run."""
    try:
        return Summary.model_validate_json((d / "summary.json").read_text(encoding="utf-8")).name
    except (OSError, ValueError):
        return ""


def plain_card(d: Path, shot_id: str, title: str, size: tuple[int, int], background: str | None = None,
               frame: Path | None = None, subtitle: str = "") -> str:
    """A card image in our own hands (B11, see cards.py): A's fal background (or plain dark), the app name,
    and optionally a real frame in a device mockup and a subtitle. No claims on it."""
    rel = f"cards/{'titled' if background else 'plain'}/{shot_id}.png"
    hero_card(d / rel, size, title, d / background if background else None, frame, subtitle)
    return rel


def style_of(run: RunConfig) -> str:
    """'trend' (default) or 'classic' (B29 fallback: hard cuts, no montage). DEMOCUT_STYLE overrides run.json."""
    return os.environ.get("DEMOCUT_STYLE") or run.style or "trend"


def silence(d: Path, rel: str, seconds: float) -> str:
    """A silent voice file (montage shots have music only)."""
    (d / rel).parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", f"{seconds:.3f}", d / rel)
    return rel


def montage(d: Path, run: RunConfig, script: Script, segments: dict, beats: list[float]) -> list[TimelineShot]:
    """B28: the opening montage of a trend-style video: the best-scored context clips (the team, people, the
    place: B25's `context` moments, never the app's screen), not used by any script shot, a few beats each,
    cut on the beat, music only. Nothing when the take has fewer than MONTAGE_MIN context clips."""
    mp, cp = d / "moments.json", d / "candidates.json"
    if not (mp.exists() and cp.exists()):
        return []
    ctx = [(m.start, m.end) for m in Moments.model_validate_json(mp.read_text(encoding="utf-8")).moments
           if m.kind == "context"]
    used = [(s.visual.source_start, s.visual.source_end) for s in script.shots
            if s.visual.kind == "footage" and s.visual.source_start is not None]
    inside = lambda a, b, spans: sum(max(0.0, min(b, e) - max(a, s)) for s, e in spans)  # noqa: E731
    all_segs = Candidates.model_validate_json(cp.read_text(encoding="utf-8")).segments
    in_ctx = [g for g in all_segs if inside(g.start, g.end, ctx) >= 0.5 * (g.end - g.start)]
    # scene clips are the short pieces of a context stretch; a whole-moment clip only when it has none inside
    scenes = [g for g in in_ctx if g.source == "scene"]
    pool = scenes + [g for g in in_ctx if g.source == "moment"
                     and not any(g.start <= x.start and x.end <= g.end for x in scenes)]
    segs = []
    for g in sorted(pool, key=lambda g: g.start):  # the clip's middle must be footage nothing else plays
        mid = (g.start + g.end) / 2
        if not inside(mid - 1.0, mid + 1.0, used) and not any(abs((x.start + x.end) / 2 - mid) < 2.0 for x in segs):
            segs.append(g)
    if len(segs) < MONTAGE_MIN:
        return []
    fp = d / "frame_scores.json"
    best: dict[str, float] = {}
    if fp.exists():
        for f in FrameScores.model_validate_json(fp.read_text(encoding="utf-8")).scores:
            best[f.segment_id] = max(best.get(f.segment_id, 0.0), f.total)
    picks = sorted(sorted(segs, key=lambda g: -best.get(g.segment_id, 0.0))[:MONTAGE_MAX], key=lambda g: g.start)
    clip = MONTAGE_BEATS * (beats[1] - beats[0]) if len(beats) > 1 else MONTAGE_CLIP_S
    shots, t = [], 0.0
    for i, g in enumerate(picks):
        speed = MONTAGE_SPEEDS[i % len(MONTAGE_SPEEDS)]
        end_t = beats[min(range(len(beats)), key=lambda k: abs(beats[k] - (t + clip)))] if beats else t + clip
        dur = frames(max(0.3, end_t - t))
        length = g.end - g.start
        if length >= dur * speed:  # the middle of the clip, at the ramp speed
            span, freeze = dur * speed, 0.0
        elif length >= dur:  # a bit short: just fast enough to fill the beats
            span, speed, freeze = length, length / dur, 0.0
        else:  # shorter than its beats: play it, hold the last frame
            span, speed, freeze = length, 1.0, dur - length
        a = round(g.start + (length - span) / 2, 3)
        sid = f"m{i + 1:02d}"
        shots.append(TimelineShot(
            shot_id=sid, start=round(t, 3), duration_s=dur,
            voice_path=silence(d, f"voice/silent/{sid}.wav", dur),
            visual=TimelineVisual(kind="footage", path=run.recording, source_start=a, source_end=round(a + span, 3),
                                  speed=round(speed, 4), freeze_s=round(freeze, 3)),
            caption=""))
        t = round(t + dur, 6)
    events.emit("timeline", f"montage: {len(shots)} context clips, {t:.1f} s, cut on the beat")
    return shots


def take_shows_app(d: Path) -> bool:
    """False when no moment of the take shows the app (all "context" / "dead": a camera video of the team): its
    frames must not sit in a browser mockup as if they were the app. No moments.json: assume it does."""
    mp = d / "moments.json"
    if not mp.exists():
        return True
    moments = Moments.model_validate_json(mp.read_text(encoding="utf-8")).moments
    return any(m.kind not in ("context", "dead") for m in moments)


def repo_screenshots(d: Path) -> list[Path]:
    """README screenshots ingest downloaded: the real screen when there is no take."""
    rp = d / "repo_files.json"
    if not rp.exists():
        return []
    shots = RepoFiles.model_validate_json(rp.read_text(encoding="utf-8")).screenshots
    return [d / p for p in shots if (d / p).exists()][:2]


def card_frames(d: Path, recording: Path, n: int) -> list[Path]:
    """Full-resolution copies of the n best-scored frames, for the card mockups ([] if none or it fails)."""
    try:
        return [grab_frame(recording, t, d / "cards" / "frames" / f"best_{i + 1}.png")
                for i, t in enumerate(best_frames(d, n))]
    except Exception as e:  # cards are decoration: a failed grab must never stop the video
        events.emit("timeline", f"card frames skipped: {e}")
        return []


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir)
    script = Script.model_validate_json((d / "script.json").read_text(encoding="utf-8"))
    assets = Assets.model_validate_json((d / "assets.json").read_text(encoding="utf-8"))
    take = take_path(d, run.recording)  # None: no take (B17), every shot is generated
    rec_len = duration(take) if take else None

    voices: dict[str, tuple[str, float]] = {}
    for v in assets.voice:  # prefer the script's language; take any take as a fallback
        if v.shot_id not in voices or v.language == script.language:
            voices[v.shot_id] = (v.path, v.duration_s)
    cards = {c.shot_id: c.path for c in assets.cards}
    cpath = d / "candidates.json"
    cands = Candidates.model_validate_json(cpath.read_text(encoding="utf-8")) if cpath.exists() else None
    segments = {g.segment_id: (g.start, g.end) for g in cands.segments} if cands else {}
    brolls = {b.shot_id: b for b in assets.broll}

    music_file = d / assets.music.path if assets.music else None
    beats, bpm = beat_times(music_file) if music_file and music_file.exists() else ([], 0.0)
    music_path = assets.music.path if assets.music else None
    if beats:  # render loops the bed: loop whole bars from the first beat, so the beat never jumps at the seam
        horizon = 2 * sum(v[1] for v in voices.values()) + 120
        grid = bar_loop(music_file, d / LOOP_FILE, beats, bpm, horizon)
        if grid:
            beats, music_path = grid, LOOP_FILE
        else:  # bed shorter than a bar: its own beats, music plays once
            events.emit("timeline", "music bed shorter than one bar: no loop on the beat")
    name = app_name(d) or run.repo_url.rstrip("/").rsplit("/", 1)[-1]
    repo_label = re.sub(r"^https?://(www\.)?", "", run.repo_url).rstrip("/")
    n_card_shots = sum(1 for s in script.shots if s.visual.kind != "footage" or take is None)
    if not n_card_shots:
        hero = []
    elif take and take_shows_app(d):  # the judge's best frames of the take
        hero = card_frames(d, take, min(n_card_shots, 2))
    else:  # no take, or a take that never shows the app: the repo's own screenshots stand in for the screen
        hero = repo_screenshots(d)
    n_cards = 0
    spans = [(s.visual.source_start, s.visual.source_end) if s.visual.kind == "footage" else None
             for s in script.shots]
    shots, t, last_end = [], 0.0, None
    if take and style_of(run) == "trend":  # B28: open on the team, cut to the music
        shots = montage(d, run, script, segments, beats)
        t = round(shots[-1].start + shots[-1].duration_s, 6) if shots else 0.0
    for s, room in zip(script.shots, rooms(spans)):
        if s.shot_id not in voices:  # fal failed for this take (assets leaves it out): silent, captions only
            voices[s.shot_id] = silent_voice(d, s.shot_id, s.voice_line)
            events.emit("timeline", f"{s.shot_id}: no voice take, playing it silent with captions "
                                    f"({voices[s.shot_id][1]:.1f} s)")
        voice_path, voice_s = voices[s.shot_id]
        dur = frames(voice_s + BREATH_S)
        if beats:  # B18: end the shot on the nearest beat within +-MAX_BEAT_SHIFT_S, never before the voice is done
            reach = MAX_BEAT_SHIFT_S - 0.5 / FPS  # the cut is then rounded to the nearest frame: stays within reach
            cut = snap(t + dur, max(t + voice_s + MIN_TAIL_S, t + dur - reach), beats, reach)
            if cut != t + dur:
                dur = round((cut - t) * FPS) / FPS
        vis = s.visual
        if vis.kind == "footage" and take:
            if last_end is not None and room[0] < last_end <= vis.source_start:
                room = (last_end, room[1])  # the shot before grew: don't replay what it showed
            start, end, speed, freeze = fit_footage(vis.source_start, vis.source_end, dur,
                                                    segments.get(vis.segment_id), rec_len, room,
                                                    MAX_BEYOND_S if s.claims else float("inf"))
            last_end = end
            zoom = Zoom(box=vis.focus_box, ease_s=ZOOM_EASE_S) if vis.focus_box else None
            visual = TimelineVisual(kind="footage", path=run.recording, source_start=start, source_end=end,
                                    speed=speed, freeze_s=freeze, zoom=zoom)
        elif vis.kind != "footage" and (b := usable_broll(d, brolls.get(s.shot_id))):
            # B16: a fal clip (A17) for a generated shot, whatever kind the script gave it: speed up to 1.5x,
            # or slow down to MIN_BROLL_SPEED and then hold its last frame (render pushes in on long holds);
            # a missing clip falls to the panel below
            start, end, speed, freeze = fit_broll(b.duration_s, dur)
            visual = TimelineVisual(kind="broll", path=b.path, source_start=start, source_end=end,
                                    speed=speed, freeze_s=freeze)
        else:  # a card (or footage with no take)
            bg = cards[s.shot_id] if s.shot_id in cards and (d / cards[s.shot_id]).exists() else None
            if bg is None and vis.kind != "footage" and s.shot_id not in brolls:  # (broll: said above)
                events.emit("timeline", f"{s.shot_id}: no {vis.kind} file in assets.json, using a plain card")
            size = size_for(run.mode)
            if bg and Path(bg).suffix.lower() == ".svg":  # Ideogram Vector art (A16): rasterize for ffmpeg
                bg = str(rasterize(d / bg, d / "cards" / "raster" / f"{s.shot_id}.png", size).relative_to(d))
            if s.beat in HERO_BEATS or not bg:  # hook / ask, or nothing generated: our hero card
                frame = hero[n_cards % len(hero)] if hero else None
                subtitle = ""  # never the repo URL on the video (user, Nov 15)
                path = plain_card(d, s.shot_id, name, size, bg, frame, subtitle)
                n_cards += 1
            else:  # a generated panel for a weak / missing step (v2): the illustration look
                path = str(panel_card(d / "cards" / "panel" / f"{s.shot_id}.png", size, d / bg).relative_to(d))
            visual = TimelineVisual(kind="card", path=path)
        shots.append(TimelineShot(shot_id=s.shot_id, start=round(t, 3), duration_s=dur,
                                  voice_path=voice_path, visual=visual, caption=s.voice_line,
                                  callout=(s.callout or "").strip() or None))
        t = round(t + dur, 6)

    music = TimelineMusic(path=music_path, gain_db=MUSIC_GAIN_DB, duck=True) if assets.music else None
    w, h = size_for(run.mode)
    tl = Timeline(fps=FPS, width=w, height=h, duration_s=round(t, 3), shots=shots, music=music)
    (d / "timeline.json").write_text(tl.model_dump_json(indent=2), encoding="utf-8")

    limit = run.brief.time_limit_s
    if beats:
        on_beat = sum(1 for x in shots if min(abs(x.start + x.duration_s - b) for b in beats) <= 1.01 / FPS)
        events.emit("timeline", f"music at {bpm:.0f} BPM: {on_beat} of {len(shots)} cuts on the beat"
                    + (f", looped in whole bars ({music_path})" if music_path == LOOP_FILE else ""))
    events.emit("timeline", f"{len(shots)} shots, {tl.duration_s:.1f} s at {w}x{h}"
                + (f" (over the {limit} s limit!)" if tl.duration_s > limit else ""))


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("timeline")
