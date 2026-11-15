"""Gemini client (owner: A). Shared by every Track A stage.

``generate(prompt, Schema)`` asks Gemini for JSON matching a Pydantic model and returns the
parsed model. Settings are fixed: ``GEMINI_MODEL``, temperature 0, schema output, two retries
then raise. Prompts live in ``backend/prompts/`` and are loaded with ``prompt(name, **vars)``.

Record/replay: when ``RECORDINGS`` is a folder, every answer is stored there under a hash of
(schema, prompt). With ``LIVE`` off, answers come only from that folder and a miss raises.
Tests turn replay on (tests/conftest.py); ``pytest --live`` refreshes the recordings.

Video: ``generate(..., video=path)`` sends an uploaded video before the text. ``upload(path)``
uploads once and remembers the upload in ``<path>.gemini.json`` next to the video, so every
stage of a run reuses it until Gemini expires it (48 h), then it uploads again. Replay never
uploads; the recording key adds the video's hash.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence, TypeVar

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


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def recording_path(
    text: str, schema: type[BaseModel], images: Sequence[Path] = (), video: Optional[Path] = None
) -> Path:
    # ponytail: key includes the schema's Field descriptions, so editing a contract orphans its
    # recordings (replay then fails loudly); re-record with `pytest --live`.
    assert RECORDINGS is not None
    parts: list = [schema.__name__, schema.model_json_schema(), text]
    if images:  # text-only keys stay as they were, so older recordings still replay
        parts.append([_sha(i) for i in images])
    if video is not None:
        parts.append({"video": _sha(video)})
    key = json.dumps(parts, sort_keys=True)
    return RECORDINGS / f"{schema.__name__}_{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def generate(text: str, schema: type[T], images: Sequence[Path] = (), video: Optional[Path] = None) -> T:
    """``images`` are image files (JPEG, PNG...) sent after the text, in order; ``video`` goes before the text."""
    path = recording_path(text, schema, images, video) if RECORDINGS is not None else None
    if not LIVE:
        if path is None or not path.exists():
            raise MissingRecording(f"no Gemini recording {path}; run `pytest --live` to record it")
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    model = os.environ["GEMINI_MODEL"]  # config errors raise once, not after retries
    for attempt in range(RETRIES + 1):
        try:
            raw = _call(model, text, schema, images, video)
            result = schema.model_validate_json(raw)
            break
        except Exception:
            if attempt == RETRIES:
                raise
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def upload(video: Path) -> tuple[str, str]:
    """(uri, mime_type) of ``video`` on Gemini, uploading only if no live upload is remembered."""
    from google import genai

    video = Path(video)
    memo = video.with_name(video.name + ".gemini.json")
    sha = _sha(video)
    client = genai.Client()
    if memo.exists():
        m = json.loads(memo.read_text(encoding="utf-8"))
        try:
            if m["sha256"] == sha and client.files.get(name=m["name"]).state == "ACTIVE":
                return m["uri"], m["mime_type"]
        except Exception:
            pass  # expired or deleted: upload again
    f = client.files.upload(file=video)
    deadline = time.monotonic() + 600
    while f.state == "PROCESSING":
        if time.monotonic() > deadline:
            raise TimeoutError(f"Gemini still processing {video.name} after 10 min")
        time.sleep(3)
        f = client.files.get(name=f.name)
    if f.state != "ACTIVE":
        raise RuntimeError(f"Gemini upload of {video.name} ended {f.state}: {f.error}")
    memo.write_text(json.dumps({"sha256": sha, "name": f.name, "uri": f.uri, "mime_type": f.mime_type}), encoding="utf-8")
    return f.uri, f.mime_type


def _call(
    model: str, text: str, schema: type[BaseModel], images: Sequence[Path] = (), video: Optional[Path] = None
) -> str:
    from google import genai
    from google.genai import types

    contents: list[Any] = [text, *(types.Part.from_bytes(data=Path(i).read_bytes(), mime_type=mimetypes.guess_type(str(i))[0] or "image/jpeg") for i in images)]
    if video is not None:
        uri, mime = upload(video)
        contents.insert(0, types.Part.from_uri(file_uri=uri, mime_type=mime))
    client = genai.Client()  # reads GEMINI_API_KEY; keep a reference or it closes mid-request
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        ),
    )
    return resp.text or ""
