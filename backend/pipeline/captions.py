"""Stage `captions` (owner: B). STUB.

Captions from the timeline.

Reads timeline.json. Writes captions.srt, captions.ass.

Run alone: python -m backend.pipeline.captions runs/<run_id>
Until this is real, keep `captions` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage captions is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("captions")
