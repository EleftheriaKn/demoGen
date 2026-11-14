"""Stage `ingest` (owner: B). STUB.

Clone the repo, extract audio.

Reads run.json. Writes repo_files.json, audio.wav, screenshots/.

Run alone: python -m backend.pipeline.ingest runs/<run_id>
Until this is real, keep `ingest` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage ingest is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("ingest")
