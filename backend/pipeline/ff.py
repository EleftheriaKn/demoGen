"""ffmpeg helpers (owner: B). Every shot is encoded with the same settings so the join never breaks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "fonts"
FONT_NAME = "Noto Sans"

SAMPLE_RATE = 48000
VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
AUDIO = ["-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE), "-ac", "2"]


def size_for(mode: str) -> tuple[int, int]:
    """Output size: 720p previews in cheap mode, 1080p for real runs."""
    return (1280, 720) if mode == "cheap" else (1920, 1080)


def fit(w: int, h: int, fps: int) -> str:
    """Scale any input into w x h (letterboxed, never stretched) at a constant frame rate."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")


def ff(*args, cwd: Path | None = None) -> None:
    """Run ffmpeg; on failure raise with ffmpeg's own error text instead of a bare exit code."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({p.returncode}): {p.stderr.strip()[-1500:]}")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))
