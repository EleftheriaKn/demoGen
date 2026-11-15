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

# Fixed tables, not the mimetypes module: its answers depend on the machine (Windows registry).
VIDEO_MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


INCOMPLETE = "The AI's answer was incomplete. Run again."
UPLOAD_FAILED = ("Uploading the recording to Gemini failed. Check the internet connection and try again; "
                 "the run resumes where it stopped.")


class MissingRecording(RuntimeError):
    pass


class MissingKey(KeyError):
    """A setting the board must provide; ``str()`` is the plain message, not KeyError's quoted repr."""

    def __str__(self) -> str:
        return str(self.args[0])


class UploadFailed(RuntimeError):
    pass


def require(name: str, *alternatives: str) -> str:
    """The value of env var ``name`` (or the first set alternative); unset raises MissingKey."""
    for n in (name, *alternatives):
        if os.environ.get(n):
            return os.environ[n]
    raise MissingKey(f"{name} isn't set. Add it in the board's Settings, then run again.")


def explain(service: str, e: BaseException, key: str) -> RuntimeError:
    """A plain RuntimeError for ``e``, raised by ``service`` after its retries; ``e`` is kept in brackets."""
    import httpx
    from pydantic import ValidationError

    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    text = str(e).lower()
    kept = "finished stages are kept."
    if isinstance(e, ValidationError):
        msg = INCOMPLETE
    elif code == 402 or "balance" in text:
        msg = f"The {service} account is out of credit. Top it up, then run again; {kept}"
    elif code == 429 and "perday" in text.replace(" ", "").replace("_", ""):
        msg = f"{service}'s daily quota is used up. Wait until it resets or raise it, then run again; {kept}"
    elif code == 429:
        msg = f"{service} is rate-limiting us. Wait a minute and run again; {kept}"
    elif code in (401, 403):
        msg = f"{service} rejected the API key. Check {key} in the board's Settings, then run again."
    elif isinstance(code, int) and code >= 500:
        msg = f"{service} is having trouble right now. Wait a few minutes and run again; {kept}"
    elif isinstance(e, (OSError, httpx.TransportError)):
        msg = f"Couldn't reach {service}. Check the internet connection and run again; {kept}"
    else:
        msg = f"{service} failed after several tries. Run again; {kept}"
    return RuntimeError(f"{msg} [{type(e).__name__}: {e}]")


def prompt(name: str, **values: str) -> str:
    """Fill ``backend/prompts/<name>.md``; ``{key}`` placeholders take ``values``.
    ``{brief_block}`` is filled with the run's judging brief (backend/brief.py, B19) unless given."""
    template = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    if "{brief_block}" in template and "brief_block" not in values:
        from backend import brief  # B's module; empty when the run has no brief
        values = {**values, "brief_block": brief.block()}
    return template.format(**values)


def mime_type(path: Path, table: dict[str, str]) -> str:
    """``path``'s MIME type from ``table`` by suffix; an unknown suffix raises."""
    suffix = Path(path).suffix.lower()
    if suffix not in table:
        if table is VIDEO_MIME:
            raise ValueError("This video format isn't supported. Use .mp4, .mov or .webm.")
        raise ValueError(f"Gemini: can't send {Path(path).name}; supported types are {', '.join(table)}")
    return table[suffix]


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


def generate(text: str, schema: type[T], images: Sequence[Path] = (), video: Optional[Path] = None,
             thinking: Optional[str] = None) -> T:
    """``images`` are image files (JPEG, PNG...) sent after the text, in order; ``video`` goes before the text.
    ``thinking`` sets the model's thinking level ("low", "high"...); None keeps its default."""
    path = recording_path(text, schema, images, video) if RECORDINGS is not None else None
    if not LIVE:
        if path is None or not path.exists():
            raise MissingRecording(f"no Gemini recording {path}; run `pytest --live` to record it")
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    # config errors raise once, not after retries
    require("GEMINI_API_KEY", "GOOGLE_API_KEY")  # genai.Client reads either
    model = require("GEMINI_MODEL")
    if video is not None:
        mime_type(video, VIDEO_MIME)
    for attempt in range(RETRIES + 1):
        try:
            raw = _call(model, text, schema, images, video, thinking)
            result = schema.model_validate_json(raw)
            break
        except UploadFailed:
            if attempt == RETRIES:
                raise
        except Exception as e:
            if attempt == RETRIES:
                raise explain("Gemini", e, "GEMINI_API_KEY") from e
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def upload(video: Path) -> tuple[str, str]:
    """(uri, mime_type) of ``video`` on Gemini, uploading only if no live upload is remembered."""
    from google import genai

    video = Path(video)
    mime = mime_type(video, VIDEO_MIME)
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
    try:
        f = client.files.upload(file=video, config={"mime_type": mime})
        deadline = time.monotonic() + 600
        while f.state == "PROCESSING":
            if time.monotonic() > deadline:
                raise TimeoutError(f"Gemini still processing {video.name} after 10 min")
            time.sleep(3)
            f = client.files.get(name=f.name)
    except Exception as e:
        raise UploadFailed(f"{UPLOAD_FAILED} [{type(e).__name__}: {e}]") from e
    if f.state != "ACTIVE":
        raise UploadFailed(f"{UPLOAD_FAILED} [upload of {video.name} ended {f.state}: {f.error}]")
    memo.write_text(json.dumps({"sha256": sha, "name": f.name, "uri": f.uri, "mime_type": f.mime_type}), encoding="utf-8")
    return f.uri, f.mime_type


def _call(
    model: str, text: str, schema: type[BaseModel], images: Sequence[Path] = (), video: Optional[Path] = None,
    thinking: Optional[str] = None,
) -> str:
    from google import genai
    from google.genai import types

    contents: list[Any] = [text, *(types.Part.from_bytes(data=Path(i).read_bytes(), mime_type=mime_type(i, IMAGE_MIME)) for i in images)]
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
            thinking_config=types.ThinkingConfig(thinking_level=thinking) if thinking else None,
        ),
    )
    return resp.text or ""
