"""C5: the MCP tool lists, makes a fake run, and returns the cached run the second time."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import run as orchestrator
from backend.mcp_server import mcp

REPO = "https://github.com/example/pantrypal"


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    return tmp_path


def call(name, args):
    res = asyncio.run(mcp.call_tool(name, args))
    return json.loads(res.content[0].text)


def test_tools_listed():
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"make_demo_video", "demo_video_status"} <= names


def test_make_then_cached(runs):
    first = call("make_demo_video", {"repo_url": REPO})
    assert first["status"] == "done" and not first["cached"]
    assert (runs / first["run_id"] / "final.mp4").exists()

    again = call("make_demo_video", {"repo_url": REPO})
    assert again["cached"] and again["run_id"] == first["run_id"]

    other = call("make_demo_video", {"repo_url": REPO, "mode": "full"})  # different brief/mode = new run
    assert not other["cached"] and other["run_id"] != first["run_id"]

    assert call("demo_video_status", {"run_id": first["run_id"]})["status"] == "done"


@pytest.mark.parametrize(
    "name,args",
    [
        ("make_demo_video", {"repo_url": REPO, "language": "fr"}),
        ("make_demo_video", {"repo_url": REPO, "recording_path": "/nope.mp4"}),
        ("demo_video_status", {"run_id": "nope"}),
    ],
)
def test_bad_inputs(runs, name, args):
    with pytest.raises(Exception):  # over stdio, the client gets an error result instead
        call(name, args)


def test_brief_text_in_run_and_cache_key(runs):
    a = call("make_demo_video", {"repo_url": REPO, "brief_text": "Judged on craft."})
    cfg = json.loads((runs / a["run_id"] / "run.json").read_text(encoding="utf-8"))
    assert cfg["brief"]["text"] == "Judged on craft."
    assert call("make_demo_video", {"repo_url": REPO, "brief_text": "Judged on craft."})["cached"]
    assert not call("make_demo_video", {"repo_url": REPO, "brief_text": "Judged on aura."})["cached"]
