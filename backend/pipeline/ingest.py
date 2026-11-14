"""Stage `ingest` (owner: B).

Clone the repo, pick what the brains should read, pull README screenshots, extract the take's audio.

Reads run.json and the recording. Writes repo_files.json, audio.wav (16 kHz mono; silence when the
take has no sound, so transcribe always has a file), screenshots/.

Private repos: set GITHUB_TOKEN. It goes to git through the environment, never on a command line,
in run files, events or error messages.

Run alone: python -m backend.pipeline.ingest runs/<run_id>
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path, PurePosixPath

from backend import events
from backend.contracts import KeyFile, RepoFiles, RunConfig
from backend.pipeline.ff import ff, video_info

CLONE_TIMEOUT_S = 180
MAX_TREE = 5000              # paths listed in repo_files.json (vendored/build dirs are skipped first)
MAX_KEY_FILES = 20           # contract limit
MAX_PER_DIR = 5              # keep key files spread over the repo
MAX_SAME_NAME = 2            # monorepos: not ten package.json files
MAX_FILE_CHARS = 8000        # per key file, so 20 files stay well inside one Gemini call
MAX_README_CHARS = 100_000
MAX_SCREENSHOTS = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 15

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "vendor", "venv", ".venv", "env", "__pycache__", ".next",
    ".nuxt", "target", "coverage", ".idea", ".vscode", "Pods", ".gradle", "bin", "obj", "site-packages",
    "third_party", "third-party", "external", ".dart_tool", ".expo", ".svelte-kit", ".turbo", ".cache",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "gemfile.lock",
    "composer.lock", "go.sum", "pipfile.lock", "bun.lockb", "uv.lock", "podfile.lock", "pubspec.lock",
}
MANIFESTS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "gemfile", "composer.json", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "compose.yaml", "app.json", "pubspec.yaml", "manifest.json", "schema.prisma",
    "openapi.yaml", "openapi.json", ".env.example", "vercel.json", "netlify.toml", "fly.toml",
}
SOURCE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb", ".php",
    ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".dart", ".vue", ".svelte", ".scala", ".ex", ".exs", ".sql",
    ".sh", ".html", ".css", ".scss", ".graphql", ".proto", ".lua", ".zig", ".m", ".mm",
}
ENTRY_STEMS = {
    "main", "app", "index", "server", "api", "cli", "routes", "router", "views", "models", "handler",
    "__main__", "manage", "page", "layout", "worker", "bot", "agent", "pipeline", "core", "service",
}
CODE_DIRS = {"src", "app", "backend", "api", "server", "lib", "core", "frontend", "web", "mobile", "cmd"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
BADGE = re.compile(r"shields\.io|badgen|badge|travis-ci|codecov|circleci|coveralls|/workflows/|"
                   r"buymeacoffee|ko-fi|star-history|contrib\.rocks", re.I)
MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
HTML_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I)
GITHUB = re.compile(r"^https://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/.*)?$")


# --------------------------------------------------------------------------- repo URL + git


def normalize_repo_url(url: str) -> tuple[str, str, str]:
    """'https://github.com/o/r(.git|/tree/...)' -> ('https://github.com/o/r', 'o', 'r'). Anything else is refused,
    so a run can't point git at a local path, ssh or another host."""
    m = GITHUB.match(url.strip().rstrip("/"))
    if not m or m.group(2) in {".", ".."}:
        raise ValueError(f"not a GitHub repo URL: {url!r} (expected https://github.com/<owner>/<repo>)")
    owner, name = m.group(1), m.group(2)
    return f"https://github.com/{owner}/{name}", owner, name


def git_env() -> dict[str, str]:
    """Never prompt for a password; pass GITHUB_TOKEN as an HTTP header through env-only git config."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def _scrub(text: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return text.replace(token, "***") if token else text


def git(repo: Path | None, *args: str, stdin: bytes | None = None, timeout: int = 120) -> bytes:
    cmd = ["git", *(["-C", str(repo)] if repo else []), *args]
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, env=git_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} timed out after {timeout} s") from None
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {_scrub(p.stderr.decode(errors='replace')).strip()[-800:]}")
    return p.stdout


def clone(url: str, dest: Path) -> None:
    """Shallow, blob-filtered clone without checkout: fast even for big repos; files are checked out on demand."""
    try:
        git(None, "clone", "--depth", "1", "--single-branch", "--filter=blob:limit=2m", "--no-checkout",
            "--quiet", "--", url, str(dest), timeout=CLONE_TIMEOUT_S)
    except RuntimeError as e:
        msg = str(e)
        if re.search(r"not found|could not read Username|Authentication failed|terminal prompts disabled", msg, re.I):
            hint = "" if os.environ.get("GITHUB_TOKEN") else " If it is private, set GITHUB_TOKEN."
            raise RuntimeError(f"could not clone {url}: repo not found or no access.{hint}") from None
        raise


def list_files(repo: Path) -> list[str]:
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def checkout(repo: Path, paths: list[str]) -> None:
    if paths:
        git(repo, "checkout", "HEAD", "--pathspec-from-file=-", "--pathspec-file-nul",
            stdin="\0".join(paths).encode(), timeout=CLONE_TIMEOUT_S)


# --------------------------------------------------------------------------- what to read


def skipped(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(p in SKIP_DIRS for p in parts[:-1])


def find_readme(tree: list[str]) -> str | None:
    rank = {"readme.md": 0, "readme.markdown": 1, "readme.rst": 2, "readme.txt": 3, "readme": 4}
    for where in ("", "docs/", ".github/"):
        hits = [p for p in tree if p.lower().removeprefix(where) in rank and p.lower().startswith(where)
                and "/" not in p[len(where):]]
        if hits:
            return min(hits, key=lambda p: rank[p.lower().removeprefix(where)])
    return None


def score(path: str) -> float:
    """How useful a file is for understanding the app. <= 0 means never pick it."""
    p = PurePosixPath(path)
    name, stem, ext, low = p.name.lower(), p.stem.lower(), p.suffix.lower(), path.lower()
    hidden_dir = any(x.startswith(".") for x in p.parts[:-1])  # .github, .devcontainer, ...
    if name in LOCKFILES or ".min." in name or name.endswith((".map", ".d.ts")) or hidden_dir:
        return 0
    if name in MANIFESTS:
        s = 8.0
    elif ext in SOURCE_EXT:
        s = 3.0 + (3.0 if stem in ENTRY_STEMS else 0.0)
    elif ext in {".md", ".mdx"} and not name.startswith(("changelog", "license", "contributing", "code_of")):
        s = 1.5
    else:
        return 0
    depth = len(p.parts) - 1
    s -= 0.5 * max(0, depth - 1)
    if any(part.lower() in CODE_DIRS for part in p.parts[:-1]):
        s += 1.0
    if re.search(r"(^|/)(tests?|__tests__|spec|e2e|fixtures?|mocks?|examples?|samples?|migrations)(/|$)", low) \
            or re.search(r"[._-](test|spec)\.", name) or name.startswith("test_") \
            or re.search(r"e2e|playwright|cypress|storybook|benchmark", low):
        s -= 4.0
    if re.search(r"(eslint|prettier|vitest|jest|babel|karma|stylelint|tsconfig|webpack|rollup)", name) \
            or re.search(r"\.setup\.|\.config\.(js|cjs|mjs|ts)$", name):
        s -= 3.0  # tooling config: says little about what the app does
    return s


def read_text(path: Path) -> str | None:
    """File content as text, or None for binary / minified / generated-looking files."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip() or b"\0" in raw[:8000]:
        return None
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") + 1
    if len(text) > 2000 and len(text) / lines > 300:  # one giant line: minified or generated
        return None
    return text


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n... [truncated, {len(text)} chars in total]"


def pick_key_files(repo: Path, tree: list[str], readme: str | None) -> list[KeyFile]:
    ranked = sorted((p for p in tree if p != readme and score(p) > 0), key=lambda p: (-score(p), len(p)))[:120]
    checkout(repo, ranked)
    picked, per_dir, per_name = [], Counter(), Counter()
    for path in ranked:
        parent, fname = str(PurePosixPath(path).parent), PurePosixPath(path).name.lower()
        if per_dir[parent] >= MAX_PER_DIR or per_name[fname] >= MAX_SAME_NAME:
            continue
        text = read_text(repo / path)
        if text is None:
            continue
        picked.append(KeyFile(path=path, content=truncate(text, MAX_FILE_CHARS)))
        per_dir[parent] += 1
        per_name[fname] += 1
        if len(picked) == MAX_KEY_FILES:
            break
    return picked


# --------------------------------------------------------------------------- screenshots


def image_refs(readme_text: str) -> list[str]:
    refs, seen = [], set()
    for m in [*MD_IMG.finditer(readme_text), *HTML_IMG.finditer(readme_text)]:
        ref = m.group(1).strip()
        if ref not in seen:
            seen.add(ref)
            refs.append((m.start(), ref))
    return [r for _, r in sorted(refs)]


def repo_path_for(ref: str, readme: str, owner: str, name: str) -> str | None:
    """Map a README image reference to a path inside this repo, or None when it lives elsewhere."""
    u = urllib.parse.urlparse(ref)
    if u.scheme in ("http", "https"):
        host, parts = u.netloc.lower(), [urllib.parse.unquote(x) for x in u.path.split("/") if x]
        same = len(parts) >= 2 and parts[0].lower() == owner.lower() and parts[1].lower() == name.lower()
        if host in ("github.com", "www.github.com") and same and len(parts) > 4 and parts[2] in ("blob", "raw"):
            return "/".join(parts[4:])
        if host == "raw.githubusercontent.com" and same and len(parts) > 3:
            return "/".join(parts[3:])
        return None
    if u.scheme:
        return None
    path = urllib.parse.unquote(u.path)
    base = PurePosixPath("") if path.startswith("/") else PurePosixPath(readme).parent
    parts = []
    for part in (base / path.lstrip("/")).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts) or None


def public_host(host: str) -> bool:
    """Refuse downloads from localhost / private networks (a README must not reach into our machine)."""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if u.scheme != "https" or not public_host(u.hostname or ""):
            raise urllib.error.URLError(f"refused redirect to {u.scheme}://{u.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url: str) -> tuple[bytes, str] | None:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not public_host(u.hostname or ""):
        return None
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "democut-ingest"})
    try:
        with opener.open(req, timeout=DOWNLOAD_TIMEOUT_S) as r:
            ctype = r.headers.get_content_type()
            if not ctype.startswith("image/") or "svg" in ctype:
                return None
            data = r.read(MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(ctype)
    return (data, ext) if ext else None


def save_image(data: bytes, ext: str, out_dir: Path, i: int, stem: str) -> str | None:
    """Write one screenshot; GIFs become a PNG of their first frame (Gemini doesn't read GIFs)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:40].strip("_") or "image"
    tmp = out_dir / f"{i:02d}_{safe}{ext}"
    tmp.write_bytes(data)
    if ext != ".gif":
        return f"screenshots/{tmp.name}"
    png = tmp.with_suffix(".png")
    try:
        ff("-i", tmp, "-frames:v", "1", png)
    except RuntimeError:
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return f"screenshots/{png.name}"


def fetch_screenshots(repo: Path, tree: list[str], readme: str | None, readme_text: str, owner: str, name: str,
                      run_dir: Path) -> list[str]:
    out_dir = run_dir / "screenshots"
    out_dir.mkdir(exist_ok=True)
    in_repo = set(tree)
    refs = image_refs(readme_text) if readme else []
    if not refs:  # no README images: look for screenshot-ish files in the repo
        refs = [p for p in tree if PurePosixPath(p).suffix.lower() in IMAGE_EXT and not skipped(p)
                and re.search(r"screen|demo|preview|shot", p, re.I)][:4]
        readme = ""
    saved: list[str] = []
    for ref in refs:
        if len(saved) == MAX_SCREENSHOTS:
            break
        if BADGE.search(ref):
            continue
        local = repo_path_for(ref, readme or "", owner, name)
        if local is not None:
            ext = PurePosixPath(local).suffix.lower()
            if local not in in_repo or ext not in IMAGE_EXT:
                continue
            checkout(repo, [local])
            data = (repo / local).read_bytes()
            if len(data) > MAX_IMAGE_BYTES:
                continue
            got = save_image(data, ".jpg" if ext == ".jpeg" else ext, out_dir, len(saved) + 1, PurePosixPath(local).stem)
        else:
            dl = download_image(ref)
            got = save_image(dl[0], dl[1], out_dir, len(saved) + 1,
                             PurePosixPath(urllib.parse.urlparse(ref).path).stem) if dl else None
        if got:
            saved.append(got)
    return saved


# --------------------------------------------------------------------------- audio


def extract_audio(recording: Path, out: Path) -> bool:
    """16 kHz mono WAV of the take's sound. No sound: write silence of the same length. Returns has_sound."""
    info = video_info(recording)
    if info.has_audio:
        ff("-i", recording, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out)
        return True
    ff("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{max(info.duration, 0.1):.3f}",
       "-c:a", "pcm_s16le", out)
    return False


# --------------------------------------------------------------------------- stage


def collect(repo: Path, repo_url: str, owner: str, name: str, run_dir: Path) -> RepoFiles:
    """Everything after the clone: tree, README, key files, screenshots."""
    tree_all = list_files(repo)
    readme = find_readme(tree_all)
    readme_text = ""
    if readme:
        checkout(repo, [readme])
        readme_text = truncate(read_text(repo / readme) or "", MAX_README_CHARS)
    key_files = pick_key_files(repo, [p for p in tree_all if not skipped(p)], readme)
    screenshots = fetch_screenshots(repo, tree_all, readme, readme_text, owner, name, run_dir)
    must = {k.path for k in key_files} | ({readme} if readme else set())
    tree = [p for p in tree_all if not skipped(p)]
    if len(tree) > MAX_TREE:
        tree = sorted(must | set(sorted(tree, key=lambda p: (p.count("/"), p))[:MAX_TREE - len(must)]))
    return RepoFiles(repo_url=repo_url, readme=readme_text, tree=tree, key_files=key_files, screenshots=screenshots)


def run(run_dir: Path, run: RunConfig) -> None:
    d = Path(run_dir)
    recording = d / run.recording
    if not recording.exists():
        raise RuntimeError(f"ingest: recording {run.recording} is missing from the run folder")
    url, owner, name = normalize_repo_url(run.repo_url)

    with tempfile.TemporaryDirectory(prefix="democut-repo-") as tmp:
        repo = Path(tmp) / name
        clone(url, repo)
        sha = git(repo, "rev-parse", "--short", "HEAD").decode().strip()
        files = collect(repo, run.repo_url, owner, name, d)
    (d / "repo_files.json").write_text(files.model_dump_json(indent=2))
    events.emit("ingest", f"{owner}/{name}@{sha}: {len(files.tree)} files, {len(files.key_files)} key files, "
                          f"{len(files.screenshots)} screenshots",
                data={"sha": sha, "key_files": [k.path for k in files.key_files]})

    has_sound = extract_audio(recording, d / "audio.wav")
    events.emit("ingest", "audio.wav extracted" if has_sound else "the take has no sound: audio.wav is silence",
                data={"has_audio": has_sound})


if __name__ == "__main__":
    from backend.run import stage_main

    stage_main("ingest")
