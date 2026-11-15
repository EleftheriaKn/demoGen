"""Gemini record/replay for every test.

By default Gemini calls replay from tests/recordings/ and a missing recording fails the test.
``pytest --live`` calls Gemini for real (needs GEMINI_API_KEY and GEMINI_MODEL) and rewrites
the recordings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.clients import gemini

RECORDINGS = Path(__file__).resolve().parent / "recordings"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="call Gemini for real and refresh tests/recordings/")


@pytest.fixture(autouse=True)
def gemini_replay(request, monkeypatch):
    monkeypatch.setattr(gemini, "RECORDINGS", RECORDINGS)
    monkeypatch.setattr(gemini, "LIVE", request.config.getoption("--live"))


@pytest.fixture(autouse=True)
def no_autorecord(monkeypatch):
    """B26 visits live sites: never from tests (no-take runs stay offline and fast)."""
    monkeypatch.setenv("DEMOCUT_AUTORECORD", "0")
