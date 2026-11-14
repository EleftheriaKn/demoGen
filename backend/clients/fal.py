"""fal client (owner: A). Needs FAL_KEY in the environment.

``tts(text, language, out_path)`` speaks one line with ElevenLabs multilingual v2 and writes the MP3.
No cache or retry here: callers wrap it in ``cache.file_call`` + ``cache.retry``.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import fal_client

TTS_ENDPOINT = "fal-ai/elevenlabs/tts/multilingual-v2"


def tts(text: str, language: str, out_path: Path) -> Path:
    """``language`` is a BCP-47 code ('en', 'el'); the voice is the endpoint's default."""
    # Checked 2026-11-15 against a real call: input accepts language_code, output is {"audio": {"url": ...}}.
    result = fal_client.subscribe(TTS_ENDPOINT, arguments={"text": text, "language_code": language})
    urllib.request.urlretrieve(result["audio"]["url"], out_path)
    return Path(out_path)
