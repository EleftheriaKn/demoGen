"""C11: GitHub login with a fake GitHub. The token stays server-side and reaches only the run's env."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend import github_auth
from backend import run as orchestrator

TOKEN = "gho_fake_token_123"


def fake_github(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/login/oauth/access_token":
        body = parse_qs(request.content.decode())
        ok = body.get("code") == ["good-code"] and body.get("client_secret") == ["s3cret"]
        return httpx.Response(200, json={"access_token": TOKEN} if ok else {"error": "bad_verification_code"})
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return httpx.Response(401, json={"message": "Bad credentials"})
    if request.url.path == "/user":
        return httpx.Response(200, json={"login": "octo", "avatar_url": "https://avatars.example/octo.png"})
    if request.url.path == "/user/repos":
        assert request.url.params["sort"] == "updated"
        return httpx.Response(200, json=[
            {"full_name": "octo/app", "html_url": "https://github.com/octo/app", "private": True,
             "description": "The app", "pushed_at": "2026-11-15T01:00:00Z"},
            {"full_name": "octo/site", "html_url": "https://github.com/octo/site", "private": False,
             "description": None, "pushed_at": "2026-11-01T01:00:00Z"},
        ])
    return httpx.Response(404)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setenv("DEMOCUT_FAKE", "all")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(github_auth, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_github)))
    github_auth._sessions.clear()
    return TestClient(api.app)


def login(client, code="good-code"):
    r = client.get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["cid"] and q["redirect_uri"][0].endswith("/auth/github/callback")
    return client.get(f"/auth/github/callback?code={code}&state={q['state'][0]}", follow_redirects=False)


def test_login_lists_repos(client):
    assert client.get("/auth/me").json() == {"enabled": True, "logged_in": False, "login": None, "avatar_url": None}
    assert client.get("/github/repos").status_code == 401
    r = login(client)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert client.get("/auth/me").json()["login"] == "octo"
    repos = client.get("/github/repos").json()
    assert [x["full_name"] for x in repos] == ["octo/app", "octo/site"] and repos[0]["private"]
    assert TOKEN not in str(client.cookies) and TOKEN not in client.get("/auth/me").text


def test_token_only_in_run_process_env(client, monkeypatch, tmp_path):
    seen = {}
    real = api.start_pipeline
    monkeypatch.setattr(api, "start_pipeline", lambda d, tok=None: seen.update(tok=tok) or real(d, tok))
    login(client)
    rid = client.post("/runs", data={"repo_url": "https://github.com/octo/app"}).json()["run_id"]
    assert seen["tok"] == TOKEN
    with client.stream("GET", f"/runs/{rid}/events") as s:
        "".join(s.iter_text())
    for f in (tmp_path / rid).rglob("*"):  # run.json, events, logs: no token anywhere
        if f.is_file() and f.suffix in (".json", ".jsonl", ".log", ".txt"):
            assert TOKEN not in f.read_text(errors="ignore"), f
    # logged out: no token passed
    client.post("/auth/logout")
    client.post("/runs", data={"repo_url": "https://github.com/octo/site"})
    assert seen["tok"] is None


def test_bad_state_or_code(client):
    client.get("/auth/github/login", follow_redirects=False)
    assert client.get("/auth/github/callback?code=good-code&state=forged", follow_redirects=False).status_code == 400
    r = login(client, code="wrong")
    assert r.headers["location"] == "/?login_error=token" and not client.get("/auth/me").json()["logged_in"]
    r = client.get("/auth/github/callback?error=access_denied", follow_redirects=False)
    assert r.headers["location"] == "/?login_error=access_denied"


def test_disabled_without_keys(client, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID")
    assert client.get("/auth/me").json()["enabled"] is False
    assert client.get("/auth/github/login", follow_redirects=False).status_code == 404
