"""Stage `captions` (owner: B).

One shot = one voice line, so each line is split into short cues spread over its voice take
(by character count). No word timestamps needed. Works for Greek and English.

Reads timeline.json, assets.json. Writes captions.srt (for players/uploads) and captions.ass (burned in).

Run alone: python -m backend.pipeline.captions runs/<run_id>
"""

from __future__ import annotations

import math
from pathlib import Path

from backend.contracts import Assets, RunConfig, Timeline
from backend.pipeline.ff import FONT_NAME

MAX_CHARS = 42       # one comfortable caption line
MIN_BREAK = 18       # break early at punctuation once a cue is this long
ORPHAN = 12          # a last cue shorter than this joins the one before it
PUNCT = ".,;:!?\u00b7\u037e"  # incl. Greek ano teleia and question mark
GAP_S = 0.05         # tiny gap between cues so players never show two at once


def chunks(text: str) -> list[str]:
    """Split a voice line into balanced cues of at most MAX_CHARS, breaking early at punctuation."""
    words = text.split()
    if not words:
        return []
    n = math.ceil(len(" ".join(words)) / MAX_CHARS)
    target = len(" ".join(words)) / n
    out, cur = [], ""
    for word in words:
        nxt = f"{cur} {word}" if cur else word
        if cur and (len(nxt) > MAX_CHARS or len(cur) >= target):
            out.append(cur)
            nxt = word
        cur = nxt
        if len(cur) >= MIN_BREAK and cur[-1] in PUNCT:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    if len(out) > 1 and len(out[-1]) < ORPHAN and len(out[-2]) + 1 + len(out[-1]) <= MAX_CHARS + ORPHAN:
        out[-2:] = [f"{out[-2]} {out[-1]}"]
    return out


def cues(tl: Timeline, voice_s: dict[str, float]) -> list[tuple[float, float, str, str]]:
    """(start, end, shot_id, text) for every cue, in order."""
    res = []
    for s in tl.shots:
        parts = chunks(s.caption)
        if not parts:
            continue
        speak = min(voice_s.get(s.voice_path, s.duration_s), s.duration_s)
        total = sum(len(p) for p in parts)
        t = s.start
        for i, p in enumerate(parts):
            end = s.start + s.duration_s if i == len(parts) - 1 else t + speak * len(p) / total
            res.append((t, max(t + 0.1, end - GAP_S), s.shot_id, p))
            t = end
    return res


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def ass_text(text: str) -> str:
    return text.replace("\\", "⧵").replace("{", "(").replace("}", ")")


def ass(tl: Timeline, cs: list[tuple[float, float, str, str]]) -> str:
    # Opaque-box style (BorderStyle 3): white text on a 35%-transparent black box, so captions read on
    # light screen recordings too. With BorderStyle 3, libass draws the box in OutlineColour; Outline = padding.
    size, pad, margin = round(tl.height * 0.05), max(4, round(tl.height / 90)), round(tl.height * 0.07)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {tl.width}
PlayResY: {tl.height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{size},&H00FFFFFF,&H000000FF,&H59000000,&H59000000,1,0,0,0,100,100,0,0,3,{pad},0,2,{margin},{margin},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,{sid},0,0,0,,{ass_text(t)}" for a, b, sid, t in cs]
    return head + "\n".join(lines) + "\n"


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir)
    tl = Timeline.model_validate_json((d / "timeline.json").read_text())
    assets = Assets.model_validate_json((d / "assets.json").read_text())
    cs = cues(tl, {v.path: v.duration_s for v in assets.voice})
    srt = "\n".join(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n" for i, (a, b, _, t) in enumerate(cs, 1))
    (d / "captions.srt").write_text(srt, encoding="utf-8")
    (d / "captions.ass").write_text(ass(tl, cs), encoding="utf-8")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("captions")
