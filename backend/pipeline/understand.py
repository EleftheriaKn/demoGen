"""Stage `understand` (owner: A). STUB.

Repo summary + timestamped moments (Gemini).

Reads repo_files.json, recording, transcript.json. Writes summary.json, moments.json.

Run alone: python -m backend.pipeline.understand runs/<run_id>
Until this is real, keep `understand` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage understand is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("understand")
