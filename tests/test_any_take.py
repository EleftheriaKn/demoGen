"""B3: the render half works on any take: size, shape, rotation, frame rate, codec, HDR, no sound."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from backend import run as orchestrator
from backend.contracts import STAGES, RunConfig, Timeline
from backend.pipeline import timeline
from backend.pipeline.ff import inner_size, video_info

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

REAL = {"timeline", "captions", "render"}
LEN = 20  # the fixture script uses 5-9.5 s and 12-18 s of the take

SRC = f"testsrc2=s={{s}}:r={{r}}:d={LEN}"
TAKES = {  # name: (ffmpeg args after -y, displayed (w, h))
    "landscape_1366x768_25fps.mp4": (["-f", "lavfi", "-i", SRC.format(s="684x384", r=25),
                                      "-f", "lavfi", "-i", f"sine=d={LEN}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"],
                                     (684, 384)),
    "portrait_60fps_hevc_silent.mov": (["-f", "lavfi", "-i", SRC.format(s="296x640", r=60),
                                        "-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
                                        "-x265-params", "log-level=none"], (296, 640)),
    "odd_641x1387_444_24fps.mp4": (["-f", "lavfi", "-i", SRC.format(s="321x693", r=24),
                                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p"], (321, 693)),
    "vfr_dropped_frames.mp4": (["-f", "lavfi", "-i", SRC.format(s="640x360", r=30), "-vf",
                                "select='not(eq(mod(n\\,7)\\,3))*not(eq(mod(n\\,11)\\,5))'",
                                "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast"], (640, 360)),
    "4x3_800x600_15fps.mp4": (["-f", "lavfi", "-i", SRC.format(s="400x300", r=15), "-c:v", "libx264", "-preset", "ultrafast"],
                              (400, 300)),
    "hdr_hlg_10bit.mov": (["-f", "lavfi", "-i", SRC.format(s="640x360", r=30), "-c:v", "libx265",
                           "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-x265-params",
                           "log-level=none:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"],
                          (640, 360)),
}
ROTATED = "rotated_portrait_flag.mp4"  # coded 640x296, displayed 296x640


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def takes(tmp_path_factory):
    d = tmp_path_factory.mktemp("takes")
    for name, (args, _) in TAKES.items():
        ffmpeg(*args, "-t", str(LEN), str(d / name))
    ffmpeg("-f", "lavfi", "-i", SRC.format(s="640x296", r=30), "-c:v", "libx264", "-preset", "ultrafast", str(d / "coded.mp4"))
    ffmpeg("-display_rotation", "90", "-i", str(d / "coded.mp4"), "-c", "copy", str(d / ROTATED))
    return d


def test_inner_size_keeps_shape_and_is_even():
    assert inner_size(1920, 1080, 1280, 720) == (1280, 720)
    assert inner_size(592, 1280, 1280, 720) == (332, 720)
    w, h = inner_size(641, 1387, 1920, 1080)
    assert w % 2 == 0 and h % 2 == 0 and h == 1080


def test_video_info_reads_rotation_and_hdr(takes):
    assert video_info(takes / ROTATED)[:2] == (296, 640)
    assert video_info(takes / "hdr_hlg_10bit.mov").hdr
    assert not video_info(takes / "portrait_60fps_hevc_silent.mov").has_audio


@pytest.mark.parametrize("name", [*TAKES, ROTATED])
def test_render_half_on_any_take(name, takes, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RUNS", tmp_path)
    monkeypatch.setattr(timeline, "size_for", lambda mode: (640, 360))  # small output: same checks, faster
    run_dir = orchestrator.new_run_from_fixture()
    (run_dir / "recording.mp4").unlink()
    cfg = RunConfig.model_validate_json((run_dir / "run.json").read_text()).model_copy(update={"recording": name})
    (run_dir / "run.json").write_text(cfg.model_dump_json(indent=2))
    shutil.copy2(takes / name, run_dir / name)
    monkeypatch.setenv("DEMOCUT_FAKE", ",".join(s for s in STAGES if s not in REAL))

    final = orchestrator.run_pipeline(run_dir)

    tl = Timeline.model_validate_json((run_dir / "timeline.json").read_text())
    assert all(s.visual.path == name for s in tl.shots if s.visual.kind == "footage")
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,width,height,nb_read_frames,r_frame_rate", "-of", "json",
                          str(final)], capture_output=True, text=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (tl.width, tl.height, f"{tl.fps}/1")
    assert int(v["nb_read_frames"]) == round(tl.duration_s * tl.fps)
    assert any(s["codec_type"] == "audio" for s in streams)
