"""fal client (owner: A). Needs FAL_KEY in the environment.

One thin function per endpoint; each makes one call and writes or returns the result.
No cache or retry here: callers wrap it in ``cache.file_call`` + ``cache.retry``.
Field names below were checked 2026-11-15 against fal's OpenAPI schema and one real call each.

"fal draws, we write": image prompts must ask for no text in the picture.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import fal_client

from backend.clients.gemini import MissingKey, explain, require  # noqa: F401  (MissingKey: callers catch fal.MissingKey)

TTS_ENDPOINT = "fal-ai/elevenlabs/tts/multilingual-v2"
CARD_ENDPOINT = "fal-ai/flux-2"
CARD_EDIT_ENDPOINT = "fal-ai/nano-banana-2/edit"
MUSIC_ENDPOINT = "fal-ai/elevenlabs/music"
SCRIBE_ENDPOINT = "fal-ai/elevenlabs/speech-to-text/scribe-v2"
VECTOR_REDRAW_ENDPOINT = "ideogram/vector/image-to-vector"  # Ideogram Vector 1.0; ids have no "fal-ai/" prefix
VECTOR_SCENE_ENDPOINT = "ideogram/vector/text-to-vector"
DOWNLOAD_TIMEOUT_S = 60


def has_key() -> bool:
    """True if fal credentials are set (FAL_KEY, or FAL_KEY_ID + FAL_KEY_SECRET)."""
    return bool(os.environ.get("FAL_KEY") or (os.environ.get("FAL_KEY_ID") and os.environ.get("FAL_KEY_SECRET")))


def _key() -> None:
    """Raise MissingKey before any fal call when no fal credentials are set."""
    if not has_key():
        require("FAL_KEY")


def failed(e: BaseException) -> RuntimeError:
    """Plain message for a fal error left after ``cache.retry``."""
    return explain("fal", e, "FAL_KEY")


def _download(url: str, out_path: Path) -> None:
    """A stalled download raises (socket timeout) instead of hanging, so ``cache.retry`` retries it."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as r:
        Path(out_path).write_bytes(r.read())


def tts(text: str, language: str, out_path: Path) -> Path:
    """``language`` is a BCP-47 code ('en', 'el'); the voice is the endpoint's default."""
    # input accepts language_code, output is {"audio": {"url": ...}}.
    _key()
    result = fal_client.subscribe(TTS_ENDPOINT, arguments={"text": text, "language_code": language})
    _download(result["audio"]["url"], out_path)
    return Path(out_path)


def card_image(prompt: str, out_path: Path) -> Path:
    """FLUX 2 text-to-image, 16:9. Output: {"images": [{"url", "width", "height", ...}], "seed", ...}."""
    _key()
    result = fal_client.subscribe(CARD_ENDPOINT, arguments={"prompt": prompt, "image_size": "landscape_16_9"})
    _download(result["images"][0]["url"], out_path)
    return Path(out_path)


def card_edit(prompt: str, reference_images: list[Path], out_path: Path) -> Path:
    """Nano Banana 2 edit: prompt + local reference frames (uploaded to fal). Output: {"images": [{"url"}], "description"}."""
    _key()
    urls = [fal_client.upload_file(str(p)) for p in reference_images]
    result = fal_client.subscribe(
        CARD_EDIT_ENDPOINT, arguments={"prompt": prompt, "image_urls": urls, "aspect_ratio": "16:9"}
    )
    _download(result["images"][0]["url"], out_path)
    return Path(out_path)


def vector_redraw(image: Path, out_path: Path) -> Path:
    """Ideogram Vector image-to-vector, "simple" mode: one real frame redrawn flat. No prompt input.
    Output: {"images": [{"url", "content_type", ...}], "seed"}; the file is SVG."""
    _key()
    url = fal_client.upload_file(str(image))
    result = fal_client.subscribe(VECTOR_REDRAW_ENDPOINT, arguments={"image_url": url, "mode": "simple"})
    _download(result["images"][0]["url"], out_path)
    return Path(out_path)


def vector_scene(prompt: str, reference_images: list[Path], out_paths: list[Path], seed: int) -> list[Path]:
    """Ideogram Vector text-to-vector, 16:9, one image per ``out_paths`` (max 8) from one call.
    ``reference_images``: 1..5 local frames, the first is the primary source. Output as vector_redraw."""
    _key()
    urls = [fal_client.upload_file(str(p)) for p in reference_images[:5]]
    args = {"prompt": prompt, "image_urls": urls, "aspect_ratio": "16:9", "num_images": len(out_paths), "seed": seed}
    result = fal_client.subscribe(VECTOR_SCENE_ENDPOINT, arguments=args)
    for img, out in zip(result["images"], out_paths):
        _download(img["url"], out)
    return [Path(o) for o in out_paths[: len(result["images"])]]


def music(prompt: str, seconds: float, out_path: Path) -> Path:
    """ElevenLabs music, instrumental, MP3. ``music_length_ms`` must be 3000..600000. Output: {"audio": {"url"}}."""
    _key()
    args = {"prompt": prompt, "music_length_ms": int(seconds * 1000), "force_instrumental": True}
    result = fal_client.subscribe(MUSIC_ENDPOINT, arguments=args)
    _download(result["audio"]["url"], out_path)
    return Path(out_path)


def transcribe(audio_path: Path) -> dict:
    """ElevenLabs Scribe v2 on a local file. Returns the transcript.json shape: language, text, words.

    Output words carry type "word" | "spacing" | "audio_event"; only "word" entries are kept.
    """
    _key()
    url = fal_client.upload_file(str(audio_path))
    result = fal_client.subscribe(SCRIBE_ENDPOINT, arguments={"audio_url": url, "diarize": False, "tag_audio_events": False})
    words = [
        {"word": w["text"], "start": w["start"], "end": w["end"]}
        for w in result["words"]
        if w["type"] == "word" and w.get("start") is not None
    ]
    # Scribe answers ISO 639-3 ("eng"); transcript.json wants BCP-47 ("en").
    # ponytail: two-language map, unknown codes pass through; use a full 639-3 table if more languages ship.
    language = {"eng": "en", "ell": "el"}.get(result["language_code"], result["language_code"])
    return {"language": language, "text": result["text"], "words": words}
