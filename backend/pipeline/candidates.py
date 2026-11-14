"""Stage `candidates` (owner: B).

Every clip the judge may pick from: scenes found by cut detection, plus one clip per moment from
moments.json with its edges snapped to a real cut within 1.5 s. Three sample frames per clip.

Cut detection is ffmpeg's `scdet` on a 320 px copy: frame-accurate timestamps on variable-frame-rate
phone takes, and ~10x faster than PySceneDetect on 10-bit HEVC.

Reads the recording, moments.json. Writes candidates.json, frames/.

Run alone: python -m backend.pipeline.candidates runs/<run_id>
"""

from __future__ import annotations

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import events
from backend.contracts import Candidates, Moment, Moments, RunConfig, Segment, frame_time
from backend.pipeline.ff import ff, hdr_to_sdr, video_info

SCENE_THRESHOLD = 10.0   # scdet score (0-100); page changes in a screen recording score far above this
SNAP_S = 1.5             # moment edges snap to a cut this close (runbook)
MIN_SCENE_S = 1.5        # shorter scenes merge into a neighbour
MAX_SCENE_S = 12.0       # longer scenes (no cut for a while) are split evenly
MAX_SCENES = 40          # keeps the judge's frame count (3 per segment) bounded on 7-minute takes
MIN_MOMENT_S = 0.3
SAME_S = 0.25            # a scene this close to a moment at both ends is the same clip: keep the moment
FRAME_AT = (0.25, 0.5, 0.75)
FRAME_WIDTH = 640
WORKERS = 4


def scene_cuts(recording: Path) -> list[float]:
    """Times (s) where the picture changes a lot, from ffmpeg scdet."""
    info = video_info(recording)
    vf = (f"{hdr_to_sdr(info)}scale=320:-2,scdet=threshold={SCENE_THRESHOLD}:sc_pass=1,"
          "metadata=print:key=lavfi.scd.time")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(recording), "-an", "-vf", vf, "-f", "null", "-"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"scene detection failed: {p.stderr.strip()[-800:]}")
    return sorted({round(float(t), 3) for t in re.findall(r"lavfi\.scd\.time=([\d.]+)", p.stderr)})


def scenes_from_cuts(cuts: list[float], dur: float) -> list[tuple[float, float]]:
    """Cuts -> scenes: drop edge cuts, merge short scenes, split long ones, then cap the count
    (the cap wins: on a busy 7-minute take some scenes end up longer than MAX_SCENE_S)."""
    edges = [0.0] + [c for c in cuts if MIN_SCENE_S / 2 < c < dur - MIN_SCENE_S / 2] + [dur]
    merged: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        if b <= a:
            continue
        if merged and (b - a < MIN_SCENE_S or merged[-1][1] - merged[-1][0] < MIN_SCENE_S):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    scenes = []
    for a, b in merged:
        n = max(1, -int(-(b - a) // MAX_SCENE_S))  # ceil
        step = (b - a) / n
        scenes += [(round(a + k * step, 3), round(a + (k + 1) * step, 3)) for k in range(n)]
    length = lambda k: scenes[k][1] - scenes[k][0]  # noqa: E731
    while len(scenes) > MAX_SCENES:  # merge the shortest scene into its shorter neighbour
        i = min(range(len(scenes)), key=length)
        j = i - 1 if i == len(scenes) - 1 or (i > 0 and length(i - 1) <= length(i + 1)) else i + 1
        lo, hi = min(i, j), max(i, j)
        scenes[lo:hi + 1] = [(scenes[lo][0], scenes[hi][1])]
    return scenes


def snap(t: float, edges: list[float]) -> float:
    near = min(edges, key=lambda e: abs(e - t))
    return near if abs(near - t) <= SNAP_S else t


def moment_span(m: Moment, edges: list[float], dur: float) -> tuple[float, float] | None:
    """Moment edges snapped to real cuts and clamped to the take; None when nothing is left."""
    a, b = max(0.0, min(m.start, dur)), max(0.0, min(m.end, dur))
    if b - a < MIN_MOMENT_S:
        return None
    sa, sb = snap(a, edges), snap(b, edges)
    if sb - sa < max(MIN_MOMENT_S, (b - a) / 2):  # snapping squashed it: keep A's own times
        sa, sb = a, b
    return round(sa, 3), round(sb, 3)


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def frame_name(seg_id: str, t: float) -> str:
    return f"frames/{seg_id}_{f'{t:.2f}'.rstrip('0').rstrip('.')}s.jpg"


def frame_times(start: float, end: float) -> list[float]:
    return [min(max(round(start + f * (end - start), 2), start), end) for f in FRAME_AT]


def build_segments(cuts: list[float], dur: float, moments: list[Moment]) -> list[Segment]:
    edges = [0.0, *cuts, dur]
    dead = [(m.start, m.end) for m in moments if m.kind == "dead"]
    scenes = [s for s in scenes_from_cuts(cuts, dur)
              if sum(overlap(s, d) for d in dead) < 0.5 * (s[1] - s[0])]
    moment_spans = [(*span, "moment", m.moment_id) for m in moments
                    if m.kind != "dead" and (span := moment_span(m, edges, dur))]
    same = lambda s, m: abs(s[0] - m[0]) <= SAME_S and abs(s[1] - m[1]) <= SAME_S  # noqa: E731
    spans = [(s[0], s[1], "scene", None) for s in scenes if not any(same(s, m) for m in moment_spans)]
    spans += moment_spans
    spans.sort(key=lambda s: (s[0], s[2] == "moment", s[1]))
    segs = []
    for i, (a, b, source, mid) in enumerate(spans, 1):
        sid = f"c{i:02d}"
        segs.append(Segment(segment_id=sid, start=a, end=b, source=source, moment_id=mid,
                            frames=[frame_name(sid, t) for t in frame_times(a, b)]))
    return segs


def extract_frames(d: Path, recording: Path, segs: list[Segment]) -> None:
    hdr = hdr_to_sdr(video_info(recording))
    jobs = [(f, f"{frame_time(f):.3f}") for s in segs for f in s.frames]

    def grab(job):
        path, t = job
        ff("-ss", t, "-i", recording, "-frames:v", "1", "-vf", f"{hdr}scale={FRAME_WIDTH}:-2", "-q:v", "3", d / path)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(grab, jobs))


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir)
    recording = d / run.recording
    dur = video_info(recording).duration
    mpath = d / "moments.json"
    moments = Moments.model_validate_json(mpath.read_text()).moments if mpath.exists() else []
    if not moments:
        events.emit("candidates", "no moments.json yet: scene cuts only")

    cuts = scene_cuts(recording)
    segs = build_segments(cuts, dur, moments)
    shutil.rmtree(d / "frames", ignore_errors=True)
    (d / "frames").mkdir()
    extract_frames(d, recording, segs)
    (d / "candidates.json").write_text(Candidates(segments=segs).model_dump_json(indent=2))
    n_moment = sum(s.source == "moment" for s in segs)
    events.emit("candidates", f"{len(cuts)} cuts -> {len(segs) - n_moment} scene + {n_moment} moment clips, "
                              f"{3 * len(segs)} frames", data={"cuts": cuts})


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("candidates")
