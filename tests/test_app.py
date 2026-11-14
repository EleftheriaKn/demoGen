"""C3: orchestrator resume / --only, and the API streaming a fake run end to end."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend import run as orchestrator
from backend.contracts import STAGES, Event


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    return tmp_path


def events(run_dir):
    return [Event.model_validate_json(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]


def test_resume_skips_finished_stages(runs):
    d = orchestrator.new_run_from_fixture()
    orchestrator.run_pipeline(d)
    assert orchestrator.finished_stages(d) == set(STAGES)
    (d / "timeline.json").unlink()  # "one stage is wrong": delete its output
    orchestrator.run_pipeline(d)
    second = events(d)[len(STAGES) * 2 + 2 :]
    ran = {e.stage for e in second if e.msg.endswith("started (fake)")}
    assert ran == {"timeline"}
    assert (d / "timeline.json").exists()


def test_only_runs_one_stage(runs):
    d = orchestrator.new_run_from_fixture()
    orchestrator.main([str(d), "--only", "understand"])
    assert (d / "moments.json").exists() and not (d / "final.mp4").exists()
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(d, only="nope")


def test_api_post_and_stream_fixture_run(runs):
    client = TestClient(api.app)
    r = client.post("/runs", data={"repo_url": "https://github.com/example/pantrypal"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]

    with client.stream("GET", f"/runs/{run_id}/events") as s:
        body = "".join(s.iter_text())
    data = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
    evs = [Event.model_validate(x) for x in data[:-1]]
    assert data[-1] == {"status": "done"}
    assert evs[0].msg == "Run started" and evs[-1].msg == "Run done"

    info = client.get(f"/runs/{run_id}").json()
    assert info["status"] == "done" and info["has_final"]
    assert client.get(f"/runs/{run_id}/files/final.mp4").status_code == 200
    assert client.get(f"/runs/{run_id}/files/../../etc/passwd").status_code == 404
    assert client.get("/runs").json()[0]["run_id"] == run_id


def test_board_is_served():
    r = TestClient(api.app).get("/")
    assert r.status_code == 200 and "EventSource" in r.text


def test_api_upload_recording_and_validation(runs):
    client = TestClient(api.app)
    rec = (orchestrator.FIXTURE_RUN / "recording.mp4").read_bytes()
    r = client.post(
        "/runs",
        data={"repo_url": "https://github.com/x/y", "criteria": "craft", "mode": "full"},
        files={"recording": ("take.MOV", rec, "video/quicktime")},
    )
    assert r.status_code == 201
    cfg = json.loads((runs / r.json()["run_id"] / "run.json").read_text())
    assert cfg["recording"] == "recording.mov" and cfg["brief"]["criteria"] == ["craft"]
    assert client.post("/runs", data={"repo_url": "x", "mode": "turbo"}).status_code == 422
    assert client.get("/runs/nope").status_code == 404
    # English and Greek only
    assert client.post("/runs", data={"repo_url": "x", "language": "fr"}).status_code == 422
    assert client.post("/runs", data={"repo_url": "x", "language": "el", "second_language": "el"}).status_code == 422
