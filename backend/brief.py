"""The pasted judging brief, for every prompt (owner: B, ticket B19).

The user pastes the hackathon's judging brief on the phone; until ``brief.text`` is in ``run.json`` (C14) the
app saves it as ``brief.txt`` in the run. ``block()`` turns it into a short section that ``gemini.prompt()``
puts wherever a prompt has ``{brief_block}``. With no brief the block is empty, so the prompt text is exactly
what it was (cached answers and recorded tests stay valid).

The brief steers what to show and in what order. It is never evidence that the app does something.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend import events

MAX_CHARS = 2500  # a brief is a paragraph or two; cap it so a pasted page can't flood every prompt

INTRO = ("\n\nThe judging brief, as the user pasted it. Use it to decide what matters most, what to show first "
         "and how to say it; it is not evidence of what the app does.\n<brief>\n")


def text(run_dir: Path | None) -> str:
    """The run's brief text, or "" (run.json ``brief.text`` when the contract has it, else ``brief.txt``)."""
    if run_dir is None:
        return ""
    d = Path(run_dir)
    try:
        b = json.loads((d / "run.json").read_text(encoding="utf-8")).get("brief", {}).get("text") or ""
    except (OSError, ValueError):
        b = ""
    if not b and (d / "brief.txt").exists():
        b = (d / "brief.txt").read_text(encoding="utf-8")
    b = " ".join(b.split()) if len(b) < 200 else b.strip()
    return b[:MAX_CHARS]


def block(run_dir: Path | None = None) -> str:
    """The prompt section for the current run's brief ("" when there is none). Defaults to the bound run."""
    b = text(run_dir if run_dir is not None else events._run_dir)  # noqa: SLF001  (run.py binds one run per process)
    return f"{INTRO}{b}\n</brief>" if b else ""
