"""Progress events (owner: C).

``emit()`` appends one validated ``Event`` line to ``<run_dir>/events.jsonl``. The API
streams the same JSON over SSE. ``run.py`` calls ``bind()`` once per run, so stages can
call ``emit()`` without passing the run folder around.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.contracts import Event

_run_dir: Optional[Path] = None
_run_id: Optional[str] = None
_progress: float = 0.0


def bind(run_dir: Path, run_id: str) -> None:
    """Point emit() at a run folder. Resets progress to 0."""
    global _run_dir, _run_id, _progress
    _run_dir, _run_id, _progress = Path(run_dir), run_id, 0.0


def emit(stage: str, msg: str, progress: Optional[float] = None, data: Optional[dict] = None) -> Event:
    """Log one event. ``progress`` is overall run progress 0-1; omitted = keep the last value.

    Progress never goes backwards: a lower value is raised to the last one.
    """
    global _progress
    if progress is not None:
        _progress = max(_progress, min(1.0, progress))
    event = Event(
        run_id=_run_id or "unbound",
        stage=stage,
        msg=msg,
        progress=round(_progress, 3),
        ts=datetime.now(timezone.utc).isoformat(),
        data=data or {},
    )
    line = event.model_dump_json()
    if _run_dir is not None:
        with open(_run_dir / "events.jsonl", "a") as f:
            f.write(line + "\n")
    print(f"[{event.stage}] {event.msg}", flush=True)
    return event
