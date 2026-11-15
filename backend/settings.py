"""Keys from the board (owner: C): set Gemini, fal and GitHub credentials without editing .env.

    GET  /settings   which keys are set (secrets show only their last 4 characters)
    POST /settings   {"values": {KEY: value}, "clear": [KEY]}  -> saved to .env, applied at once

Laptop only. Anyone on the venue Wi-Fi can open the board, so both calls answer only a browser
on this machine: the client must be 127.0.0.1 / ::1, with no proxy headers (a tunnel connects
from localhost too) and, when the browser sends one, an Origin of this same server. Secrets are
write-only: no call ever returns one. Values go to .env (mode 600) and into os.environ, so the
GitHub button appears at once and the next run's process gets the new keys. No restart.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Only these can be read or written from the board.
FIELDS: list[dict] = [
    {"key": "GEMINI_API_KEY", "label": "Gemini API key", "secret": True, "group": "Gemini",
     "help": "https://aistudio.google.com/apikey"},
    {"key": "GEMINI_MODEL", "label": "Gemini model id", "secret": False, "group": "Gemini",
     "help": "The model A's prompts are tuned for (ask A)."},
    {"key": "FAL_KEY", "label": "fal key", "secret": True, "group": "fal",
     "help": "https://fal.ai/dashboard/keys"},
    {"key": "GITHUB_CLIENT_ID", "label": "GitHub OAuth client ID", "secret": False, "group": "GitHub login",
     "help": "https://github.com/settings/developers → New OAuth App"},
    {"key": "GITHUB_CLIENT_SECRET", "label": "GitHub OAuth client secret", "secret": True, "group": "GitHub login",
     "help": "Generate it on the same OAuth app page."},
    {"key": "GITHUB_OAUTH_SCOPES", "label": "GitHub scopes", "secret": False, "group": "GitHub login",
     "help": "read:user = public repos only; 'read:user repo' = private repos too."},
]
KEYS = {f["key"] for f in FIELDS}
LOCAL = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "cf-ray")
VALUE_RE = re.compile(r"^[\x21-\x7e]( ?[\x21-\x7e])*$")  # printable ASCII, single spaces, no quotes/newlines issues


def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def require_local(request: Request) -> None:
    """Refuse anything that isn't a browser on this laptop."""
    if _client_host(request) not in LOCAL:
        raise HTTPException(403, "settings can only be changed on the laptop running DemoCut (open it at http://localhost:8000)")
    if any(h in request.headers for h in PROXY_HEADERS):
        raise HTTPException(403, "settings can't be changed through a tunnel or proxy")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "settings can only be changed from DemoCut's own page")


def _hint(value: str) -> str:
    return "…" + value[-4:] if len(value) > 8 else "set"


def read_env(path: Optional[Path] = None) -> dict[str, str]:
    path = path or ENV_PATH
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(updates: dict[str, Optional[str]], path: Optional[Path] = None) -> None:
    """Set (str) or clear (None) keys in .env, keeping every other line and comment as it was."""
    path = path or ENV_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k in updates:
                lines[i] = f"{k}={updates[k] or ''}"
                done.add(k)
    lines += [f"{k}={v or ''}" for k, v in updates.items() if k not in done]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _current(key: str, file_values: dict[str, str]) -> str:
    return os.environ.get(key) or file_values.get(key, "")


@router.get("/settings")
def get_settings(request: Request) -> dict:
    require_local(request)
    file_values = read_env()
    fields = []
    for f in FIELDS:
        v = _current(f["key"], file_values)
        item = {**f, "set": bool(v)}
        item["value"] = v if not f["secret"] else None   # secrets never leave the server
        item["hint"] = _hint(v) if (f["secret"] and v) else None
        fields.append(item)
    base = str(request.base_url).rstrip("/")
    return {"fields": fields, "github_callback_hint": "/auth/github/callback", "this_address": base}


@router.post("/settings")
def save_settings(request: Request, body: dict = Body(...)) -> dict:
    require_local(request)
    values = body.get("values") or {}
    clear = body.get("clear") or []
    if not isinstance(values, dict) or not isinstance(clear, list):
        raise HTTPException(422, "send {values: {KEY: value}, clear: [KEY]}")
    unknown = (set(values) | set(clear)) - KEYS
    if unknown:
        raise HTTPException(422, f"unknown setting(s): {sorted(unknown)}")
    updates: dict[str, Optional[str]] = {}
    for k, v in values.items():
        v = str(v).strip()
        if not v:
            continue  # empty = keep what's there
        if len(v) > 500 or not VALUE_RE.match(v):
            raise HTTPException(422, f"{k}: not a valid value (one line, no quotes or control characters)")
        updates[k] = v
    for k in clear:
        updates[k] = None
    if updates:
        write_env(updates)
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return {"saved": sorted(k for k, v in updates.items() if v is not None), "cleared": sorted(k for k, v in updates.items() if v is None)}
