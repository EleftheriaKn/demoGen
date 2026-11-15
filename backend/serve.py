"""Start DemoCut for the phone demo (owner: B, tickets B12-B14).

    python -m backend.serve                 # http://<laptop>:8000, QR code in the terminal
    python -m backend.serve --tunnel        # also an https link through cloudflared (any Wi-Fi)
    DEMOCUT_FAKE=assets python -m backend.serve   # fake stages as usual

Runs C's app (backend/app.py, which loads .env) unchanged, plus:
- prints every address the phone can use, which stages are real or fake, missing keys (never their values)
  and the GitHub login callback URL to register for this address
- logs each new device that connects (backend/devices.py) and which device started which run
- GET /qr.svg: a QR code of the phone address, to show on the laptop screen
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import segno
import uvicorn
from fastapi.responses import Response

from backend.app import app  # importing it loads .env
from backend.contracts import STAGES
from backend.devices import DeviceLog
from backend.run import RUNS, fake_stages

KEYS = {  # stage -> keys it needs when real
    "transcribe": ["FAL_KEY"], "understand": ["GEMINI_API_KEY", "GEMINI_MODEL"],
    "judge_frames": ["GEMINI_API_KEY", "GEMINI_MODEL"], "script": ["GEMINI_API_KEY", "GEMINI_MODEL"],
    "assets": ["FAL_KEY"], "rubric": ["GEMINI_API_KEY", "GEMINI_MODEL"],
}
public_url: list[str] = []  # what the QR code points at: the tunnel link if any, else the first LAN address


def lan_addresses() -> list[str]:
    """IPv4 addresses other devices on the network can reach (best first)."""
    found: list[str] = []
    try:  # the address of the default route; a UDP 'connect' sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    good = [a for a in dict.fromkeys(found) if not a.startswith(("127.", "169.254.")) and not re.match(
        r"172\.(1[7-9]|2\d|3[01])\.0\.1$", a)]  # skip loopback, link-local and docker bridges
    return good


def qr_terminal(url: str) -> str:
    out = io.StringIO()
    segno.make(url, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def start_tunnel(port: int) -> tuple[subprocess.Popen, str] | None:
    """cloudflared quick tunnel -> (process, https link), or None when it isn't installed or fails."""
    if not shutil.which("cloudflared"):
        print("  --tunnel: cloudflared is not installed (Arch: pacman -S cloudflared, macOS: brew install cloudflared)")
        return None
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")
    found: list[str] = []

    def read():
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=read, daemon=True).start()
    for _ in range(300):  # up to 30 s
        if found or proc.poll() is not None:
            break
        threading.Event().wait(0.1)
    if not found:
        proc.terminate()
        print("  --tunnel: cloudflared did not give a link (check your internet connection)")
        return None
    return proc, found[0]


@app.get("/qr.svg", include_in_schema=False)
def qr_svg() -> Response:
    url = public_url[0] if public_url else "http://localhost:8000"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2, dark="#111827")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def banner(host: str, port: int, tunnel: str | None) -> None:
    lan = lan_addresses() if host in ("0.0.0.0", "::") else [host]
    urls = ([tunnel] if tunnel else []) + [f"http://{a}:{port}" for a in lan]
    public_url[:] = urls[:1] or [f"http://localhost:{port}"]
    fakes = fake_stages()
    print("\nDemoCut ready\n")
    print(f"  laptop:  http://localhost:{port}")
    for u in urls:
        print(f"  phone:   {u}")
    if not urls:
        print("  phone:   no network address found (start with --host 0.0.0.0, or use --tunnel)")
    print(f"\n  stages:  {'  '.join(s + (' (fake)' if s in fakes else '') for s in STAGES)}")
    missing = sorted({k for s in STAGES if s not in fakes for k in KEYS.get(s, []) if not os.environ.get(k)})
    if missing:
        print(f"  MISSING: {', '.join(missing)} in .env: those stages will fail (or fake them with DEMOCUT_FAKE)")
    if os.environ.get("GITHUB_CLIENT_ID"):
        print(f"  GitHub login callback for this address: {public_url[0]}/auth/github/callback")
    else:
        print("  GitHub login: off (no GITHUB_CLIENT_ID); the repo URL field still works")
    print(f"  new devices: logged here and in {RUNS / 'devices.jsonl'}")
    print(f"\n  scan with the phone ({public_url[0]}), or open /qr.svg on the laptop:\n")
    print(qr_terminal(public_url[0]))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m backend.serve", description="Start DemoCut for the phone demo.")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the phone (default)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tunnel", action="store_true", help="also open an https link with cloudflared")
    args = ap.parse_args()

    sys.stdout.reconfigure(line_buffering=True)  # the banner shows before uvicorn's own lines
    app.add_middleware(DeviceLog, log_file=RUNS / "devices.jsonl")
    tunnel = start_tunnel(args.port) if args.tunnel else None
    banner(args.host, args.port, tunnel[1] if tunnel else None)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if tunnel:
            tunnel[0].terminate()


if __name__ == "__main__":
    main()
