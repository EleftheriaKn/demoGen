"""B4: ingest. Offline: a local git repo stands in for GitHub."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.contracts import RepoFiles
from backend.pipeline import ingest
from backend.pipeline.ff import duration

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("ffmpeg")), reason="needs git + ffmpeg")
FIXTURE_TAKE = Path(__file__).resolve().parents[1] / "fixtures" / "run" / "recording.mp4"


@pytest.mark.parametrize("url,clean", [
    ("https://github.com/acme/app", "https://github.com/acme/app"),
    ("https://github.com/acme/app.git", "https://github.com/acme/app"),
    ("https://github.com/acme/my.app/", "https://github.com/acme/my.app"),
    ("https://github.com/acme/app/tree/main/src", "https://github.com/acme/app"),
    ("https://www.github.com/acme/app", "https://github.com/acme/app"),
])
def test_normalize_repo_url(url, clean):
    assert ingest.normalize_repo_url(url)[0] == clean


@pytest.mark.parametrize("url", [
    "http://github.com/acme/app", "git@github.com:acme/app.git", "file:///etc", "/home/me/repo",
    "https://gitlab.com/acme/app", "https://github.com/acme", "https://github.com/acme/..", "--upload-pack=x",
])
def test_normalize_repo_url_refuses(url):
    with pytest.raises(ValueError):
        ingest.normalize_repo_url(url)


def test_token_stays_out_of_argv_and_errors(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123")
    env = ingest.git_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret123" not in env["GIT_CONFIG_VALUE_0"]  # base64 in a header, via env only
    assert ingest._scrub("fatal: ghp_secret123 bad") == "fatal: *** bad"


def test_score_prefers_app_code():
    s = ingest.score
    assert s("package.json") > s("src/main.py") > s("src/utils/helpers.py") > 0
    assert s("package-lock.json") == 0 and s("dist/app.min.js") == 0 and s(".github/workflows/ci.yml") == 0
    assert s("tests/test_main.py") < s("src/main.py") and s("src/app.test.ts") < s("src/app.ts")
    assert s("vitest.config.ts") < s("src/app.ts")


@pytest.mark.parametrize("ref,expected", [
    ("docs/shot.png", "docs/shot.png"),
    ("./docs/shot.png", "docs/shot.png"),
    ("/docs/shot.png", "docs/shot.png"),
    ("../x.png", None),
    ("https://github.com/acme/app/blob/main/docs/a%20b.png", "docs/a b.png"),
    ("https://raw.githubusercontent.com/acme/app/main/docs/shot.png", "docs/shot.png"),
    ("https://example.com/shot.png", None),
    ("data:image/png;base64,xx", None),
])
def test_repo_path_for(ref, expected):
    assert ingest.repo_path_for(ref, "README.md", "acme", "app") == expected


def test_public_host_refuses_local():
    assert not ingest.public_host("localhost") and not ingest.public_host("127.0.0.1")
    assert not ingest.public_host("192.168.1.10") and not ingest.public_host("169.254.169.254")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def local_repo(tmp_path):
    src = tmp_path / "origin"
    (src / "src").mkdir(parents=True)
    (src / "docs").mkdir()
    (src / "tests").mkdir()
    (src / "node_modules" / "left-pad").mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1",
                    "-frames:v", "1", str(src / "docs" / "shot.png")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=0.3",
                    str(src / "docs" / "demo.gif")], check=True)
    (src / "README.md").write_text(
        "# App\n\n![ci](https://img.shields.io/badge/ci-passing-green)\n![shot](./docs/shot.png)\n"
        "<img src=\"docs/demo.gif\" width=300>\n![logo](docs/logo.svg)\n\n## Features\n- fast\n")
    (src / "package.json").write_text('{"name": "app", "scripts": {"start": "node src/main.js"}}\n')
    (src / "package-lock.json").write_text("{}\n" * 50)
    (src / "src" / "main.py").write_text("def main():\n    print('hi')\n")
    (src / "src" / "bundle.min.js").write_text("x" * 5000)
    (src / "tests" / "test_main.py").write_text("def test(): pass\n")
    (src / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (src / "docs" / "logo.svg").write_text("<svg/>")
    (src / "data.bin").write_bytes(b"\0\1\2" * 100)
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "init")
    dest = tmp_path / "clone" / "app"
    ingest.clone(f"file://{src}", dest)
    return dest


def test_collect_from_a_clone(local_repo, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    files = ingest.collect(local_repo, "https://github.com/acme/app", "acme", "app", run_dir)
    RepoFiles.model_validate(files.model_dump())

    assert files.readme.startswith("# App")
    assert "node_modules/left-pad/index.js" not in files.tree and "src/main.py" in files.tree
    keys = [k.path for k in files.key_files]
    assert keys[0] == "package.json" and "src/main.py" in keys
    assert not {"package-lock.json", "src/bundle.min.js", "data.bin", "README.md"} & set(keys)
    assert keys.index("src/main.py") < keys.index("tests/test_main.py") if "tests/test_main.py" in keys else True
    # badge and svg skipped; png copied; gif turned into a png of its first frame
    assert files.screenshots == ["screenshots/01_shot.png", "screenshots/02_demo.png"]
    assert all((run_dir / s).stat().st_size > 0 for s in files.screenshots)


def test_audio_with_and_without_sound(tmp_path):
    assert ingest.extract_audio(FIXTURE_TAKE, tmp_path / "a.wav")
    assert abs(duration(tmp_path / "a.wav") - 30) < 0.1
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", str(silent)], check=True)
    assert not ingest.extract_audio(silent, tmp_path / "s.wav")
    assert abs(duration(tmp_path / "s.wav") - 3) < 0.1


def test_missing_repo_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError):
        ingest.clone(f"file://{tmp_path}/nope", tmp_path / "x")
