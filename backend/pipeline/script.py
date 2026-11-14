"""Stage `script` (owner: A). STUB.

Hooks, shot list and truth check (Gemini).

Reads summary.json, moments.json, frame_scores.json, run.brief. Writes script.json, decisions.json.

Run alone: python -m backend.pipeline.script runs/<run_id>
Until this is real, keep `script` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage script is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("script")
