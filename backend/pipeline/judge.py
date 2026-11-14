"""Judge (owner: A). STUB.

Two stages plus one function other tracks call:

- ``run_frames``  stage ``judge_frames``: reads candidates.json + frames/, writes
  frame_scores.json and appends ``frame_pick`` decisions to decisions.json.
- ``run_rubric``  stage ``rubric``: reads timeline.json, script.json, brief; writes the
  rubric (and an optional ``recut``) into decisions.json.
- ``pick_best``   called by C's assets stage to choose among N generated drafts.

Run alone: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict, Union

from backend.contracts import JudgeAnswer, RunConfig


class PickResult(TypedDict):
    """What ``pick_best`` returns. Enough for the caller to build a ``take_pick`` Decision."""

    index: int  # index into ``candidates`` of the winner
    scores: list[float]  # one 0-1 score per candidate, same order as ``candidates``
    answers: list[dict]  # JudgeAnswer dicts for the winner, one per question, same order
    judge_score: float  # 0-1, == scores[index]
    reason: str  # one line, shown in the UI


def pick_best(candidates: list[Union[str, Path]], questions: list[str]) -> PickResult:
    """Pick the best of N candidate files (images, audio or video) against ``questions``.

    STUB: always picks index 0 with neutral scores. The return shape is final.
    """
    if not candidates:
        raise ValueError("pick_best needs at least one candidate")
    answers = [JudgeAnswer(question=q, score=0.5, reason="stub judge").model_dump() for q in questions]
    return PickResult(
        index=0,
        scores=[0.5] * len(candidates),
        answers=answers,
        judge_score=0.5,
        reason="stub judge: picked the first candidate",
    )


def run_frames(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage judge_frames is not built yet; add it to DEMOCUT_FAKE")


def run_rubric(run_dir: Path, run: RunConfig) -> None:
    raise NotImplementedError("stage rubric is not built yet; add it to DEMOCUT_FAKE")


if __name__ == "__main__":
    from backend.run import stage_main

    which = {"frames": "judge_frames", "rubric": "rubric"}
    if len(sys.argv) != 3 or sys.argv[1] not in which:
        raise SystemExit("usage: python -m backend.pipeline.judge {frames|rubric} runs/<run_id>")
    stage = which[sys.argv.pop(1)]
    stage_main(stage)
