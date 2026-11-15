"""Stage transcribe: silent take, take with sound, word clamping, cache hit. fal and ffprobe are faked."""

from __future__ import annotations

from backend import events
from backend.clients import fal
from backend.contracts import Brief, RunConfig, Transcript
from backend.pipeline import ff, transcribe

RUN = RunConfig(run_id="t", repo_url="https://github.com/x/y", recording="recording.mp4",
                brief=Brief(criteria=["craft"], time_limit_s=60, language="en"), mode="cheap")


def _setup(tmp_path, monkeypatch, has_audio, answer=None):
    monkeypatch.setenv("DEMOCUT_CACHE", str(tmp_path / "cache"))
    events.bind(tmp_path, "t")  # emit() is module-global; don't write into an earlier test's deleted run
    (tmp_path / "audio.wav").write_bytes(b"RIFF fake " + str(answer).encode())
    calls = []
    monkeypatch.setattr(ff, "has_audio", lambda p: has_audio)
    monkeypatch.setattr(ff, "duration", lambda p: 10.0)
    monkeypatch.setattr(fal, "transcribe", lambda p: calls.append(p) or answer)
    return calls


def _read(tmp_path) -> Transcript:
    return Transcript.model_validate_json((tmp_path / "transcript.json").read_text(encoding="utf-8"))


def test_silent_take_writes_empty_transcript_without_fal(tmp_path, monkeypatch):
    calls = _setup(tmp_path, monkeypatch, has_audio=False)
    transcribe.run(tmp_path, RUN)
    assert calls == []
    assert _read(tmp_path) == Transcript(language="en", text="", words=[])


def test_sound_take_writes_words_and_clamps(tmp_path, monkeypatch):
    answer = {"language": "el", "text": "a b c d e", "words": [
        {"word": "a", "start": 0.5, "end": 1.0},
        {"word": "b", "start": -0.2, "end": 0.4},   # clamped to 0
        {"word": "c", "start": 9.8, "end": 10.6},   # clamped to 10
        {"word": "d", "start": 11.0, "end": 12.0},  # past the end: empty span, dropped
        {"word": " ", "start": 2.0, "end": 2.5},    # blank, dropped
        {"word": "e", "start": 3.0, "end": 3.0},    # zero length, dropped
    ]}
    calls = _setup(tmp_path, monkeypatch, has_audio=True, answer=answer)
    transcribe.run(tmp_path, RUN)
    t = _read(tmp_path)
    assert calls == [tmp_path / "audio.wav"]
    assert t.language == "el" and t.text == "a b c d e"
    assert [(w.word, w.start, w.end) for w in t.words] == [("a", 0.5, 1.0), ("b", 0.0, 0.4), ("c", 9.8, 10.0)]


def test_second_run_is_cache_hit(tmp_path, monkeypatch):
    answer = {"language": "en", "text": "hi", "words": [{"word": "hi", "start": 0.0, "end": 0.3}]}
    calls = _setup(tmp_path, monkeypatch, has_audio=True, answer=answer)
    transcribe.run(tmp_path, RUN)
    (tmp_path / "transcript.json").unlink()
    transcribe.run(tmp_path, RUN)
    assert len(calls) == 1 and _read(tmp_path).words[0].word == "hi"
