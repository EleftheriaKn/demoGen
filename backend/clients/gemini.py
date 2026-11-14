"""Gemini client (owner: A). Shared by every Track A stage.

``generate(prompt, Schema)`` asks Gemini for JSON matching a Pydantic model and returns the
parsed model. Settings are fixed: ``GEMINI_MODEL``, temperature 0, schema output, two retries
then raise. Prompts live in ``backend/prompts/`` and are loaded with ``prompt(name, **vars)``.

Record/replay: when ``RECORDINGS`` is a folder, every answer is stored there under a hash of
(schema, prompt). With ``LIVE`` off, answers come only from that folder and a miss raises.
Tests turn replay on (tests/conftest.py); ``pytest --live`` refreshes the recordings.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
RECORDINGS: Optional[Path] = None
LIVE = True
RETRIES = 2

T = TypeVar("T", bound=BaseModel)


class MissingRecording(RuntimeError):
    pass


def prompt(name: str, **values: str) -> str:
    """Fill ``backend/prompts/<name>.md``; ``{key}`` placeholders take ``values``."""
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").format(**values)


def recording_path(text: str, schema: type[BaseModel]) -> Path:
    # ponytail: key includes the schema's Field descriptions, so editing a contract orphans its
    # recordings (replay then fails loudly); re-record with `pytest --live`.
    assert RECORDINGS is not None
    key = json.dumps([schema.__name__, schema.model_json_schema(), text], sort_keys=True)
    return RECORDINGS / f"{schema.__name__}_{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def generate(text: str, schema: type[T]) -> T:
    path = recording_path(text, schema) if RECORDINGS is not None else None
    if not LIVE:
        if path is None or not path.exists():
            raise MissingRecording(f"no Gemini recording {path}; run `pytest --live` to record it")
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    model = os.environ["GEMINI_MODEL"]  # config errors raise once, not after retries
    for attempt in range(RETRIES + 1):
        try:
            raw = _call(model, text, schema)
            result = schema.model_validate_json(raw)
            break
        except Exception:
            if attempt == RETRIES:
                raise
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def _call(model: str, text: str, schema: type[BaseModel]) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client()  # reads GEMINI_API_KEY; keep a reference or it closes mid-request
    resp = client.models.generate_content(
        model=model,
        contents=text,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        ),
    )
    return resp.text or ""
