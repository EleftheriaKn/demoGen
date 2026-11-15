"""Stage `transcribe` (owner: A).

Speech-to-text via fal (ElevenLabs Scribe v2).

Reads the recording (to check for sound) and audio.wav. Writes transcript.json.

A take with no audio stream gets an empty transcript and no fal call: ingest writes silent
audio.wav for those, which must never go to Scribe. A take with sound is sent once per
audio.wav content (``cache.json_call``), so a repeat run costs nothing. Word times are clamped
to the recording length; words left empty or reversed are dropped.

Run alone: python -m backend.pipeline.transcribe runs/<run_id>
"""

from __future__ import annotations

from pathlib import Path

from backend import cache, events
from backend.clients import fal
from backend.contracts import RunConfig, Transcript, Word
from backend.pipeline import ff


def clamp_words(words: list[dict], duration: float) -> list[Word]:
    """Clamp start/end into [0, duration]; drop words that end up blank or with end <= start."""
    out = []
    for w in words:
        start, end = max(0.0, min(w["start"], duration)), max(0.0, min(w["end"], duration))
        if w["word"].strip() and end > start:
            out.append(Word(word=w["word"], start=start, end=end))
    return out


def run(run_dir: Path, run: RunConfig) -> None:
    recording = run_dir / run.recording
    if not ff.has_audio(recording):
        transcript = Transcript(language=run.brief.language, text="", words=[])
        events.emit("transcribe", "Recording has no sound: empty transcript, no fal call")
    else:
        audio = run_dir / "audio.wav"
        raw = cache.json_call("transcribe", {"endpoint": fal.SCRIBE_ENDPOINT, "audio": audio},
                              lambda: cache.retry(lambda: fal.transcribe(audio)))
        words = clamp_words(raw["words"], ff.duration(recording))
        # fal.transcribe already maps eng->en, ell->el; Scribe may return no language on wordless audio.
        transcript = Transcript(language=raw["language"] or run.brief.language, text=raw["text"], words=words)
        dropped = len(raw["words"]) - len(words)
        events.emit("transcribe", f"Transcript: {len(words)} words ({transcript.language})"
                    + (f", dropped {dropped} outside the recording" if dropped else ""))
    (run_dir / "transcript.json").write_text(transcript.model_dump_json(indent=2), encoding="utf-8")


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("transcribe")
