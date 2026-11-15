"""fal client (owner: A). Needs FAL_KEY in the environment.

One thin function per endpoint; each makes one call and writes or returns the result.
No cache or retry here: callers wrap it in ``cache.file_call`` + ``cache.retry``.
Field names below were checked 2026-11-15 against fal's OpenAPI schema and one real call each.

"fal draws, we write": image prompts must ask for no text in the picture.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import fal_client

TTS_ENDPOINT = "fal-ai/elevenlabs/tts/multilingual-v2"
CARD_ENDPOINT = "fal-ai/flux-2"
CARD_EDIT_ENDPOINT = "fal-ai/nano-banana-2/edit"
MUSIC_ENDPOINT = "fal-ai/elevenlabs/music"
SCRIBE_ENDPOINT = "fal-ai/elevenlabs/speech-to-text/scribe-v2"
DOWNLOAD_TIMEOUT_S = 60


def _download(url: str, out_path: Path) -> None:
    """A stalled download raises (socket timeout) instead of hanging, so ``cache.retry`` retries it."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as r:
        Path(out_path).write_bytes(r.read())


def tts(text: str, language: str, out_path: Path) -> Path:
    """``language`` is a BCP-47 code ('en', 'el'); the voice is the endpoint's default."""
    # input accepts language_code, output is {"audio": {"url": ...}}.
    result = fal_client.subscribe(TTS_ENDPOINT, arguments={"text": text, "language_code": language})
    _download(result["audio"]["url"], out_path)
    return Path(out_path)


def card_image(prompt: str, out_path: Path) -> Path:
    """FLUX 2 text-to-image, 16:9. Output: {"images": [{"url", "width", "height", ...}], "seed", ...}."""
    result = fal_client.subscribe(CARD_ENDPOINT, arguments={"prompt": prompt, "image_size": "landscape_16_9"})
    _download(result["images"][0]["url"], out_path)
    return Path(out_path)


def card_edit(prompt: str, reference_images: list[Path], out_path: Path) -> Path:
    """Nano Banana 2 edit: prompt + local reference frames (uploaded to fal). Output: {"images": [{"url"}], "description"}."""
    urls = [fal_client.upload_file(str(p)) for p in reference_images]
    result = fal_client.subscribe(
        CARD_EDIT_ENDPOINT, arguments={"prompt": prompt, "image_urls": urls, "aspect_ratio": "16:9"}
    )
    _download(result["images"][0]["url"], out_path)
    return Path(out_path)


def music(prompt: str, seconds: float, out_path: Path) -> Path:
    """ElevenLabs music, instrumental, MP3. ``music_length_ms`` must be 3000..600000. Output: {"audio": {"url"}}."""
    args = {"prompt": prompt, "music_length_ms": int(seconds * 1000), "force_instrumental": True}
    result = fal_client.subscribe(MUSIC_ENDPOINT, arguments=args)
    _download(result["audio"]["url"], out_path)
    return Path(out_path)


def transcribe(audio_path: Path) -> dict:
    """ElevenLabs Scribe v2 on a local file. Returns the transcript.json shape: language, text, words.

    Output words carry type "word" | "spacing" | "audio_event"; only "word" entries are kept.
    """
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
