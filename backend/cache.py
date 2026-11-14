"""Answer cache (owner: C). Every Gemini and fal answer is stored in ``.cache/``.

A repeat run of the same take costs nothing: the same request gives the same key, so the
answer comes from disk. **Never clear .cache/ before the pitch.**

Use it in a client (A) like this::

    from backend import cache

    # JSON answers (Gemini). Paths in the request are keyed by their file content.
    answer = cache.json_call(
        "gemini",
        {"model": model, "prompt": prompt, "schema": schema, "video": Path(run_dir, "recording.mp4")},
        lambda: call_gemini(...),          # only runs on a cache miss; must return JSON-able data
    )

    # Files (fal voice, cards, music): the maker writes to the path it is given.
    cache.file_call("fal-voice", {"text": line, "voice": v}, out_path, lambda tmp: download(url, tmp))

    # Retry flaky calls with back-off (do this inside the cached function).
    cache.retry(lambda: client.generate(...), tries=4)

Env: ``DEMOCUT_CACHE`` = cache folder (default ``.cache/``); ``DEMOCUT_NO_CACHE=1`` = ignore
stored answers (new answers are still written).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

ROOT = Path(__file__).resolve().parent.parent

_hash_memo: dict[tuple[str, int, int], str] = {}
_memo_lock = threading.Lock()


def cache_dir() -> Path:
    return Path(os.environ.get("DEMOCUT_CACHE") or ROOT / ".cache")


def _enabled() -> bool:
    return os.environ.get("DEMOCUT_NO_CACHE", "") not in ("1", "true", "yes")


def file_hash(path: Path) -> str:
    """sha256 of a file's bytes, memoized by (path, size, mtime) so a big take is read once."""
    path = Path(path).resolve()
    st = path.stat()
    memo_key = (str(path), st.st_size, st.st_mtime_ns)
    with _memo_lock:
        if memo_key in _hash_memo:
            return _hash_memo[memo_key]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    with _memo_lock:
        _hash_memo[memo_key] = digest
    return digest


def _normalize(obj: Any) -> Any:
    """Make a request JSON-stable. Paths become their content hash, so moving a file or
    using another run folder still hits the cache."""
    if isinstance(obj, Path):
        return {"__file_sha256__": file_hash(obj)}
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return {"__bytes_sha256__": hashlib.sha256(obj).hexdigest()}
    if hasattr(obj, "model_dump"):  # pydantic models
        return _normalize(obj.model_dump(mode="json"))
    if isinstance(obj, type) and hasattr(obj, "model_json_schema"):  # a pydantic class as schema
        return _normalize(obj.model_json_schema())
    return obj


def key(namespace: str, request: Any) -> str:
    """Stable key for a request: sha256 of the namespace + normalized request."""
    blob = json.dumps({"ns": namespace, "req": _normalize(request)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _path(namespace: str, k: str, suffix: str) -> Path:
    safe_ns = "".join(c if c.isalnum() or c in "-_" else "_" for c in namespace)
    return cache_dir() / safe_ns / k[:2] / f"{k}{suffix}"


def _atomic_write(dst: Path, write: Callable[[Path], None]) -> None:
    """Write via a temp file in the same folder, then rename: parallel threads never see half a file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".tmp-", suffix=dst.suffix)
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        write(tmp_path)
        os.replace(tmp_path, dst)
    finally:
        tmp_path.unlink(missing_ok=True)


def get_json(namespace: str, request: Any) -> Optional[Any]:
    p = _path(namespace, key(namespace, request), ".json")
    if not _enabled() or not p.exists():
        return None
    return json.loads(p.read_text())["answer"]


def put_json(namespace: str, request: Any, answer: Any) -> None:
    p = _path(namespace, key(namespace, request), ".json")
    record = {"ns": namespace, "saved": time.time(), "answer": answer}
    _atomic_write(p, lambda tmp: tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1)))


def json_call(namespace: str, request: Any, make: Callable[[], T]) -> T:
    """Return the stored answer for ``request``, or run ``make()`` once and store its answer."""
    cached = get_json(namespace, request)
    if cached is not None:
        return cached
    answer = make()
    put_json(namespace, request, answer)
    return answer


def file_call(namespace: str, request: Any, out_path: Path, make: Callable[[Path], None]) -> Path:
    """Copy the stored file for ``request`` to ``out_path``; on a miss, ``make(tmp_path)``
    writes it first. ``out_path``'s suffix (e.g. ``.mp3``) is kept in the cache."""
    out_path = Path(out_path)
    stored = _path(namespace, key(namespace, request), out_path.suffix)
    if not (_enabled() and stored.exists()):
        def write(tmp: Path) -> None:
            make(tmp)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError(f"cache.file_call({namespace!r}): maker wrote nothing")
        _atomic_write(stored, write)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stored, out_path)
    return out_path


def retry(fn: Callable[[], T], tries: int = 4, base_s: float = 2.0, max_s: float = 30.0,
          on: tuple[type[BaseException], ...] = (Exception,)) -> T:
    """Call ``fn``; on failure wait 2, 4, 8... s (capped) and try again, ``tries`` times in all."""
    for attempt in range(tries):
        try:
            return fn()
        except on:
            if attempt == tries - 1:
                raise
            time.sleep(min(max_s, base_s * 2**attempt))
    raise AssertionError("unreachable")
