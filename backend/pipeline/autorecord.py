"""B26: the app records itself.

When a run has no recording, ingest calls ``record_take``: it looks for a live address of the app (the
repo's GitHub homepage, then a link in the brief, then in the README), opens it in headless Chromium and
lets Gemini use it one action at a time (screenshot + numbered elements -> click / type / press / scroll /
done), with a drawn cursor and click ripple. Frames come from Chrome's screencast (JPEG, timestamped), so
the time spent waiting for Gemini is cut out: only each action and the second after it are kept. The result
is an H.264 MP4 at ``run.recording`` and the run goes on as if the team had uploaded it.

Nothing from the repo is run: only the public site is visited, and only https hosts on the public internet.
Any failure (no address, no browser, no key, site down) returns None and the run makes its video from the
repo alone (B17 / A20). Set DEMOCUT_AUTORECORD=0 to turn it off; DEMOCUT_CHROMIUM names a browser binary.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from backend import events
from backend.clients import gemini
from backend.pipeline.ff import ff

VIEWPORT = (1280, 720)
SCALE = 1.5               # device pixels: frames are 1920x1080
MAX_STEPS = 12
MAX_WALL_S = 240          # stop asking after this long, whatever happens
MAX_TAKE_S = 90
KEEP_BEFORE_S, KEEP_AFTER_S = 0.3, 1.6   # kept around each action (its result settling)
OPEN_KEEP_S = 2.5                       # the page as it opens
END_HOLD_S = 2.0                        # the payoff holds at the end
TYPE_DELAY_MS = 70
FPS = 30

# hosts that are never the app itself
SKIP_HOSTS = ("github.com", "githubusercontent.com", "shields.io", "npmjs.com", "npmjs.org", "pypi.org",
              "twitter.com", "x.com", "linkedin.com", "youtube.com", "youtu.be", "discord.gg", "discord.com",
              "medium.com", "wikipedia.org", "creativecommons.org", "opensource.org", "codecov.io",
              "travis-ci.org", "travis-ci.com", "circleci.com", "gitter.im", "stackoverflow.com", "google.com",
              "docs.", "readthedocs", "figma.com", "vercel.com", "netlify.com", "heroku.com")
LIVE_HINT = re.compile(r"\b(live|demo|try|app|deployed|website|play|launch|open)\b", re.I)
APP_HOSTS = (".vercel.app", ".netlify.app", ".github.io", ".pages.dev", ".onrender.com", ".herokuapp.com",
             ".fly.dev", ".streamlit.app", ".railway.app", ".surge.sh", ".web.app", ".firebaseapp.com",
             "huggingface.co/spaces", ".replit.app", ".glitch.me", ".up.railway.app", ".azurewebsites.net")
URL_RE = re.compile(r"https://[^\s)\]>\"'`]+")

CURSOR_JS = r"""
(() => {
  const add = () => {
    if (document.getElementById('__dc_cursor')) return;
    const c = document.createElement('div');
    c.id = '__dc_cursor';
    c.style.cssText = 'position:fixed;left:0;top:0;width:24px;height:24px;z-index:2147483647;pointer-events:none;' +
      'transition:transform .05s linear;background:url("data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 22 22%22><path d=%22M2 2 L2 18 L7 13 L10 20 L13 19 L10 12 L17 12 Z%22 fill=%22black%22 stroke=%22white%22 stroke-width=%221.5%22/></svg>") no-repeat;';
    const pos = JSON.parse(sessionStorage.getItem('__dc_cur') || '[640,360]');
    c.style.transform = 'translate(' + pos[0] + 'px,' + pos[1] + 'px)';
    document.documentElement.appendChild(c);
  };
  document.addEventListener('mousemove', e => {
    const c = document.getElementById('__dc_cursor');
    if (c) c.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)';
    try { sessionStorage.setItem('__dc_cur', JSON.stringify([e.clientX, e.clientY])); } catch (_) {}
  }, true);
  document.addEventListener('mousedown', e => {
    const r = document.createElement('div');
    r.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;border-radius:50%;border:3px solid rgba(255,140,0,.85);' +
      'width:10px;height:10px;left:' + (e.clientX - 5) + 'px;top:' + (e.clientY - 5) + 'px;transition:all .45s ease-out;';
    document.documentElement.appendChild(r);
    requestAnimationFrame(() => { r.style.width = r.style.height = '44px'; r.style.left = (e.clientX - 22) + 'px';
                                  r.style.top = (e.clientY - 22) + 'px'; r.style.opacity = 0; });
    setTimeout(() => r.remove(), 600);
  }, true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();
"""

# numbered, visible, usable elements; each gets data-dc-idx so we can find it again
ELEMENTS_JS = r"""
() => {
  document.querySelectorAll('[data-dc-idx]').forEach(e => e.removeAttribute('data-dc-idx'));
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=tab],' +
              '[role=checkbox],[role=menuitem],[contenteditable=true],label[for],summary,[onclick]';
  const out = [];
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0 || e.disabled) continue;
    const text = (e.getAttribute('aria-label') || e.innerText || e.value || e.placeholder || e.title ||
                  e.getAttribute('alt') || e.name || '').replace(/\s+/g, ' ').trim().slice(0, 70);
    const kind = e.tagName.toLowerCase() + (e.type && e.tagName !== 'BUTTON' ? '[' + e.type + ']' : '') +
                 (e.getAttribute('role') ? '[role=' + e.getAttribute('role') + ']' : '');
    e.setAttribute('data-dc-idx', String(out.length));
    out.push({i: out.length, kind, text, checked: !!e.checked});
    if (out.length >= 80) break;
  }
  return out;
}
"""


class Step(BaseModel):
    action: Literal["click", "type", "press", "scroll", "done"]
    element: Optional[int] = Field(description="Element number for click / type; null otherwise.")
    text: Optional[str] = Field(description="Text to type or key to press; null otherwise.")
    note: str = Field(description="One short line on what this shows the viewer.")


def enabled() -> bool:
    return os.environ.get("DEMOCUT_AUTORECORD", "1").strip().lower() not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# finding the live app
# --------------------------------------------------------------------------- #


def _host_ok(url: str) -> bool:
    from backend.pipeline.ingest import public_host  # SSRF guard shared with the screenshot downloads
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or skipped_host(host):
        return False
    return public_host(host)


def skipped_host(host: str) -> bool:
    """"github.com" matches it and its subdomains, "docs." a prefix, "readthedocs" anywhere."""
    for s in SKIP_HOSTS:
        if s.endswith("."):
            hit = host.startswith(s)
        elif "." in s:
            hit = host == s or host.endswith("." + s)
        else:
            hit = s in host
        if hit:
            return True
    return False


def github_homepage(owner: str, name: str) -> str | None:
    """The repo's "website" field (where most teams put the deployed app)."""
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{name}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "democut"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            home = (json.load(r).get("homepage") or "").strip()
    except Exception:
        return None
    if home and not home.startswith("http"):
        home = "https://" + home
    if home.startswith("http://"):  # most sites answer on https too; we never visit plain http
        home = "https://" + home[len("http://"):]
    return home or None


def candidate_urls(readme: str, brief: str) -> list[str]:
    """Links that may be the running app, best first: known app hosts and links next to "live", "demo",
    "try"... in the brief, then in the README. Images and badges are skipped."""
    scored: list[tuple[float, int, str]] = []
    for src_rank, text in enumerate((brief, readme)):
        for m in URL_RE.finditer(text or ""):
            url = m.group(0).rstrip(".,;:!?*_")
            if re.search(r"\.(png|jpe?g|gif|svg|webp|mp4|mov|pdf|zip)(\?|$)", url, re.I):
                continue
            before = text[max(0, m.start() - 60):m.start()]
            if before.rstrip().endswith("]("):  # markdown link: its label sits before "]("
                before = text[max(0, m.start() - 120):m.start()]
            if before.rstrip().endswith("!["):
                continue
            score = (2.0 if any(h in url for h in APP_HOSTS) else 0.0) + (1.0 if LIVE_HINT.search(before) else 0.0)
            scored.append((-score, src_rank, url))
    seen, out = set(), []
    for _, _, url in sorted(scored, key=lambda s: (s[0], s[1])):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def live_url(owner: str, name: str, readme: str, brief: str) -> str | None:
    """The first candidate that is a public https page; the GitHub homepage field goes first."""
    home = github_homepage(owner, name)
    for url in ([home] if home else []) + candidate_urls(readme, brief)[:8]:
        if _host_ok(url):
            return url
    return None


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


def _launch(p):
    try:
        return p.chromium.launch()
    except Exception:
        exe = os.environ.get("DEMOCUT_CHROMIUM") or next(
            (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
             if shutil.which(n)), None)
        mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        exe = exe or (mac if Path(mac).exists() else None)
        if not exe:
            raise RuntimeError("no Chromium: run `playwright install chromium` or set DEMOCUT_CHROMIUM")
        return p.chromium.launch(executable_path=exe)


def _same_site(a: str, b: str) -> bool:
    ha, hb = urllib.parse.urlparse(a).hostname or "", urllib.parse.urlparse(b).hostname or ""
    root = lambda h: ".".join(h.split(".")[-2:])  # noqa: E731
    return root(ha) == root(hb)


def _glide(page, x: float, y: float) -> None:
    page.mouse.move(x, y, steps=18)
    time.sleep(0.15)


def _center(page, idx: int):
    loc = page.locator(f'[data-dc-idx="{idx}"]').first
    loc.scroll_into_view_if_needed(timeout=3000)
    box = loc.bounding_box(timeout=3000)
    if not box:
        raise RuntimeError(f"element {idx} has no box")
    return loc, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _act(page, step: Step) -> None:
    if step.action in ("click", "type"):
        if step.element is None:
            raise RuntimeError("no element")
        loc, x, y = _center(page, step.element)
        _glide(page, x, y)
        page.mouse.click(x, y)
        if step.action == "type":
            time.sleep(0.2)
            page.keyboard.type(step.text or "", delay=TYPE_DELAY_MS)
    elif step.action == "press":
        page.keyboard.press(step.text or "Enter")
    elif step.action == "scroll":
        for _ in range(8):
            page.mouse.wheel(0, VIEWPORT[1] * 0.7 / 8)
            time.sleep(0.04)


def drive(url: str, app: str, about: str, work: Path) -> tuple[list[tuple[float, Path]], list[tuple[float, float]]]:
    """Use the app at ``url``; returns (frames as (time, jpeg), kept windows as (start, end)), wall-clock seconds."""
    from playwright.sync_api import sync_playwright

    frames: list[tuple[float, Path]] = []
    keep: list[tuple[float, float]] = []
    history: list[str] = []
    fdir = work / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            ctx = browser.new_context(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
                                      device_scale_factor=SCALE, accept_downloads=False, locale="en-US")
            ctx.add_init_script(CURSOR_JS)
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)

            def on_frame(ev):
                f = fdir / f"f{len(frames):06d}.jpg"
                f.write_bytes(base64.b64decode(ev["data"]))
                frames.append((float(ev["metadata"]["timestamp"]), f))
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
                except Exception:
                    pass

            cdp.on("Page.screencastFrame", on_frame)
            page.goto(url, wait_until="load", timeout=30000)
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": int(VIEWPORT[0] * SCALE),
                                              "maxHeight": int(VIEWPORT[1] * SCALE), "everyNthFrame": 1})
            t0 = time.time()
            page.mouse.move(VIEWPORT[0] / 2, VIEWPORT[1] / 2)
            time.sleep(OPEN_KEEP_S)
            keep.append((t0, time.time()))
            shot = work / "step.png"
            for n in range(MAX_STEPS):
                if time.time() - t0 > MAX_WALL_S or sum(b - a for a, b in keep) > MAX_TAKE_S:
                    break
                try:
                    page.wait_for_load_state("load", timeout=8000)
                except Exception:
                    pass
                elements = page.evaluate(ELEMENTS_JS)
                page.screenshot(path=str(shot))
                listing = "\n".join(f"{e['i']}: {e['kind']} \"{e['text']}\"" + (" (checked)" if e["checked"] else "")
                                    for e in elements) or "(none)"
                text = gemini.prompt("autorecord_step", app=app, about=about, done=str(n), max_steps=str(MAX_STEPS),
                                     history="\n".join(history) or "(none yet)", elements=listing)
                step = gemini.generate(text, Step, images=[shot], thinking="low")
                if step.action == "done":
                    events.emit("ingest", f"auto-recording: done after {n} actions ({step.note})")
                    break
                start = time.time()
                try:
                    _act(page, step)
                    time.sleep(KEEP_AFTER_S)
                except Exception as e:
                    history.append(f"{n + 1}. {step.action} {step.element} {step.text or ''}: FAILED ({str(e)[:80]})")
                    continue
                if not _same_site(page.url, url):  # never wander off the app
                    page.go_back(timeout=10000)
                    history.append(f"{n + 1}. {step.action} {step.element}: left the app, went back")
                    continue
                keep.append((start - KEEP_BEFORE_S, time.time()))
                desc = f"{step.action} {step.element if step.element is not None else ''} {step.text or ''}".strip()
                history.append(f"{n + 1}. {desc}: {step.note}")
                events.emit("ingest", f"auto-recording {n + 1}: {step.note}")
            hold = time.time()
            time.sleep(END_HOLD_S)
            keep.append((hold, time.time()))
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
        finally:
            browser.close()
    return frames, keep


def kept_concat(frames: list[tuple[float, Path]], keep: list[tuple[float, float]], end: float) -> list[tuple[Path, float]]:
    """(frame, seconds on screen) after cutting everything outside ``keep``: a frame lasts until the next one,
    but only the parts of that span inside a kept window count."""
    out = []
    for i, (t, f) in enumerate(frames):
        nxt = frames[i + 1][0] if i + 1 < len(frames) else end
        d = sum(max(0.0, min(nxt, b) - max(t, a)) for a, b in keep)
        if d > 0.004:
            out.append((f, d))
    return out


def encode(parts: list[tuple[Path, float]], out: Path, work: Path) -> None:
    lst = work / "frames.ffconcat"
    lines = ["ffconcat version 1.0"]
    for f, d in parts:
        lines += [f"file '{f.as_posix()}'", f"duration {d:.4f}"]
    lines.append(f"file '{parts[-1][0].as_posix()}'")  # the concat demuxer drops the last duration otherwise
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    ff("-f", "concat", "-safe", "0", "-i", lst, "-vf", f"fps={FPS},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-movflags", "+faststart", "-an", tmp)
    tmp.replace(out)


def record_take(out: Path, owner: str, name: str, readme: str, brief: str, app: str) -> Path | None:
    """Record the live app into ``out`` (MP4); None when there's no live address or anything fails."""
    if not enabled():
        return None
    url = live_url(owner, name, readme, brief)
    if not url:
        events.emit("ingest", "auto-recording: no live address of the app in the repo or the brief")
        return None
    events.emit("ingest", f"auto-recording the live app at {url}", data={"autorecord_url": url})
    about = (readme or "")[:4000]
    try:
        with tempfile.TemporaryDirectory(prefix="democut-rec-") as tmp:
            work = Path(tmp)
            frames, keep = drive(url, app, about, work)
            if len(frames) < 2:
                raise RuntimeError("the browser sent no frames")
            parts = kept_concat(frames, keep, end=keep[-1][1])
            total = sum(d for _, d in parts)
            if total < 3:
                raise RuntimeError(f"only {total:.1f} s kept")
            encode(parts, out, work)
    except Exception as e:  # never fatal: the run goes on from the repo alone
        events.emit("ingest", f"auto-recording failed ({str(e)[:160]}): the video is made from the repo alone")
        return None
    events.emit("ingest", f"auto-recording: {total:.1f} s of the live app at {url}",
                data={"autorecord_s": round(total, 1)})
    return out
