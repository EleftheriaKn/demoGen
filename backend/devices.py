"""Log new devices (owner: B, ticket B13).

The first request from a device prints one line, e.g. ``new device: iPhone · Safari · 172.20.1.42``, and
appends it to ``runs/devices.jsonl`` (time, address, device, browser: nothing else). A device id cookie
(address + browser as a fallback) keeps reloads quiet. Starting a run logs which device started it.

Attached to C's app by ``backend/serve.py``; ``app.py`` itself is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

COOKIE = "democut_device"
log = logging.getLogger("uvicorn.error")

DEVICES = [  # first match wins
    (r"iPhone", "iPhone"), (r"iPad", "iPad"), (r"Android.*Mobile", "Android phone"), (r"Android", "Android tablet"),
    (r"Macintosh|Mac OS X", "Mac"), (r"Windows", "Windows PC"), (r"CrOS", "Chromebook"), (r"Linux", "Linux PC"),
]
BROWSERS = [
    (r"EdgA?/|Edg/", "Edge"), (r"SamsungBrowser", "Samsung Internet"), (r"OPR/|Opera", "Opera"),
    (r"FxiOS|Firefox", "Firefox"), (r"CriOS|Chrome", "Chrome"), (r"Safari", "Safari"), (r"curl", "curl"),
]


def describe(user_agent: str) -> tuple[str, str]:
    """('iPhone', 'Safari') from a User-Agent; 'unknown' when nothing matches."""
    dev = next((name for pat, name in DEVICES if re.search(pat, user_agent)), "unknown device")
    browser = next((name for pat, name in BROWSERS if re.search(pat, user_agent)), "unknown browser")
    return dev, browser


class DeviceLog(BaseHTTPMiddleware):
    def __init__(self, app, log_file: Path):
        super().__init__(app)
        self.log_file = Path(log_file)
        self.seen: set[str] = set()
        self.lock = threading.Lock()

    def _write(self, record: dict) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def dispatch(self, request: Request, call_next) -> Response:
        addr = request.client.host if request.client else "?"
        dev, browser = describe(request.headers.get("user-agent", ""))
        cookie = request.cookies.get(COOKIE)
        key = cookie or f"{addr}|{dev}|{browser}"
        new = key not in self.seen and (not cookie or cookie not in self.seen)
        device_id = cookie or uuid.uuid4().hex[:12]
        if new:
            with self.lock:
                self.seen.update({key, device_id, f"{addr}|{dev}|{browser}"})
            log.info("new device: %s · %s · %s", dev, browser, addr)
            self._write({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "new device",
                         "device_id": device_id, "address": addr, "device": dev, "browser": browser})

        response = await call_next(request)

        if request.method == "POST" and request.url.path == "/runs" and response.status_code == 201:
            body = b"".join([chunk async for chunk in response.body_iterator])
            run_id = json.loads(body or b"{}").get("run_id", "?")
            log.info("run %s started by %s · %s · %s", run_id, dev, browser, addr)
            self._write({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "run started",
                         "run_id": run_id, "device_id": device_id, "address": addr, "device": dev,
                         "browser": browser})
            response = Response(body, status_code=response.status_code, headers=dict(response.headers),
                                media_type=response.media_type)
        if not cookie:
            response.set_cookie(COOKIE, device_id, max_age=30 * 24 * 3600, samesite="lax")
        return response
