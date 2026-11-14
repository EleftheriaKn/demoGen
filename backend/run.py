"""Orchestrator (owner: C).

Runs every stage in order against one run folder. Stages talk only through files there.

    DEMOCUT_FAKE=all python -m backend.run                 # new run seeded from fixtures/
    DEMOCUT_FAKE=understand,script python -m backend.run runs/my_run

``DEMOCUT_FAKE`` lists stages to fake: instead of running them, their outputs
(``STAGE_OUTPUTS``) are copied from ``fixtures/run/``. ``all`` fakes every stage.
Take stages off the list as they become real.

After the rubric stage, if ``decisions.json`` has a ``recut``, timeline runs once more
before captions and render. There is only ever one recut.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Callable

from backend import events
from backend.contracts import MODELS, STAGE_OUTPUTS, STAGES, Decisions, RunConfig

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_RUN = ROOT / "fixtures" / "run"
RUNS = ROOT / "runs"

StageFn = Callable[[Path, RunConfig], None]

# stage -> "module:function". Every function is run(run_dir, run) -> None.
STAGE_FUNCS: dict[str, str] = {
    "ingest": "backend.pipeline.ingest:run",  # B
    "transcribe": "backend.pipeline.transcribe:run",  # C
    "understand": "backend.pipeline.understand:run",  # A
    "candidates": "backend.pipeline.candidates:run",  # B
    "judge_frames": "backend.pipeline.judge:run_frames",  # A
    "script": "backend.pipeline.script:run",  # A
    "assets": "backend.pipeline.assets:run",  # C
    "timeline": "backend.pipeline.timeline:run",  # B
    "rubric": "backend.pipeline.judge:run_rubric",  # A
    "captions": "backend.pipeline.captions:run",  # B
    "render": "backend.pipeline.render:run",  # B
}
assert list(STAGE_FUNCS) == STAGES


def fake_stages() -> set[str]:
    raw = os.environ.get("DEMOCUT_FAKE", "").strip()
    if raw == "all":
        return set(STAGES)
    names = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = names - set(STAGES)
    if unknown:
        raise SystemExit(f"DEMOCUT_FAKE: unknown stage(s) {sorted(unknown)}; valid: all, {', '.join(STAGES)}")
    return names


def stage_fn(stage: str) -> StageFn:
    module, fn = STAGE_FUNCS[stage].split(":")
    return getattr(importlib.import_module(module), fn)


def copy_fixture_outputs(stage: str, run_dir: Path) -> None:
    for out in STAGE_OUTPUTS[stage]:
        src, dst = FIXTURE_RUN / out, run_dir / out
        if out.endswith("/"):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def check_outputs(stage: str, run_dir: Path) -> None:
    """Every output exists; every JSON output validates against its contract."""
    for out in STAGE_OUTPUTS[stage]:
        path = run_dir / out
        if not path.exists():
            raise RuntimeError(f"stage {stage} did not write {out}")
        if out in MODELS:
            MODELS[out].model_validate_json(path.read_text())


def load_run(run_dir: Path) -> RunConfig:
    return RunConfig.model_validate_json((run_dir / "run.json").read_text())


def new_run_from_fixture() -> Path:
    """Create runs/<id>/ with the fixture's inputs (run.json + recording)."""
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    cfg = load_run(FIXTURE_RUN).model_copy(update={"run_id": run_id})
    (run_dir / "run.json").write_text(cfg.model_dump_json(indent=2))
    shutil.copy2(FIXTURE_RUN / cfg.recording, run_dir / cfg.recording)
    return run_dir


def run_stage(stage: str, run_dir: Path, cfg: RunConfig, fake: bool, progress: float) -> None:
    events.emit(stage, f"{stage} started" + (" (fake)" if fake else ""))
    if fake:
        copy_fixture_outputs(stage, run_dir)
    else:
        stage_fn(stage)(run_dir, cfg)
    check_outputs(stage, run_dir)
    events.emit(stage, f"{stage} done", progress, {"outputs": STAGE_OUTPUTS[stage], "fake": fake})


def run_pipeline(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    cfg = load_run(run_dir)
    fakes = fake_stages()
    events.bind(run_dir, cfg.run_id)
    events.emit("run", "Run started", 0.0, {"mode": cfg.mode, "fake": sorted(fakes)})
    recut_done = False
    try:
        for i, stage in enumerate(STAGES):
            run_stage(stage, run_dir, cfg, stage in fakes, (i + 1) / len(STAGES))
            if stage == "rubric" and not recut_done:
                recut = Decisions.model_validate_json((run_dir / "decisions.json").read_text()).recut
                if recut is not None:
                    recut_done = True
                    events.emit("run", f"Recut {recut.shot_id}: {recut.reason}", data=recut.model_dump())
                    run_stage("timeline", run_dir, cfg, "timeline" in fakes, (i + 1) / len(STAGES))
    except Exception as e:
        events.emit("run", f"Run failed: {e}", data={"error": type(e).__name__})
        raise
    events.emit("run", "Run done", 1.0, {"final": "final.mp4"})
    return run_dir / "final.mp4"


def stage_main(stage: str) -> None:
    """CLI for one stage: ``python -m backend.pipeline.<module> runs/<run_id>``."""
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: python -m {STAGE_FUNCS[stage].split(':')[0]} runs/<run_id>")
    run_dir = Path(sys.argv[1])
    cfg = load_run(run_dir)
    events.bind(run_dir, cfg.run_id)
    run_stage(stage, run_dir, cfg, fake=False, progress=0.0)


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else new_run_from_fixture()
    final = run_pipeline(run_dir)
    print(final)


if __name__ == "__main__":
    main()
