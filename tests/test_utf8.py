"""C's files never read or write text without encoding="utf-8" (Greek-locale Windows: 'charmap' errors)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
C_FILES = ["backend/run.py", "backend/app.py", "backend/cache.py", "backend/events.py",
           "backend/mcp_server.py", "backend/github_auth.py"]


@pytest.mark.parametrize("name", C_FILES)
def test_text_io_is_utf8(name):
    src = (ROOT / name).read_text(encoding="utf-8")
    bad = [m.group(0) for m in re.finditer(r"\.(read_text|write_text)\([^\n]*", src) if "encoding=" not in m.group(0)]
    bad += [m.group(0) for m in re.finditer(r"\bopen\([^\n]*\)", src)
            if "encoding=" not in m.group(0) and not re.search(r'"[rwa]b"', m.group(0))]
    assert not bad, f"{name}: text I/O without encoding: {bad}"


def test_pipeline_process_is_utf8(tmp_path, monkeypatch):
    from backend import app as api
    seen = {}
    monkeypatch.setattr(api.subprocess, "Popen", lambda *a, **k: seen.update(k) or type("P", (), {"poll": lambda s: None})())
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    api.start_pipeline(tmp_path)
    assert seen["env"]["PYTHONUTF8"] == "1"
