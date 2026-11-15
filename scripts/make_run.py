"""Make a run from one take plus extra videos, from the command line (owner: B, ticket B27).

    python -m scripts.make_run --repo https://github.com/EleftheriaKn/demoGen \\
        --take desktop.mp4 --extra team1.mov team2.mov funny.mp4 \\
        --brief-file brief.txt --mode full --start

Copies the videos into runs/<id>/ (take as recording.<ext>, extras as extras/<n>.<ext>), writes run.json
with brief.text and extra_recordings, and with --start runs the pipeline (same as the board's "Make the
video"). Ingest joins the take and the extras into one take (B23); clips without the app on screen become
`context` moments (B25) that the trend style uses for the beat montage and the problem / impact shots.
Limits as in the app: 10 minutes per video, 5 GiB for all of them together.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from backend.contracts import Brief, RunConfig
from backend.pipeline.ff import video_info
from backend.run import RUNS, create_run, new_run_id, run_pipeline

MAX_VIDEO_S = 600
MAX_TOTAL_BYTES = 5 * 1024 ** 3
MAX_EXTRAS = 10


def check(paths: list[Path]) -> None:
    total = 0
    for p in paths:
        if not p.is_file():
            raise SystemExit(f"not a file: {p}")
        try:
            dur = video_info(p).duration
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"{p}: not a video ffprobe can read ({type(e).__name__})")
        if dur > MAX_VIDEO_S + 0.5:
            raise SystemExit(f"{p}: {dur / 60:.1f} min; the limit is 10:00 per video")
        total += p.stat().st_size
    if total > MAX_TOTAL_BYTES:
        raise SystemExit(f"all videos together are {total / 1024 ** 3:.2f} GiB; the limit is 5 GiB")


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(prog="python -m scripts.make_run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="GitHub URL of the app")
    ap.add_argument("--take", type=Path, help="main recording (screen capture and/or camera); optional")
    ap.add_argument("--extra", type=Path, nargs="*", default=[], help=f"up to {MAX_EXTRAS} more videos")
    ap.add_argument("--brief-file", type=Path, help="the judging brief as text")
    ap.add_argument("--criteria", default="usefulness,craft,agent-ready,honesty")
    ap.add_argument("--time-limit", type=int, default=120)
    ap.add_argument("--language", default="en")
    ap.add_argument("--second-language")
    ap.add_argument("--mode", choices=["full", "cheap"], default="full")
    ap.add_argument("--style", choices=["trend", "classic"], help="edit style; default trend")
    ap.add_argument("--start", action="store_true", help="run the pipeline right away")
    a = ap.parse_args(argv)

    extras = list(a.extra)
    if len(extras) > MAX_EXTRAS:
        raise SystemExit(f"{len(extras)} extra videos; at most {MAX_EXTRAS}")
    take = a.take or (extras.pop(0) if extras else None)  # no main take: the first extra leads
    check([p for p in [take, *extras] if p])
    brief = Brief(criteria=[c.strip() for c in a.criteria.split(",") if c.strip()], time_limit_s=a.time_limit,
                  language=a.language, second_language=a.second_language,
                  text=a.brief_file.read_text(encoding="utf-8").strip() or None if a.brief_file else None)
    run_id = new_run_id()
    rel = [f"extras/{i}{p.suffix.lower() or '.mp4'}" for i, p in enumerate(extras, 1)]
    cfg = RunConfig(run_id=run_id, repo_url=a.repo, recording=f"recording{(take.suffix.lower() if take else '.mp4')}",
                    brief=brief, mode=a.mode, extra_recordings=rel or None, style=a.style)
    if take:
        run_dir = create_run(cfg, take)
    else:  # repo + brief only (B17 / B26 records the app itself)
        run_dir = RUNS / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    for src, dst in zip(extras, rel):
        (run_dir / dst).parent.mkdir(exist_ok=True)
        shutil.copy2(src, run_dir / dst)
    if brief.text:
        (run_dir / "brief.txt").write_text(brief.text + "\n", encoding="utf-8")
    print(f"{run_dir}  ({1 if take else 0} take + {len(extras)} extra videos, {a.mode}, {a.style or 'trend'})")
    if a.start:
        print(run_pipeline(run_dir))
    else:
        print(f"start it: python -m backend.run {run_dir}")
    return run_dir


if __name__ == "__main__":
    main(sys.argv[1:])
