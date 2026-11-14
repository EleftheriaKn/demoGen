"""Stage `transcribe` (owner: C). STUB.

Speech-to-text via fal.

Reads audio.wav. Writes transcript.json.

Run alone: python -m backend.pipeline.transcribe runs/<run_id>
Until this is real, keep `transcribe` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage transcribe is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("transcribe")
