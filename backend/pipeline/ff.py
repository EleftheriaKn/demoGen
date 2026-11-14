"""ffmpeg helpers (owner: B). Every shot is encoded with the same settings so the join never breaks.

Inputs can be anything a phone or laptop records: any size, portrait or landscape, rotation flags,
25/30/60 fps or variable frame rate, H.264 or HEVC, SDR or HDR, with or without sound.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "fonts"
FONT_NAME = "Noto Sans"

SAMPLE_RATE = 48000
VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO = ["-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2"]

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ and HLG (iPhone camera HDR)


class VideoInfo(NamedTuple):
    width: int        # as displayed, after the rotation flag
    height: int
    fps: float
    duration: float
    hdr: bool
    has_audio: bool


def size_for(mode: str) -> tuple[int, int]:
    """Output size: 720p previews in cheap mode, 1080p for real runs."""
    return (1280, 720) if mode == "cheap" else (1920, 1080)


def ff(*args, cwd: Path | None = None) -> None:
    """Run ffmpeg; on failure raise with ffmpeg's own error text instead of a bare exit code."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({p.returncode}): {p.stderr.strip()[-1500:]}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            return int(float(sd["rotation"]))
    return int(stream.get("tags", {}).get("rotate", 0))


def _rate(s: str) -> float:
    num, _, den = s.partition("/")
    return float(num) / float(den or 1) if float(den or 1) else 0.0


@lru_cache(maxsize=64)
def video_info(path: Path) -> VideoInfo:
    """Displayed size (rotation applied), frame rate, HDR flag. Images report fps 0."""
    p = probe(Path(path))
    v = next(s for s in p["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    if abs(_rotation(v)) % 180 == 90:
        w, h = h, w
    sar = v.get("sample_aspect_ratio", "1:1")
    if sar not in ("1:1", "0:1", "N/A") and ":" in sar:
        a, b = (int(x) for x in sar.split(":"))
        w = round(w * a / b)
    fps = _rate(v.get("avg_frame_rate", "0/1")) or _rate(v.get("r_frame_rate", "0/1"))
    dur = float(p.get("format", {}).get("duration") or v.get("duration") or 0)
    hdr = v.get("color_transfer") in HDR_TRANSFERS
    return VideoInfo(w, h, fps, dur, hdr, any(s.get("codec_type") == "audio" for s in p["streams"]))


@lru_cache(maxsize=1)
def _filters() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout


def has_filter(name: str) -> bool:
    return f" {name} " in _filters()


def hdr_to_sdr(info: VideoInfo) -> str:
    """Tone-map HDR phone footage to normal SDR colours, or nothing when SDR (or no zscale)."""
    if not info.hdr or not (has_filter("zscale") and has_filter("tonemap")):
        return ""
    return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")


def inner_size(src_w: int, src_h: int, w: int, h: int) -> tuple[int, int]:
    """Largest even size with the source's shape that fits inside w x h."""
    k = min(w / src_w, h / src_h)
    return max(2, round(src_w * k / 2) * 2), max(2, round(src_h * k / 2) * 2)


def place(src: str, out: str, iw: int, ih: int, w: int, h: int) -> str:
    """Filter graph: put [src] (already iw x ih) in the middle of a w x h frame.
    Same shape: just pad the rounding. Other shapes (portrait phone, 4:3): over a blurred, darker
    copy of itself instead of black bars."""
    if abs(iw / ih - w / h) < 0.02:
        return f"[{src}]scale={w}:{h},setsar=1,format=yuv420p[{out}]"
    bw, bh = w // 4, h // 4
    return (f"[{src}]split=2[{out}_bg][{out}_fg];"
            f"[{out}_bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"gblur=sigma=10,eq=brightness=-0.25:saturation=0.7,scale={w}:{h},setsar=1[{out}_b];"
            f"[{out}_b][{out}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[{out}]")
