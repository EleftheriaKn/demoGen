"""B's stages on other machines: UTF-8 everywhere (Greek-locale Windows), filter paths that survive 'C:'."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.pipeline.ff import FONTS, filter_path

B_FILES = ["ff", "ingest", "candidates", "timeline", "captions", "render"]
SRC = Path(__file__).resolve().parents[1] / "backend" / "pipeline"


@pytest.mark.parametrize("name", B_FILES)
def test_every_text_io_is_utf8(name):
    code = (SRC / f"{name}.py").read_text(encoding="utf-8")
    assert not re.search(r"\.read_text\(\)", code), "read_text() without encoding"
    for call in re.findall(r"\.write_text\((?:[^()]|\([^()]*\))*\)", code):
        assert "encoding=" in call, call
    for call in re.findall(r"text=True[^)]*\)", code):
        assert 'encoding="utf-8"' in call, call


def test_filter_path_escapes_windows_drive():
    assert filter_path(r"C:\Users\Νίκος\demoGen\fonts") == "'C\\:/Users/Νίκος/demoGen/fonts'"
    assert filter_path("/home/b/demoGen/fonts") == "'/home/b/demoGen/fonts'"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_captions_render_with_a_colon_in_the_font_path(tmp_path):
    fonts = tmp_path / "C:odd fonts"
    shutil.copytree(FONTS, fonts)
    (tmp_path / "c.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nΚαλησπέρα ✔\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=s=320x180:d=1",
                    "-vf", f"subtitles=c.srt:fontsdir={filter_path(fonts)}", "-frames:v", "1", "out.png"],
                   cwd=tmp_path, check=True)
    assert (tmp_path / "out.png").stat().st_size > 0
