"""HTTP API (owner: C).

    uvicorn backend.app:app --reload            # http://127.0.0.1:8000/docs

GET  /                         the board (web/index.html)
POST /runs                    start a run (multipart form; no recording = fixture take)
GET  /runs                    list runs, newest first
GET  /runs/{id}               run.json + status + finished stages
GET  /runs/{id}/events        Server-Sent Events: every events.jsonl line, live, until the run ends
GET  /runs/{id}/files/{path}  any file in the run folder (final.mp4, decisions.json, ...)

Each run is a separate ``python -m backend.run runs/<id>`` process, so runs never share
state and a crashed run can be resumed by starting the same command again.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from backend import run as orchestrator
from backend.contracts import Brief, RunConfig

ROOT = orchestrator.ROOT
app = FastAPI(title="DemoCut")

# run_id -> pipeline process started by this server
_procs: dict[str, subprocess.Popen] = {}

POLL_S = 0.25


def runs_root() -> Path:
    return orchestrator.RUNS


def run_dir_or_404(run_id: str) -> Path:
    d = (runs_root() / run_id).resolve()
    if d.parent != runs_root().resolve() or not (d / "run.json").exists():
        raise HTTPException(404, f"no run {run_id!r}")
    return d


def read_events(run_dir: Path) -> list[dict]:
    log = run_dir / "events.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line being written right now
    return out


def run_status(run_dir: Path) -> str:
    """queued | running | done | failed."""
    evs = [e for e in read_events(run_dir) if e.get("stage") == "run"]
    last = evs[-1]["msg"] if evs else ""
    if last == "Run done":
        return "done"
    if last.startswith("Run failed"):
        return "failed"
    proc = _procs.get(run_dir.name)
    if proc is not None and proc.poll() not in (None, 0):
        return "failed"  # died before it could log
    return "running" if evs else "queued"


def start_pipeline(run_dir: Path) -> subprocess.Popen:
    log = open(run_dir / "pipeline.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.run", str(run_dir)],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )
    _procs[run_dir.name] = proc
    return proc


@app.get("/", include_in_schema=False)
def board() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/runs", status_code=201)
async def create_run(
    repo_url: str = Form(...),
    criteria: str = Form("usefulness,craft,aura", description="Comma-separated judging criteria."),
    time_limit_s: int = Form(180),
    language: str = Form("en"),
    second_language: Optional[str] = Form(None),
    mode: str = Form("cheap"),
    recording: Optional[UploadFile] = File(None, description="Screen recording; omit to use the fixture take."),
) -> dict:
    brief = Brief(
        criteria=[c.strip() for c in criteria.split(",") if c.strip()],
        time_limit_s=time_limit_s,
        language=language,
        second_language=second_language or None,
    )
    run_id = orchestrator.new_run_id()
    try:
        if recording is None or not recording.filename:
            src = orchestrator.FIXTURE_RUN / orchestrator.load_run(orchestrator.FIXTURE_RUN).recording
            name = src.name
            cfg = RunConfig(run_id=run_id, repo_url=repo_url, recording=name, brief=brief, mode=mode)
            run_dir = orchestrator.create_run(cfg, src)
        else:
            name = "recording" + (Path(recording.filename).suffix.lower() or ".mp4")
            cfg = RunConfig(run_id=run_id, repo_url=repo_url, recording=name, brief=brief, mode=mode)
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / name
                with open(src, "wb") as f:
                    shutil.copyfileobj(recording.file, f)
                run_dir = orchestrator.create_run(cfg, src)
    except ValueError as e:  # pydantic validation
        raise HTTPException(422, str(e))
    start_pipeline(run_dir)
    return {"run_id": run_id, "events": f"/runs/{run_id}/events", "final": f"/runs/{run_id}/files/final.mp4"}


@app.get("/runs")
def list_runs() -> list[dict]:
    root = runs_root()
    if not root.exists():
        return []
    dirs = sorted((d for d in root.iterdir() if (d / "run.json").exists()), key=lambda d: d.stat().st_mtime, reverse=True)
    return [{"run_id": d.name, "status": run_status(d)} for d in dirs]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    d = run_dir_or_404(run_id)
    return {
        "run_id": run_id,
        "status": run_status(d),
        "config": json.loads((d / "run.json").read_text()),
        "finished_stages": sorted(orchestrator.finished_stages(d)),
        "has_final": (d / "final.mp4").exists(),
    }


@app.get("/runs/{run_id}/events")
async def stream_events(run_id: str) -> StreamingResponse:
    d = run_dir_or_404(run_id)

    async def gen():
        sent = 0
        while True:
            evs = read_events(d)
            for ev in evs[sent:]:
                yield f"data: {json.dumps(ev)}\n\n"
            sent = len(evs)
            status = run_status(d)
            if status in ("done", "failed"):
                yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                return
            await asyncio.sleep(POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/runs/{run_id}/files/{path:path}")
def get_file(run_id: str, path: str) -> FileResponse:
    d = run_dir_or_404(run_id)
    f = (d / path).resolve()
    if not f.is_relative_to(d) or not f.is_file():
        raise HTTPException(404, f"no file {path!r} in run {run_id}")
    return FileResponse(f)
