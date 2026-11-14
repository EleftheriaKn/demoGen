"""C12: resumable uploads in parts, and the 7:00 / 1 GiB limits enforced by the server."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend import run as orchestrator

needs_ffprobe = pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffmpeg/ffprobe")
TAKE = (orchestrator.FIXTURE_RUN / "recording.mp4").read_bytes()  # 30 s real video
REPO = "https://github.com/example/pantrypal"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    return TestClient(api.app)


def upload(client, data: bytes, name="take.mov", chunk=100_000) -> str:
    r = client.post("/uploads", json={"filename": name, "size": len(data)})
    assert r.status_code == 201, r.text
    uid = r.json()["upload_id"]
    for off in range(0, len(data), chunk):
        r = client.put(f"/uploads/{uid}?offset={off}", content=data[off : off + chunk])
        assert r.status_code == 200, r.text
    assert client.get(f"/uploads/{uid}").json()["complete"]
    return uid


@needs_ffprobe
def test_parts_then_run(client, tmp_path):
    uid = upload(client, TAKE)
    r = client.post("/runs", data={"repo_url": REPO, "upload_id": uid})
    assert r.status_code == 201, r.text
    run_dir = tmp_path / r.json()["run_id"]
    assert (run_dir / "recording.mov").read_bytes() == TAKE           # moved in whole
    assert not list((tmp_path / "_uploads").iterdir())                # upload cleaned up
    assert client.get(f"/uploads/{uid}").status_code == 404


def test_resume_after_dropped_part(client):
    data = TAKE[:300_000]
    uid = client.post("/uploads", json={"filename": "t.mp4", "size": len(data)}).json()["upload_id"]
    client.put(f"/uploads/{uid}?offset=0", content=data[:100_000])
    # the phone lost the answer and resends part 1 at offset 0: server says what it has
    r = client.put(f"/uploads/{uid}?offset=0", content=data[:100_000])
    assert r.status_code == 409 and r.json()["detail"]["received"] == 100_000
    assert client.get(f"/uploads/{uid}").json()["received"] == 100_000
    client.put(f"/uploads/{uid}?offset=100000", content=data[100_000:])
    assert client.get(f"/uploads/{uid}").json()["complete"]


def test_unfinished_upload_cannot_start_a_run(client):
    uid = client.post("/uploads", json={"filename": "t.mp4", "size": 1000}).json()["upload_id"]
    client.put(f"/uploads/{uid}?offset=0", content=b"x" * 10)
    r = client.post("/runs", data={"repo_url": REPO, "upload_id": uid})
    assert r.status_code == 409 and r.json()["detail"]["received"] == 10


def test_size_limits(client):
    assert client.post("/uploads", json={"filename": "big.mov", "size": api.MAX_TAKE_BYTES + 1}).status_code == 413
    assert client.post("/uploads", json={"filename": "notes.txt", "size": 10}).status_code == 422
    uid = client.post("/uploads", json={"filename": "t.mp4", "size": 10}).json()["upload_id"]
    assert client.put(f"/uploads/{uid}?offset=0", content=b"x" * 11).status_code == 413
    assert client.get(f"/uploads/{uid}").json()["received"] == 0      # the oversized part was dropped
    assert client.get("/uploads/../../etc").status_code == 404


@needs_ffprobe
def test_too_long_or_not_a_video(client, tmp_path):
    long = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=1", "-t", "425",
                    "-c:v", "libx264", "-preset", "ultrafast", str(long)], check=True)
    uid = upload(client, long.read_bytes(), name="long.mp4")
    r = client.post("/runs", data={"repo_url": REPO, "upload_id": uid})
    assert r.status_code == 422 and "7:00" in r.json()["detail"]
    assert client.get(f"/uploads/{uid}").status_code == 404           # rejected take not kept

    uid = upload(client, b"definitely not a video" * 100, name="fake.mp4")
    assert client.post("/runs", data={"repo_url": REPO, "upload_id": uid}).status_code == 422
    # the one-shot form upload gets the same checks
    r = client.post("/runs", data={"repo_url": REPO}, files={"recording": ("x.mp4", b"nope", "video/mp4")})
    assert r.status_code == 422
