"""Decision log helper (owner: A). The one way stages append to ``decisions.json``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from backend.contracts import Decision, Decisions, JudgeAnswer, Stage


def append_decision(
    run_dir: Path,
    *,
    stage: Stage,
    kind: str,
    subject: str,
    options: list[str],
    chosen: str,
    questions: list[str],
    answers: list[Union[JudgeAnswer, dict]],
    judge_score: float,
    reason: str,
    heuristic_score: Optional[float] = None,
) -> Decision:
    """Append one decision with the next ``dNN`` id and write the file back.

    Raises ValueError (file untouched) if ``chosen`` isn't an option, the answers don't
    follow ``questions`` in order, or the result breaks the contract.
    """
    path = Path(run_dir) / "decisions.json"
    log = Decisions.model_validate_json(path.read_text()) if path.exists() else Decisions(decisions=[], rubric=[])

    answers = [JudgeAnswer.model_validate(a) for a in answers]
    if [a.question for a in answers] != questions:
        raise ValueError(f"{kind} {subject}: answers must follow the question order {questions}")

    nums = (d.decision_id[1:] for d in log.decisions if d.decision_id.startswith("d"))
    n = max((int(x) for x in nums if x.isdigit()), default=0) + 1
    decision = Decision(
        decision_id=f"d{n:02d}",
        stage=stage,
        kind=kind,
        subject=subject,
        options=options,
        chosen=chosen,
        answers=answers,
        heuristic_score=heuristic_score,
        judge_score=judge_score,
        reason=reason,
    )
    log = Decisions.model_validate({**log.model_dump(), "decisions": [*log.decisions, decision]})
    path.write_text(log.model_dump_json(indent=2))
    return decision
