"""Stage `assets` (owner: C). STUB.

Voice, music, cards, b-roll via fal; picks with judge.pick_best.

Reads script.json. Writes assets.json, voice/, music.wav, cards/, decisions.json (take_pick).

Run alone: python -m backend.pipeline.assets runs/<run_id>
Until this is real, keep `assets` in DEMOCUT_FAKE.
"""

from __future__ import annotations

from pathlib import Path

from backend.contracts import RunConfig


def run(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage assets is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("assets")
