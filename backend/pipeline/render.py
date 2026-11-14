"""Stage `render` (owner: B). STUB.

Per-shot render, then concat.

Reads timeline.json, assets, captions. Writes shots/, final.mp4.

Run alone: python -m backend.pipeline.render runs/<run_id>
Until this is real, keep `render` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage render is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("render")
