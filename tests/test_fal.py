"""fal client field mapping, with fal_client faked (shapes copied from real calls on 2026-11-15)."""

from __future__ import annotations

import socket

import pytest

from backend import cache
from backend.clients import fal

RAW = {
    fal.TTS_ENDPOINT: {"audio": {"url": "u-tts"}},
    fal.CARD_ENDPOINT: {"images": [{"url": "u-card", "width": 1024, "height": 576}], "seed": 1, "prompt": "p"},
    fal.CARD_EDIT_ENDPOINT: {"images": [{"url": "u-edit"}], "description": ""},
    fal.MUSIC_ENDPOINT: {"audio": {"url": "u-music", "content_type": "audio/mpeg"}},
    fal.SCRIBE_ENDPOINT: {
        "text": "Add a todo.",
        "language_code": "eng",
        "language_probability": 0.9,
        "words": [
            {"text": "Add", "start": 0.1, "end": 0.3, "type": "word", "speaker_id": None},
            {"text": " ", "start": 0.3, "end": 0.35, "type": "spacing", "speaker_id": None},
            {"text": "(click)", "start": 0.35, "end": 0.4, "type": "audio_event", "speaker_id": None},
            {"text": "todo.", "start": 0.4, "end": 0.7, "type": "word", "speaker_id": None},
        ],
    },
}


def test_fal_field_mapping(monkeypatch, tmp_path):
    calls, downloads = [], []
    monkeypatch.setattr(fal.fal_client, "subscribe", lambda ep, arguments: calls.append((ep, arguments)) or RAW[ep])
    monkeypatch.setattr(fal.fal_client, "upload_file", lambda p: f"up:{p}")
    monkeypatch.setattr(fal, "_download", lambda url, out: downloads.append(url))

    fal.tts("hi", "en", tmp_path / "v.mp3")
    fal.card_image("bg", tmp_path / "c.png")
    fal.card_edit("bg", [tmp_path / "f.jpg"], tmp_path / "e.png")
    fal.music("calm", 10, tmp_path / "m.mp3")
    transcript = fal.transcribe(tmp_path / "a.wav")

    assert downloads == ["u-tts", "u-card", "u-edit", "u-music"]
    args = dict(calls)
    assert args[fal.TTS_ENDPOINT]["language_code"] == "en"
    assert args[fal.CARD_EDIT_ENDPOINT]["image_urls"] == [f"up:{tmp_path / 'f.jpg'}"]
    assert args[fal.MUSIC_ENDPOINT]["music_length_ms"] == 10000
    assert args[fal.SCRIBE_ENDPOINT]["audio_url"] == f"up:{tmp_path / 'a.wav'}"
    assert transcript == {
        "language": "en",
        "text": "Add a todo.",
        "words": [{"word": "Add", "start": 0.1, "end": 0.3}, {"word": "todo.", "start": 0.4, "end": 0.7}],
    }


class _Body:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


def test_stalled_download_raises_and_retry_recovers(monkeypatch, tmp_path):
    attempts = []

    def urlopen(url, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise socket.timeout("timed out")
        return _Body(b"mp3")

    monkeypatch.setattr(fal.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(fal.fal_client, "subscribe", lambda ep, arguments: RAW[ep])
    out = tmp_path / "v.mp3"
    with pytest.raises(socket.timeout):
        fal.tts("hi", "en", out)
    cache.retry(lambda: fal.tts("hi", "en", out), base_s=0)
    assert attempts == [60, 60] and out.read_bytes() == b"mp3"
