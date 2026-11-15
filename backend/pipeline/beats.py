"""Beat grid of the music bed (owner: B, ticket B18), so cuts can land on the beat.

A plain onset envelope (rise in loudness, ~23 ms steps), its autocorrelation for the tempo (60-180 BPM,
nudged toward 120) and the best phase for that tempo. Generated music beds keep a steady tempo, so one
constant grid is enough. Silent or beat-less audio gives [] and the timeline cuts as before.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SR = 22050
HOP = 256
MIN_BPM, MAX_BPM = 60, 180
MIN_STRENGTH = 0.15  # normalized autocorrelation at the tempo: below this the music has no clear beat
MIN_ONSET = 0.3      # loudness jump (log scale) the top 1% of attacks must reach: music ~0.9, a steady tone ~0.06


def _samples(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def onset_envelope(x: np.ndarray) -> np.ndarray:
    n = len(x) // HOP
    if n < 8:
        return np.zeros(0)
    frames = x[: n * HOP].reshape(n, HOP)
    energy = np.log1p(1000 * np.sqrt((frames ** 2).mean(axis=1)))
    onset = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
    if float(np.percentile(onset, 99)) < MIN_ONSET:  # a steady tone: only tiny ripples, no attacks
        return np.zeros(0)
    return onset - onset.mean()


def beat_times(path: Path) -> tuple[list[float], float]:
    """(beat times in seconds, tempo in BPM); ([], 0) when there is no clear beat."""
    x = _samples(Path(path))
    if len(x) == 0 or float(np.sqrt((x ** 2).mean())) < 1e-4:  # silence (cheap mode's music.wav)
        return [], 0.0
    env = onset_envelope(x)
    if env.size == 0 or not env.any():
        return [], 0.0
    fps = SR / HOP
    lo, hi = int(fps * 60 / MAX_BPM), int(fps * 60 / MIN_BPM)
    ac = np.correlate(env, env, mode="full")[env.size - 1:]
    if ac.size <= hi + 1 or ac[0] <= 0:
        return [], 0.0
    ac = ac / ac[0]  # normalized: 1 at lag 0, so the strength test doesn't depend on loudness
    lags = np.arange(lo, hi + 1)
    prior = np.exp(-0.5 * (np.log2((60 * fps / lags) / 120.0) / 0.9) ** 2)  # prefer ~120 BPM over halves/doubles
    k = int(lags[np.argmax(ac[lo:hi + 1] * prior)])
    if ac[k] < MIN_STRENGTH:  # a steady tone or noise: no beat to cut on
        return [], 0.0
    a, b, c = ac[k - 1], ac[k], ac[k + 1]  # parabolic peak: the period in fractions of a frame
    lag = k + (0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0)
    n = int((env.size - 1) / lag)
    starts = np.arange(0, lag, 0.25)
    phase = float(starts[np.argmax([env[np.round(p + lag * np.arange(n)).astype(int).clip(0, env.size - 1)].sum()
                                    for p in starts])])
    beats = [round(float((phase + i * lag) / fps), 3) for i in range(int((env.size - 1 - phase) / lag) + 1)]
    return beats, round(60 * fps / lag, 1)


BAR = 4              # beats per bar
SEAM_FADE_S = 0.02   # tiny fades at the loop seam, so the join never clicks


def bar_loop(bed: Path, out: Path, beats: list[float], bpm: float, horizon_s: float) -> list[float] | None:
    """Cut the bed to whole bars, starting on its first beat, and write it to `out`. Render loops that file,
    so a video longer than the bed keeps one steady beat: no jump at the seam. Returns the beat grid of the
    looped music up to `horizon_s` (beat i at i * period), or None when the bed is shorter than one bar."""
    if not beats or bpm <= 0:
        return None
    period = 60.0 / bpm
    bars = int((_length(bed) - beats[0]) / (BAR * period))
    if bars < 1:
        return None
    start, length = beats[0], bars * BAR * period
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(bed), "-af",
                    f"atrim=start={start:.4f}:duration={length:.4f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:d={SEAM_FADE_S},afade=t=out:st={length - SEAM_FADE_S:.4f}:d={SEAM_FADE_S}",
                    "-c:a", "pcm_s16le", str(out)], check=True, capture_output=True)
    return [round(i * period, 3) for i in range(int(horizon_s / period) + 1)]


def _length(path: Path) -> float:
    return len(_samples(path)) / SR


def snap(boundary: float, earliest: float, beats: list[float], max_shift: float) -> float:
    """The beat closest to `boundary` that is not before `earliest` (the voice must finish) and at most
    `max_shift` after `boundary`; the boundary itself when no beat qualifies."""
    ok = [b for b in beats if earliest <= b <= boundary + max_shift]
    return min(ok, key=lambda b: abs(b - boundary)) if ok else boundary
