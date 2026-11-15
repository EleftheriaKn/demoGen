"""cache.py: same request -> stored answer, nothing paid twice; files keyed by content."""

from __future__ import annotations

import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import cache


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMOCUT_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("DEMOCUT_NO_CACHE", raising=False)
    return tmp_path


def counter():
    calls = []

    def make(value="answer"):
        calls.append(1)
        return {"text": value}

    return calls, make


def test_json_call_runs_once():
    calls, make = counter()
    req = {"model": "flash", "prompt": "find moments"}
    assert cache.json_call("gemini", req, make) == {"text": "answer"}
    assert cache.json_call("gemini", dict(reversed(list(req.items()))), make) == {"text": "answer"}  # key order ignored
    assert len(calls) == 1
    cache.json_call("gemini", {**req, "prompt": "other"}, make)
    cache.json_call("fal", req, make)  # other namespace
    assert len(calls) == 3


def test_paths_keyed_by_content(tmp_cache):
    a, b = tmp_cache / "a" / "rec.mp4", tmp_cache / "b" / "take.mp4"
    a.parent.mkdir(); b.parent.mkdir()
    a.write_bytes(b"same video")
    shutil.copy(a, b)
    assert cache.key("gemini", {"video": a}) == cache.key("gemini", {"video": b})  # other run folder: hit
    b.write_bytes(b"new take")
    assert cache.key("gemini", {"video": a}) != cache.key("gemini", {"video": b})


def test_no_cache_env_skips_reads(monkeypatch):
    calls, make = counter()
    cache.json_call("gemini", {"p": 1}, make)
    monkeypatch.setenv("DEMOCUT_NO_CACHE", "1")
    cache.json_call("gemini", {"p": 1}, make)
    assert len(calls) == 2


def test_file_call(tmp_cache):
    made = []

    def make(tmp):
        made.append(1)
        tmp.write_bytes(b"ID3 fake mp3")

    out1 = cache.file_call("fal-voice", {"text": "Hi"}, tmp_cache / "run1" / "voice" / "s01.mp3", make)
    out2 = cache.file_call("fal-voice", {"text": "Hi"}, tmp_cache / "run2" / "voice" / "s01.mp3", make)
    assert out1.read_bytes() == out2.read_bytes() == b"ID3 fake mp3" and len(made) == 1
    with pytest.raises(RuntimeError, match="wrote nothing"):
        cache.file_call("fal-voice", {"text": "empty"}, tmp_cache / "x.mp3", lambda tmp: None)


def test_parallel_calls_safe():
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: cache.json_call("fal", {"i": i % 3}, lambda: {"i": i % 3}), range(24)))
    assert sorted({r["i"] for r in results}) == [0, 1, 2]


def test_retry(monkeypatch):
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)
    n = []

    def flaky():
        n.append(1)
        if len(n) < 3:
            raise TimeoutError("429")
        return "ok"

    assert cache.retry(flaky) == "ok" and len(n) == 3
    with pytest.raises(TimeoutError):
        cache.retry(lambda: (_ for _ in ()).throw(TimeoutError()), tries=2)


def test_windows_replace_race(monkeypatch):
    """Windows can't os.replace over a file another thread has open: the first answer stands."""
    real = cache.os.replace

    def windows_replace(src, dst):
        if Path(dst).exists():
            raise PermissionError(13, "The process cannot access the file because it is being used")
        real(src, dst)

    monkeypatch.setattr(cache.os, "replace", windows_replace)
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: cache.json_call("fal", {"k": 1}, lambda: {"v": 1}), range(16)))
    assert all(r == {"v": 1} for r in results)
    cache.put_json("fal", {"k": 1}, {"v": 2})  # a second write of the same key doesn't crash
    assert cache.get_json("fal", {"k": 1}) == {"v": 1}
    assert not list(Path(cache.cache_dir()).rglob(".tmp-*"))  # no temp files left behind


def test_greek_round_trip():
    answer = {"text": "Φτιάξε συνταγές από το ψυγείο σου"}
    cache.put_json("gemini", {"p": "el"}, answer)
    assert cache.get_json("gemini", {"p": "el"}) == answer
