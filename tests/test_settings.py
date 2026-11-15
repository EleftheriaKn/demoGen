"""Keys from the board: laptop only, secrets write-only, saved to .env and applied without a restart."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend import settings


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# my comment\nGEMINI_MODEL=old-model\nOTHER=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV_PATH", p)
    for k in settings.KEYS:
        monkeypatch.delenv(k, raising=False)
    return p


@pytest.fixture
def laptop(env_file):
    return TestClient(api.app, client=("127.0.0.1", 50000))


def test_save_secret_never_returned(laptop, env_file):
    r = laptop.post("/settings", json={"values": {"FAL_KEY": "fal-secret-abcd1234", "GEMINI_MODEL": "new-model"}})
    assert r.status_code == 200 and r.json()["saved"] == ["FAL_KEY", "GEMINI_MODEL"]
    text = env_file.read_text(encoding="utf-8")
    assert "FAL_KEY=fal-secret-abcd1234" in text and "GEMINI_MODEL=new-model" in text
    assert "# my comment" in text and "OTHER=keep-me" in text          # other lines untouched
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert os.environ["FAL_KEY"] == "fal-secret-abcd1234"              # applied without a restart
    page = laptop.get("/settings")
    assert "fal-secret-abcd1234" not in page.text                      # write-only
    fal = next(f for f in page.json()["fields"] if f["key"] == "FAL_KEY")
    assert fal["set"] and fal["value"] is None and fal["hint"] == "…1234"
    model = next(f for f in page.json()["fields"] if f["key"] == "GEMINI_MODEL")
    assert model["value"] == "new-model"


def test_empty_keeps_and_clear_removes(laptop, env_file):
    laptop.post("/settings", json={"values": {"FAL_KEY": "fal-secret-abcd1234"}})
    laptop.post("/settings", json={"values": {"FAL_KEY": ""}})          # empty field = keep
    assert os.environ["FAL_KEY"] == "fal-secret-abcd1234"
    r = laptop.post("/settings", json={"clear": ["FAL_KEY"]})
    assert r.json()["cleared"] == ["FAL_KEY"] and "FAL_KEY" not in os.environ
    assert "FAL_KEY=\n" in env_file.read_text(encoding="utf-8")


def test_github_login_turns_on_at_once(laptop):
    assert laptop.get("/auth/me").json()["enabled"] is False
    laptop.post("/settings", json={"values": {"GITHUB_CLIENT_ID": "Iv1.abc", "GITHUB_CLIENT_SECRET": "s3cret-value-xyz9"}})
    assert laptop.get("/auth/me").json()["enabled"] is True


@pytest.mark.parametrize("client,headers", [
    (("172.20.1.42", 50000), {}),                                          # a phone / another laptop on the Wi-Fi
    (("127.0.0.1", 50000), {"x-forwarded-for": "203.0.113.9"}),          # through a proxy
    (("127.0.0.1", 50000), {"cf-connecting-ip": "203.0.113.9"}),         # through a cloudflared tunnel
    (("127.0.0.1", 50000), {"origin": "http://evil.example"}),           # another website in the laptop's browser
])
def test_refused_off_laptop(env_file, client, headers):
    c = TestClient(api.app, client=client)
    assert c.get("/settings", headers=headers).status_code == 403
    assert c.post("/settings", json={"values": {"FAL_KEY": "stolen-key-0000"}}, headers=headers).status_code == 403
    assert "stolen" not in env_file.read_text(encoding="utf-8")


def test_bad_input(laptop, env_file):
    assert laptop.post("/settings", json={"values": {"PATH": "/evil"}}).status_code == 422          # only known keys
    assert laptop.post("/settings", json={"values": {"FAL_KEY": "a\nGEMINI_MODEL=x"}}).status_code == 422  # no line injection
    assert laptop.post("/settings", json={"values": "nope"}).status_code == 422
    assert laptop.post("/settings", json={"values": {"GITHUB_OAUTH_SCOPES": "read:user repo"}}).status_code == 200


def test_board_has_settings_panel():
    page = TestClient(api.app).get("/").text
    assert 'id="settings-btn"' in page and 'id="settings-form"' in page
