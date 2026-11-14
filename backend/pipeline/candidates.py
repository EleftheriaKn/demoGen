"""Stage `candidates` (owner: B). STUB.

Scene detection + frame samples.

Reads recording, moments.json. Writes candidates.json, frames/.

Run alone: python -m backend.pipeline.candidates runs/<run_id>
Until this is real, keep `candidates` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage candidates is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("candidates")
