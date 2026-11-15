"""MCP server (owner: C): lets Claude Code make a demo video.

Add it to Claude Code once, from the repo root:

    claude mcp add democut -- "$PWD/.venv/bin/python" -m backend.mcp_server

Tools:
- make_demo_video(repo_url, recording_path, ...) -> runs the whole pipeline and returns the
  path of final.mp4. If a finished run already has the same repo, recording and brief, it
  returns that run at once ("cached run") and nothing is paid twice.
- demo_video_status(run_id) -> status and last progress line of a run.

Same pipeline as the board: each run is ``python -m backend.run runs/<id>`` in its own process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from backend import app as api
from backend import run as orchestrator
from backend.contracts import Brief, RunConfig

mcp = MCPServer(
    name="democut",
    instructions="DemoCut turns a GitHub repo and one rough screen recording into a 2-3 minute demo video.",
)

POLL_S = 2.0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_cached_run(repo_url: str, recording: Path, brief: Brief, mode: str) -> Optional[Path]:
    """A finished run with the same repo, brief, mode and recording bytes, newest first."""
    root = orchestrator.RUNS
    if not root.exists():
        return None
    want = brief.model_dump()
    rec_hash = None
    for d in sorted(root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
        if not (d / "run.json").exists() or not (d / "final.mp4").exists() or api.run_status(d) != "done":
            continue
        cfg = orchestrator.load_run(d)
        if cfg.repo_url != repo_url or cfg.mode != mode or cfg.brief.model_dump() != want:
            continue
        rec_hash = rec_hash or _sha256(recording)
        if _sha256(d / cfg.recording) == rec_hash:
            return d
    return None


def _result(run_dir: Path, cached: bool) -> dict:
    status = api.run_status(run_dir)
    out = {"run_id": run_dir.name, "status": status, "cached": cached, "run_dir": str(run_dir)}
    if status == "done":
        out["final_mp4"] = str(run_dir / "final.mp4")
        out["captions_srt"] = str(run_dir / "captions.srt")
        out["decisions_json"] = str(run_dir / "decisions.json")
    else:
        out["log"] = str(run_dir / "pipeline.log")
        evs = api.read_events(run_dir)
        out["last_event"] = evs[-1]["msg"] if evs else None
    return out


@mcp.tool()
async def make_demo_video(
    repo_url: str,
    ctx: Context,
    recording_path: Optional[str] = None,
    criteria: list[str] = ["usefulness", "craft", "aura"],
    time_limit_s: int = 180,
    language: str = "en",
    second_language: Optional[str] = None,
    mode: str = "cheap",
    wait: bool = True,
) -> dict:
    """Make a 2-3 minute demo video from a GitHub repo and a rough screen recording.

    Args:
        repo_url: GitHub URL of the app.
        recording_path: Absolute path of the screen recording on this machine.
            Omit to use the built-in fixture take.
        criteria: Judging criteria, verbatim from the hackathon brief.
        time_limit_s: Maximum length of the video, in seconds: 30 or 45 for a short clip,
            up to 180 for a full hackathon demo.
        language: Voice and caption language: "en" or "el".
        second_language: Optional second caption language: "en", "el" or null.
        mode: "cheap" (fast, for testing) or "full" (best quality).
        wait: True = return when the video is ready (up to ~15 min).
            False = start it and return the run_id; check with demo_video_status.
    """
    if language not in api.LANGUAGES or second_language not in (None, *api.LANGUAGES) or second_language == language:
        raise ValueError(f"languages must be one of {api.LANGUAGES} (second one different or null)")
    brief = Brief(criteria=criteria, time_limit_s=time_limit_s, language=language, second_language=second_language)

    if recording_path:
        recording = Path(recording_path).expanduser()
        if not recording.is_file():
            raise ValueError(f"recording not found: {recording}")
    else:
        recording = orchestrator.FIXTURE_RUN / orchestrator.load_run(orchestrator.FIXTURE_RUN).recording

    cached = find_cached_run(repo_url, recording, brief, mode)
    if cached is not None:
        return _result(cached, cached=True)

    name = "recording" + (recording.suffix.lower() or ".mp4")
    cfg = RunConfig(run_id=orchestrator.new_run_id(), repo_url=repo_url, recording=name, brief=brief, mode=mode)
    run_dir = orchestrator.create_run(cfg, recording)
    proc = api.start_pipeline(run_dir)
    if not wait:
        return _result(run_dir, cached=False)

    last = None
    while proc.poll() is None:
        evs = api.read_events(run_dir)
        if evs and evs[-1] != last:
            last = evs[-1]
            await ctx.report_progress(last["progress"], 1.0, f"[{last['stage']}] {last['msg']}")
        await asyncio.sleep(POLL_S)
    return _result(run_dir, cached=False)


@mcp.tool()
def demo_video_status(run_id: str) -> dict:
    """Status of a DemoCut run started earlier: queued, running, done or failed."""
    try:
        return _result(api.run_dir_or_404(run_id), cached=False)
    except api.HTTPException:
        raise ValueError(f"no run {run_id!r}")


if __name__ == "__main__":
    mcp.run("stdio")
