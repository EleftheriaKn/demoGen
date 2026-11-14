"""Stage `timeline` (owner: B). STUB.

Fit footage to voice durations.

Reads script.json, assets.json, candidates.json. Writes timeline.json.

Run alone: python -m backend.pipeline.timeline runs/<run_id>
Until this is real, keep `timeline` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage timeline is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("timeline")
