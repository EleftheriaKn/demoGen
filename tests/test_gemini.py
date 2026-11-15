"""Gemini client: fixed MIME tables, so a request is the same on every machine."""

from __future__ import annotations

import pytest

from backend.clients import gemini


def test_mime_types_are_fixed_and_unknown_suffix_raises():
    assert gemini.mime_type("a/rec.MOV", gemini.VIDEO_MIME) == "video/quicktime"
    assert gemini.mime_type("f.jpg", gemini.IMAGE_MIME) == gemini.mime_type("f.jpeg", gemini.IMAGE_MIME) == "image/jpeg"
    assert gemini.mime_type("f.webp", gemini.IMAGE_MIME) == "image/webp"
    with pytest.raises(ValueError, match="format isn.t supported"):
        gemini.mime_type("rec.avi", gemini.VIDEO_MIME)
    with pytest.raises(ValueError, match="f.gif"):
        gemini.mime_type("f.gif", gemini.IMAGE_MIME)
