"""Errors a board user can hit start with a plain sentence saying what to do. Failures are faked."""

from __future__ import annotations

import shutil

import pytest
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

from backend import cache, events
from backend.clients import fal, gemini
from backend.contracts import Candidates, RepoFiles, Summary
from backend.pipeline import judge, script, transcribe, understand
from backend.run import FIXTURE_RUN, load_run
from tests.test_transcribe import RUN, _setup


class _Out(BaseModel):
    x: int


@pytest.fixture
def live(monkeypatch):
    """Live Gemini with keys set and no recording or sleeping."""
    monkeypatch.setattr(gemini, "LIVE", True)
    monkeypatch.setattr(gemini, "RECORDINGS", None)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)


def test_no_usable_moments():
    with pytest.raises(ValueError) as e:
        understand.fix_moments([], 10.0, set())
    assert str(e.value).startswith("The recording doesn't show clear steps in the app. Record a 1–4 minute take")


def test_no_feature_has_evidence():
    summary = Summary.model_validate_json((FIXTURE_RUN / "summary.json").read_text(encoding="utf-8"))
    repo = RepoFiles.model_validate_json((FIXTURE_RUN / "repo_files.json").read_text(encoding="utf-8"))
    repo = repo.model_copy(update={"readme": "", "tree": []})
    with pytest.raises(ValueError) as e:
        understand.check_evidence(summary, repo)
    assert str(e.value).startswith("Couldn't match what the app does to its repo. Check the repo URL")


def test_unsupported_video_format(live, tmp_path):
    with pytest.raises(ValueError) as e:
        gemini.generate("t", _Out, video=tmp_path / "rec.avi")
    assert str(e.value) == "This video format isn't supported. Use .mp4, .mov or .webm."


def test_upload_failed(live, monkeypatch, tmp_path):
    class Files:
        def upload(self, **kw):
            raise ConnectionError("network down")

    monkeypatch.setattr(genai, "Client", lambda: type("C", (), {"files": Files()})())
    video = tmp_path / "rec.mp4"
    video.write_bytes(b"fake")
    with pytest.raises(RuntimeError) as e:
        gemini.generate("t", _Out, video=video)
    assert str(e.value).startswith("Uploading the recording to Gemini failed. Check the internet connection")
    assert "network down" in str(e.value)


@pytest.mark.parametrize("unset", ["GEMINI_API_KEY", "GEMINI_MODEL"])
def test_missing_gemini_setting(live, monkeypatch, unset):
    monkeypatch.delenv(unset)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(gemini, "_call", lambda *a: pytest.fail("called Gemini without its settings"))
    with pytest.raises(KeyError) as e:
        gemini.generate("t", _Out)
    assert str(e.value) == f"{unset} isn't set. Add it in the board's Settings, then run again."


def test_missing_fal_key_stops_transcribe_and_assets(monkeypatch, tmp_path):
    for k in ("FAL_KEY", "FAL_KEY_ID", "FAL_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)
    with pytest.raises(KeyError) as e:
        fal.tts("hi", "en", tmp_path / "v.mp3")
    assert str(e.value) == "FAL_KEY isn't set. Add it in the board's Settings, then run again."
    _setup(tmp_path, monkeypatch, has_audio=True)
    monkeypatch.setattr(fal, "transcribe", lambda p: fal._key())
    with pytest.raises(KeyError, match="FAL_KEY isn't set"):
        transcribe.run(tmp_path, RUN)


def test_script_with_no_footage(tmp_path, monkeypatch):
    for name in ("run.json", "repo_files.json", "summary.json", "moments.json", "candidates.json", "frame_scores.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    (tmp_path / "candidates.json").write_text(Candidates(segments=[]).model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(script, "draft_script", lambda *a: pytest.fail("drafted with no footage"))
    cfg = load_run(tmp_path)
    events.bind(tmp_path, cfg.run_id)
    with pytest.raises(ValueError) as e:
        script.run(tmp_path, cfg)
    assert str(e.value).startswith("Nothing in the recording could be used as footage. Record a longer take")


def test_gemini_rate_limit_after_retries(live, monkeypatch):
    def limited(*a):
        raise genai_errors.ClientError(429, {"error": {"code": 429, "message": "slow down", "status": "RESOURCE_EXHAUSTED"}})

    monkeypatch.setattr(gemini, "_call", limited)
    with pytest.raises(RuntimeError) as e:
        gemini.generate("t", _Out)
    assert str(e.value).startswith("Gemini is rate-limiting us. Wait a minute and run again; finished stages are kept. [")
    assert "slow down" in str(e.value)


def test_gemini_unreadable_answer_is_incomplete(live, monkeypatch):
    monkeypatch.setattr(gemini, "_call", lambda *a: "{}")
    with pytest.raises(RuntimeError) as e:
        gemini.generate("t", _Out)
    assert str(e.value).startswith(gemini.INCOMPLETE + " [ValidationError")


def test_fal_network_error_after_retries(monkeypatch, tmp_path):
    _setup(tmp_path, monkeypatch, has_audio=True)
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)

    def down(p):
        raise ConnectionError("no route")

    monkeypatch.setattr(fal, "transcribe", down)
    with pytest.raises(RuntimeError) as e:
        transcribe.run(tmp_path, RUN)
    assert str(e.value).startswith("Couldn't reach fal. Check the internet connection and run again;")
    assert "no route" in str(e.value)


def test_wrong_number_of_answers_says_run_again(monkeypatch, tmp_path):
    monkeypatch.setattr(gemini, "generate", lambda *a, **k: judge._Verdict(frames=[]))
    with pytest.raises(ValueError) as e:
        judge.judge_segment(tmp_path, ["f1.jpg"], ["Q?"], "App")
    assert str(e.value).startswith("The AI's answer was incomplete. Run again. [judge_frames:")
