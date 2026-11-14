"""HTTP API (owner: C).

    uvicorn backend.app:app --reload            # http://127.0.0.1:8000/docs

GET  /                         the board (web/index.html); installable: /manifest.webmanifest, /sw.js, /web/*
POST /runs                    start a run (multipart form; no recording = fixture take)
POST /uploads                 start a resumable upload {filename, size} -> {upload_id, chunk_size}
PUT  /uploads/{id}?offset=N   append one part (raw bytes); a wrong offset -> 409 with what arrived
GET  /uploads/{id}            how much arrived (to resume after a dropped connection)
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
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import run as orchestrator
from backend.contracts import Brief, RunConfig

ROOT = orchestrator.ROOT
app = FastAPI(title="DemoCut")

# run_id -> pipeline process started by this server
_procs: dict[str, subprocess.Popen] = {}

POLL_S = 0.25
# Runbook limits for a take: 7:00 and 1 GiB, checked on the phone AND here.
MAX_TAKE_S = 420
MAX_TAKE_BYTES = 1024**3
CHUNK_BYTES = 8 * 1024**2
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".3gp", ".avi"}
LANGUAGES = ("en", "el")  # runbook: English and Greek only, for now


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


def uploads_root() -> Path:
    return runs_root() / "_uploads"


def check_take(path: Path) -> None:
    """Server-side limits: a real, readable video of at most 7:00 and 1 GiB (ffprobe, via B's ff)."""
    from backend.pipeline.ff import video_info

    size = path.stat().st_size
    if size > MAX_TAKE_BYTES:
        raise HTTPException(413, f"recording is {size:,} bytes; the limit is 1 GiB ({MAX_TAKE_BYTES:,})")
    try:
        dur = video_info(path).duration
    except Exception:
        raise HTTPException(422, "the recording is not a video we can read")
    if not dur or dur <= 0:
        raise HTTPException(422, "could not read the recording's length")
    if dur > MAX_TAKE_S + 0.5:
        raise HTTPException(422, f"recording is {int(dur // 60)}:{int(dur % 60):02d} long; the limit is 7:00")


def _upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not upload_id.isalnum() or len(upload_id) > 64:
        raise HTTPException(404, "no such upload")
    d = uploads_root()
    meta, data = d / f"{upload_id}.json", d / f"{upload_id}.part"
    if not meta.exists():
        raise HTTPException(404, "no such upload")
    return meta, data


def _upload_state(upload_id: str) -> dict:
    meta, data = _upload_paths(upload_id)
    m = json.loads(meta.read_text())
    m["received"] = data.stat().st_size if data.exists() else 0
    m["complete"] = m["received"] == m["size"]
    return m


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


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(ROOT / "web" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Served from / so its scope covers the whole app (needed for "Install").
    return FileResponse(ROOT / "web" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/web", StaticFiles(directory=ROOT / "web"), name="web")


@app.post("/uploads", status_code=201)
async def start_upload(body: dict = Body(...)) -> dict:
    """Start a resumable upload. The phone then PUTs 8 MB parts in order."""
    filename, size = str(body.get("filename", "")), body.get("size")
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(422, f"not a video file: {filename!r}")
    if not isinstance(size, int) or size <= 0:
        raise HTTPException(422, "size must be the file size in bytes")
    if size > MAX_TAKE_BYTES:
        raise HTTPException(413, f"recording is {size:,} bytes; the limit is 1 GiB ({MAX_TAKE_BYTES:,})")
    upload_id = uuid.uuid4().hex
    uploads_root().mkdir(parents=True, exist_ok=True)
    (uploads_root() / f"{upload_id}.json").write_text(json.dumps({"upload_id": upload_id, "ext": ext, "size": size}))
    (uploads_root() / f"{upload_id}.part").touch()
    return {"upload_id": upload_id, "chunk_size": CHUNK_BYTES, "received": 0}


@app.get("/uploads/{upload_id}")
def upload_status(upload_id: str) -> dict:
    return _upload_state(upload_id)


@app.put("/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, offset: int) -> dict:
    """Append one part at ``offset``. Resend from ``received`` if the offsets don't match."""
    state = _upload_state(upload_id)
    if offset != state["received"]:
        raise HTTPException(409, {"msg": "offset mismatch", "received": state["received"]})
    _, data = _upload_paths(upload_id)
    written = 0
    with open(data, "ab") as f:
        async for piece in request.stream():
            written += len(piece)
            if state["received"] + written > state["size"]:
                f.truncate(state["received"])  # drop this part, keep what arrived before
                raise HTTPException(413, "more bytes than the declared size")
            f.write(piece)
    state = _upload_state(upload_id)
    return {"received": state["received"], "complete": state["complete"]}


@app.post("/runs", status_code=201)
async def create_run(
    repo_url: str = Form(...),
    criteria: str = Form("usefulness,craft,aura", description="Comma-separated judging criteria."),
    time_limit_s: int = Form(180),
    language: str = Form("en"),
    second_language: Optional[str] = Form(None),
    brief_text: str = Form("", description="The judging brief as pasted; saved as brief.txt in the run."),
    mode: str = Form("cheap"),
    recording: Optional[UploadFile] = File(None, description="Small recording in one go; omit to use the fixture take."),
    upload_id: Optional[str] = Form(None, description="A finished POST /uploads (big recordings from the phone)."),
) -> dict:
    second_language = second_language or None
    if language not in LANGUAGES or second_language not in (None, *LANGUAGES) or second_language == language:
        raise HTTPException(422, f"languages must be one of {LANGUAGES} (second one different or empty)")
    brief = Brief(
        criteria=[c.strip() for c in criteria.split(",") if c.strip()],
        time_limit_s=time_limit_s,
        language=language,
        second_language=second_language,
    )
    run_id = orchestrator.new_run_id()
    with tempfile.TemporaryDirectory() as tmp:
        if upload_id:
            state = _upload_state(upload_id)
            if not state["complete"]:
                raise HTTPException(409, {"msg": "upload not finished", "received": state["received"], "size": state["size"]})
            _, src = _upload_paths(upload_id)
            name = "recording" + state["ext"]
        elif recording is not None and recording.filename:
            name = "recording" + (Path(recording.filename).suffix.lower() or ".mp4")
            src = Path(tmp) / name
            with open(src, "wb") as f:
                while piece := recording.file.read(CHUNK_BYTES):
                    f.write(piece)
                    if f.tell() > MAX_TAKE_BYTES:
                        raise HTTPException(413, f"recording is over the 1 GiB limit ({MAX_TAKE_BYTES:,} bytes)")
        else:
            src = orchestrator.FIXTURE_RUN / orchestrator.load_run(orchestrator.FIXTURE_RUN).recording
            name = src.name
        if src.parent != orchestrator.FIXTURE_RUN:
            try:
                check_take(src)
            except HTTPException:
                if upload_id:  # a rejected take is not kept
                    for f in _upload_paths(upload_id):
                        f.unlink(missing_ok=True)
                raise
        try:
            cfg = RunConfig(run_id=run_id, repo_url=repo_url, recording=name, brief=brief, mode=mode)
            run_dir = orchestrator.create_run(cfg, src, move=src.parent != orchestrator.FIXTURE_RUN)
        except ValueError as e:  # pydantic validation
            raise HTTPException(422, str(e))
    if upload_id:  # the take now lives in the run folder
        for f in _upload_paths(upload_id):
            f.unlink(missing_ok=True)
    if brief_text.strip():
        # Until A, B and C agree on an optional brief.text in run.json (runbook), it lives next to it.
        (run_dir / "brief.txt").write_text(brief_text.strip() + "\n")
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
