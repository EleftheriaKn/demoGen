"""Card images (owner: B, ticket B11). "fal draws, we write": A's fal backgrounds carry no text; render adds it.

A card shot becomes: the background (fal card, or plain dark), the app name, and a real frame from the
recording in a device mockup (browser window for landscape takes, phone for portrait). The frame is the one
the judge scored best (frame_scores.json), re-extracted at full resolution. The ask card may add the repo
address. Nothing on a card is a claim: name, address and a real frame. Greek and English (Noto Sans).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from backend.contracts import frame_time
from backend.pipeline.ff import FONTS, ff, hdr_to_sdr, video_info

FONT = FONTS / "NotoSans-Bold.ttf"
BG_PLAIN = (17, 24, 39)
SUBSTITUTE = {"→": "›", "⇒": "›", "➜": "›", "⟶": "›", "➔": "›", "←": "‹", "⇐": "‹", "↔": "‹›",
              "✓": "•", "✔": "•", "✅": "•", "★": "•", "☆": "•"}
_NOTDEF: dict = {}


def _has_glyph(ch: str) -> bool:
    f = _font(40)
    if "m" not in _NOTDEF:
        m = f.getmask("\U0010FFFF")
        _NOTDEF["m"] = (m.getbbox(), bytes(m))
    m = f.getmask(ch)
    return (m.getbbox(), bytes(m)) != _NOTDEF["m"]


def font_safe(text: str) -> str:
    """Text our font can draw: arrows / check marks become ›, ‹, •; other missing glyphs are dropped.
    Noto Sans Bold has Latin and Greek but no arrows or dingbats, which would render as boxes."""
    out = []
    for ch in text:
        if ch.isspace() or ch.isascii() or _has_glyph(ch):
            out.append(ch)
        elif ch in SUBSTITUTE:
            out.append(SUBSTITUTE[ch])
    return " ".join("".join(out).split())


def best_frames(d: Path, n: int = 2) -> list[float]:
    """Recording times of the n best-scored frames, one per segment (frame_scores.json, else candidates)."""
    times, segs = [], set()
    fs = d / "frame_scores.json"
    if fs.exists():
        scores = sorted(json.loads(fs.read_text(encoding="utf-8"))["scores"], key=lambda s: -s["total"])
        for s in scores:
            if s["segment_id"] not in segs:
                segs.add(s["segment_id"])
                times.append(frame_time(s["frame"]))
            if len(times) == n:
                return times
    cp = d / "candidates.json"
    if cp.exists():
        for g in json.loads(cp.read_text(encoding="utf-8"))["segments"]:
            if g["source"] == "moment" and g["segment_id"] not in segs:
                segs.add(g["segment_id"])
                times.append(frame_time(g["frames"][1]))
            if len(times) == n:
                break
    return times


def grab_frame(recording: Path, t: float, out: Path) -> Path:
    """Full-resolution frame at t (rotation and HDR handled), as PNG."""
    out.parent.mkdir(parents=True, exist_ok=True)
    hdr = hdr_to_sdr(video_info(recording))
    ff("-ss", f"{t:.3f}", "-i", recording, "-frames:v", "1", *(["-vf", hdr.rstrip(",")] if hdr else []), out)
    return out


def _font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), px)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: int, px: int) -> ImageFont.FreeTypeFont:
    while px > 12 and draw.textlength(text, font=_font(px)) > max_w:
        px -= 2
    return _font(px)


def _shadow(size: tuple[int, int], box: tuple[int, int, int, int], radius: int, blur: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=(0, 0, 0, 120))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def mockup(frame: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """The frame inside a browser window (landscape) or a phone (portrait), with rounded corners."""
    portrait = frame.height > frame.width
    if portrait:
        bezel = max(8, max_h // 45)
        inner_h = max_h - 2 * bezel
        inner_w = round(frame.width * inner_h / frame.height)
        if inner_w + 2 * bezel > max_w:
            inner_w = max_w - 2 * bezel
            inner_h = round(frame.height * inner_w / frame.width)
        w, h, top, side = inner_w + 2 * bezel, inner_h + 2 * bezel, bezel, bezel
        radius = bezel * 4
    else:
        bar = max(14, max_h // 18)
        inner_w = max_w
        inner_h = round(frame.height * inner_w / frame.width)
        if inner_h + bar > max_h:
            inner_h = max_h - bar
            inner_w = round(frame.width * inner_h / frame.height)
        w, h, top, side = inner_w, inner_h + bar, bar, 0
        radius = max(8, bar // 2)
    shell = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shell)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=(20, 20, 22, 255) if portrait else (236, 236, 238, 255))
    if not portrait:  # window buttons
        r = max(3, top // 6)
        for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            cx = top // 2 + i * 3 * r
            draw.ellipse((cx - r, top // 2 - r, cx + r, top // 2 + r), fill=col)
    screen = frame.convert("RGBA").resize((inner_w, inner_h), Image.LANCZOS)
    mask = Image.new("L", (inner_w, inner_h), 0)
    if portrait:  # phone screen: all corners rounded inside the bezel
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, inner_w - 1, inner_h - 1), radius - bezel, fill=255)
    else:  # browser: square under the title bar, rounded bottom corners
        ImageDraw.Draw(mask).rounded_rectangle((0, -radius, inner_w - 1, inner_h - 1), radius, fill=255)
    shell.paste(screen, (side, top), mask)
    return shell


def hero_card(out: Path, size: tuple[int, int], title: str, background: Path | None = None,
              frame: Path | None = None, subtitle: str = "") -> Path:
    """Compose one card PNG: background, title, optional framed real screen, optional subtitle."""
    w, h = size
    title, subtitle = font_safe(title), font_safe(subtitle)
    if background and background.exists():
        bg = Image.open(background).convert("RGB")
        k = max(w / bg.width, h / bg.height)
        bg = bg.resize((round(bg.width * k), round(bg.height * k)), Image.LANCZOS)
        x0, y0 = (bg.width - w) // 2, (bg.height - h) // 2
        img = bg.crop((x0, y0, x0 + w, y0 + h)).convert("RGBA")
        img = Image.alpha_composite(img, Image.new("RGBA", (w, h), (0, 0, 0, 70)))
    else:
        img = Image.new("RGBA", (w, h), BG_PLAIN + (255,))
    draw = ImageDraw.Draw(img)
    pad = round(h * 0.06)

    if frame is None:  # title alone, centred
        f = _fit_text(draw, title, w - 2 * pad, round(h * 0.11))
        draw.text((w / 2, h / 2), title, font=f, fill="white", anchor="mm", stroke_width=max(1, h // 400),
                  stroke_fill=(0, 0, 0, 90))
    else:
        title_px = round(h * 0.075)
        f = _fit_text(draw, title, w - 2 * pad, title_px)
        draw.text((w / 2, pad + title_px / 2), title, font=f, fill="white", anchor="mm",
                  stroke_width=max(1, h // 400), stroke_fill=(0, 0, 0, 90))
        sub_h = round(h * 0.07) if subtitle else 0
        top = pad + title_px + pad // 2
        box_h = h - top - pad - sub_h - round(h * 0.12)  # leave room for the captions at the bottom
        dev = mockup(Image.open(frame), round(w * 0.62), box_h)
        x, y = (w - dev.width) // 2, top
        radius = max(8, h // 60)
        img = Image.alpha_composite(img, _shadow((w, h), (x, y + h // 90, x + dev.width, y + dev.height + h // 90),
                                                radius, max(6, h // 70)))
        img.alpha_composite(dev, (x, y))
        if subtitle:
            draw = ImageDraw.Draw(img)
            sf = _fit_text(draw, subtitle, w - 2 * pad, round(h * 0.04))
            draw.text((w / 2, y + dev.height + sub_h / 2 + pad // 3), subtitle, font=sf, fill=(235, 235, 235),
                      anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out


def rasterize(src: Path, out: Path, size: tuple[int, int]) -> Path:
    """SVG (fal Ideogram Vector) -> PNG fitted inside size; other images are returned as they are."""
    if src.suffix.lower() != ".svg":
        return src
    w, h = size
    out.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsvg-convert"):  # keeps the drawing's aspect ratio at our size
        subprocess.run(["rsvg-convert", "-a", "-w", str(w), "-h", str(h), "-o", str(out), str(src)], check=True)
        return out
    try:  # ffmpeg built with librsvg (most Linux builds, the Windows "full" builds) reads SVG too
        ff("-i", src, "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease", "-frames:v", "1", out)
        return out
    except RuntimeError:
        raise RuntimeError(f"can't rasterize {src.name}: install rsvg-convert (librsvg) or an ffmpeg with librsvg") \
            from None


def _halftone(size: tuple[int, int], step: int, alpha: int) -> Image.Image:
    """A faint dot grid: the 'printed panel' texture behind generated art."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = max(1, step // 5)
    for y in range(0, size[1], step):
        for x in range((y // step % 2) * step // 2, size[0], step):
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, alpha))
    return layer


def panel_card(out: Path, size: tuple[int, int], art: Path | None, tag: str = "Illustration") -> Path:
    """A generated shot (fal panel / infographic) as a designed panel: the art fitted in a rounded frame with a
    white border and shadow, over a blurred, darkened copy of itself with a faint halftone, and a small tag
    so viewers know it is an illustration, not footage (honesty rule)."""
    w, h = size
    pad, radius, border = round(h * 0.07), max(10, h // 45), max(3, h // 220)
    if art and art.exists():
        src = Image.open(art).convert("RGB")
        k = max(w / src.width, h / src.height)
        bg = src.resize((round(src.width * k), round(src.height * k)), Image.LANCZOS)
        x0, y0 = (bg.width - w) // 2, (bg.height - h) // 2
        bg = bg.crop((x0, y0, x0 + w, y0 + h)).filter(ImageFilter.GaussianBlur(h // 30)).convert("RGBA")
        bg = Image.alpha_composite(bg, Image.new("RGBA", (w, h), (10, 12, 20, 150)))
    else:
        src, bg = None, Image.new("RGBA", (w, h), BG_PLAIN + (255,))
    img = Image.alpha_composite(bg, _halftone((w, h), max(10, h // 70), 18))
    if src is not None:
        k = min((w - 2 * pad) / src.width, (h - 2 * pad - round(h * 0.08)) / src.height)
        pw, ph = round(src.width * k), round(src.height * k)
        x, y = (w - pw) // 2, pad
        img = Image.alpha_composite(img, _shadow((w, h), (x, y + h // 90, x + pw, y + ph + h // 90), radius, h // 60))
        frame = Image.new("RGBA", (pw + 2 * border, ph + 2 * border), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rounded_rectangle((0, 0, frame.width - 1, frame.height - 1), radius + border,
                                                fill=(255, 255, 255, 255))
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), radius, fill=255)
        frame.paste(src.resize((pw, ph), Image.LANCZOS).convert("RGBA"), (border, border), mask)
        img.alpha_composite(frame, (x - border, y - border))
    if tag:
        _draw_tag(img, tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out


def _draw_tag(img: Image.Image, tag: str) -> None:
    """The small "Illustration" pill in the top-left corner (same place on panels and broll clips)."""
    h = img.height
    d, pad = ImageDraw.Draw(img), round(h * 0.07)
    f = _font(max(12, round(h * 0.024)))
    tw = d.textlength(tag, font=f)
    tx, ty, ph_ = pad // 2, pad // 2, round(h * 0.04)
    d.rounded_rectangle((tx, ty, tx + tw + ph_, ty + ph_), ph_ // 2, fill=(0, 0, 0, 150))
    d.text((tx + ph_ / 2, ty + ph_ / 2), tag, font=f, fill=(255, 255, 255, 230), anchor="lm")


def tag_overlay(out: Path, size: tuple[int, int], tag: str = "Illustration") -> Path:
    """A transparent w x h PNG with only the tag on it, for render to lay over generated video (B16)."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    _draw_tag(img, tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
