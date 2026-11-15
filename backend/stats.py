"""Pitch numbers from a finished run (owner: C, ticket C9).

    python -m backend.stats runs/<run_id>            # markdown, ready to paste into docs/PITCH.md
    python -m backend.stats runs/<run_id> --json

Reads only the run's own files (events.jsonl, timeline.json, script.json, decisions.json,
final.mp4). Prints what the runbook asks to measure: wall-clock time from repo URL to MP4, time per
stage, video length, real-footage share, claims made vs rejected (with the example to show), and
the hook. fal cost is not in the run files: read it from the fal dashboard.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def _load(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _events(run_dir: Path) -> list[dict]:
    log = run_dir / "events.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _ts(ev: dict) -> datetime:
    return datetime.fromisoformat(ev["ts"])


def _video_seconds(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout
        return float(out.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def collect(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    evs = _events(run_dir)
    out: dict = {"run_id": run_dir.name}

    # Wall clock: the last "Run started" (a resumed run starts again) to "Run done".
    starts = [e for e in evs if e.get("stage") == "run" and e.get("msg") == "Run started"]
    done = [e for e in evs if e.get("stage") == "run" and e.get("msg") == "Run done"]
    out["status"] = "done" if done else ("failed" if any(str(e.get("msg", "")).startswith("Run failed") for e in evs) else "unfinished")
    if starts and done:
        first, last = _ts(starts[0]), _ts(done[-1])
        out["wall_clock_s"] = round((last - first).total_seconds(), 1)
        out["resumed"] = len(starts) > 1
        out["faked_stages"] = starts[-1].get("data", {}).get("fake", [])

    # Time per stage: "<stage> started" -> "<stage> done" (last pair wins: a recut reruns timeline).
    stage_s: dict[str, float] = {}
    began: dict[str, datetime] = {}
    for e in evs:
        st, msg = e.get("stage"), str(e.get("msg", ""))
        if st == "run":
            continue
        if msg.startswith(f"{st} started"):
            began[st] = _ts(e)
        elif msg == f"{st} done" and st in began:
            stage_s[st] = round((_ts(e) - began[st]).total_seconds(), 1)
    out["stage_s"] = stage_s

    out["video_s"] = _video_seconds(run_dir / "final.mp4")

    timeline = _load(run_dir / "timeline.json")
    if timeline:
        total = timeline["duration_s"]
        real = sum(s["duration_s"] for s in timeline["shots"] if s["visual"]["kind"] == "footage")
        out["shots"] = len(timeline["shots"])
        out["real_footage_share"] = round(real / total, 3) if total else None

    script = _load(run_dir / "script.json")
    decisions = (_load(run_dir / "decisions.json") or {}).get("decisions", [])
    reasons = {d["subject"]: d["reason"] for d in decisions if d.get("kind") == "claim_check"}
    if script:
        claims = [c for s in script["shots"] for c in s["claims"]]
        count = {k: sum(c["status"] == k for c in claims) for k in ("backed", "rewritten", "dropped", "pending")}
        out["claims"] = {"made": len(claims), **count}
        out["rejected_examples"] = [
            {"claim": c["text"], "status": c["status"], "why": reasons.get(c["claim_id"], "")}
            for c in claims if c["status"] in ("dropped", "rewritten")
        ]
        hook = next((h for h in script["hooks"] if h["hook_id"] == script["chosen_hook_id"]), None)
        out["hook"] = hook["text"] if hook else None
        out["hooks_considered"] = len(script["hooks"])
    return out


def _mmss(s: Optional[float]) -> str:
    return "–" if s is None else f"{int(s // 60)}:{int(round(s % 60)):02d}"


def markdown(n: dict) -> str:
    lines = [f"## Pitch numbers · {n['run_id']} ({n['status']})", "", "| Number | Value |", "|---|---|"]
    if "wall_clock_s" in n:
        extra = " (resumed: not a clean number)" if n.get("resumed") else ""
        if n.get("faked_stages"):
            extra += f" (FAKE stages: {', '.join(n['faked_stages'])}; not a real number)"
        lines.append(f"| Repo URL → MP4, wall clock (m:ss) | {_mmss(n['wall_clock_s'])}{extra} |")
    lines.append(f"| Final video length | {_mmss(n.get('video_s'))} |")
    if "real_footage_share" in n:
        lines.append(f"| Real footage | {round(100 * n['real_footage_share'])}% of {n['shots']} shots' runtime |")
    if "claims" in n:
        c = n["claims"]
        lines.append(f"| Claims made / backed / rewritten / rejected | {c['made']} / {c['backed']} / {c['rewritten']} / {c['dropped']} |")
    if n.get("hook"):
        lines.append(f"| Hook (best of {n['hooks_considered']}) | “{n['hook']}” |")
    lines.append("| fal cost | read it on the fal dashboard |")
    lines.append("| Human minutes | stopwatch: recording + filling the board |")
    if n.get("rejected_examples"):
        lines += ["", "**Claims the truth check changed** (pick one for the pitch):", ""]
        lines += [f"- {e['status']}: “{e['claim']}”" + (f" · {e['why']}" if e["why"] else "") for e in n["rejected_examples"]]
    if n.get("stage_s"):
        lines += ["", "**Time per stage**: " + " · ".join(f"{k} {v:.0f}s" for k, v in n["stage_s"].items())]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m backend.stats", description="Pitch numbers from a finished run.")
    ap.add_argument("run_dir", help="runs/<run_id>")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir)
    if not (run_dir / "events.jsonl").exists():
        raise SystemExit(f"{run_dir}: no events.jsonl (is this a run folder?)")
    n = collect(run_dir)
    print(json.dumps(n, indent=2, ensure_ascii=False) if args.json else markdown(n))


if __name__ == "__main__":
    sys.exit(main())
