# DemoCut

**One rough screen recording + the GitHub repo + the judging brief → a 2–3 minute demo video, cut from the real footage.**

Every hackathon team spends its last hours editing a demo video. DemoCut does it hands-off:
Gemini watches the recording and decides what to show, fal makes the voice, music and title
cards, and ffmpeg cuts it. It is an editor, not a generator: almost every frame is real
footage, and a truth check stops the video from claiming anything the product can't do.

Nobody edited the final video by hand. Fixes go through prompts or code, then a re-run.

## Quick start

Needs Python 3.11+ and ffmpeg 5+ (with libass and freetype).

```bash
brew install ffmpeg                       # or your OS's package manager
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # add GEMINI_API_KEY, GEMINI_MODEL, FAL_KEY
pytest -q                                 # everything should pass
```

Try the whole app with **no keys**: fake mode copies example outputs from `fixtures/run/`
instead of calling Gemini, fal or ffmpeg.

```bash
DEMOCUT_FAKE=all uvicorn backend.app:app  # then open http://127.0.0.1:8000
```

## Three ways to make a video

All three start the same pipeline (`backend/run.py`) in its own process.

**1. The board / phone app.** `uvicorn backend.app:app`, open http://127.0.0.1:8000. Three
steps: paste the repo URL, pick the recording (up to 7 minutes and 1 GB, checked on the device
before upload), paste the brief. You see each stage live, then the video with Download and Share,
then how it decided: which shots are real footage and which are generated (with the real-footage share, target ≥ 50%), claims backed or rejected, ranked frames and hooks, the facts it found in the
repo and the final check. `http://127.0.0.1:8000/#<run_id>` opens one run. On a phone, use the
browser's *Add to Home Screen* / *Install* to get it as an app.

**On your phone (same Wi-Fi as the laptop).** The easy way:

```bash
python -m backend.serve          # prints the phone address, checks keys, shows a QR code
python -m backend.serve --tunnel # https link through cloudflared, if the Wi-Fi keeps devices apart
```

Scan the QR code in the terminal, or the one on the board's laptop screen, with the phone's camera.
The manual way: `uvicorn backend.app:app --host 0.0.0.0 --port 8000`, then open
`http://<laptop-ip>:8000` on the phone (`ipconfig getifaddr en0` on macOS).

Recordings up to 7:00 and 1 GiB upload in 4 MB parts; if the Wi-Fi drops, the upload carries on
from where it stopped (even after reloading the page and picking the same file). The server checks
both limits again with ffprobe before a run starts. Every upload is logged in the server's terminal and in
`runs/_uploads/<id>.log` (moved to the run as `upload.log`), including the exact error a phone hit.
Over plain `http`, phones don't offer *Install*
and *Share* falls back to copying the link; upload and playback work. If the venue Wi-Fi keeps
devices apart, a tunnel such as `cloudflared tunnel --url http://localhost:8000` gives an `https`
link instead (optional, not a project dependency).

**Keys from the board.** On the laptop (`http://localhost:8000`), *⚙ Settings* sets the Gemini, fal
and GitHub keys: saved to `.env`, used from the next run, no restart. Saved keys are never shown
again. Phones, other devices on the Wi-Fi and tunnels can't open it.

**GitHub login (optional).** With an OAuth app's keys (in Settings or `.env`), step 1 shows *Log in with
GitHub*, then your repos, most recently updated first: tap one instead of pasting a link.
Create the app at https://github.com/settings/developers → *New OAuth App*, with callback URL
`http://localhost:8000/auth/github/callback` (for a phone: `http://<laptop-ip>:8000/auth/github/callback`),
and set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` (see `.env.example`). Add `repo` to
`GITHUB_OAUTH_SCOPES` for private repos. The token stays in the server's memory: it is never
written to `run.json`, events or logs, and a run gets it only as `GITHUB_TOKEN` in its own process.

**2. The command line.**

```bash
python -m backend.run                           # new run from the fixture take
python -m backend.run runs/<run_id>             # run, or resume, a run
python -m backend.run runs/<run_id> --only script   # redo one stage
python -m backend.stats runs/<run_id>               # pitch numbers: time, claims rejected, real footage
```

**3. Claude Code (MCP).** Add the tool once, from the repo root:

```bash
claude mcp add democut -- "$PWD/.venv/bin/python" -m backend.mcp_server
```

Then ask Claude Code to *"make a demo video of github.com/you/app from ~/Desktop/take.mov"*.
It calls `make_demo_video`. The same repo, recording and brief return the finished run at
once, at no cost. `demo_video_status` checks a run started with `wait: false`.

## How it works

![DemoCut pipeline](docs/pipeline.png)

Each stage is one Python file with `run(run_dir, cfg)`. It reads the files before it in
`runs/<run_id>/` and writes its own. **Files are the interface**: stages never call each other.
Every file has a Pydantic model in [`backend/contracts.py`](backend/contracts.py).

| # | Stage | Tool | Writes |
|---|---|---|---|
| 1 | ingest | git, ffmpeg | `repo_files.json`, `audio.wav`, `screenshots/` |
| 2 | transcribe | fal | `transcript.json` |
| 3 | understand | Gemini (video) | `summary.json`, `moments.json` |
| 4 | candidates | PySceneDetect, ffmpeg | `candidates.json`, `frames/` |
| 5 | judge_frames | Gemini (images) | `frame_scores.json`, `decisions.json` |
| 6 | script | Gemini + code | `script.json` (hooks, shots, checked claims) |
| 7 | assets | fal | `assets.json`, `voice/`, `music.wav`, `cards/` |
| 8 | timeline | Python | `timeline.json` |
| 9 | rubric | Gemini | `decisions.json` (final check; may ask for one recut) |
| 10 | captions | Python | `captions.srt`, `captions.ass` |
| 11 | render | ffmpeg | `shots/`, `final.mp4` |

**The truth check.** Every factual claim in the voice-over needs evidence: a stretch of the
footage, a README section or a repo file. Quotes are verified by code, not by the model.
Unsupported claims are rewritten to match the evidence, or dropped. The board shows each one.

**Resume.** A run that fails halfway resumes from the last finished stage when you run the
same command again. Delete a stage's output (or use `--only <stage>`) to redo it.

**Cache.** Every Gemini and fal answer is stored in `.cache/`, keyed by the request and the
*content* of any file in it ([`backend/cache.py`](backend/cache.py)). A repeat run of the same
recording costs nothing and only re-renders. `DEMOCUT_NO_CACHE=1` ignores stored answers.

**Fake mode.** `DEMOCUT_FAKE=all` or `DEMOCUT_FAKE=understand,script` copies those stages'
outputs from `fixtures/run/`, so every stage can be built and tested before the others exist.

## API

| Method | Path | What |
|---|---|---|
| `GET` | `/` | The board |
| `POST` | `/uploads` | Start a resumable upload (`{filename, size}`), max 1 GiB |
| `PUT` | `/uploads/{id}?offset=N` | Add one part (raw bytes); a wrong offset answers 409 with what arrived |
| `GET` | `/uploads/{id}` | How much arrived, to resume |
| `POST` | `/runs` | Start a run (form: `repo_url`, `upload_id` or `recording`, `brief_text`, `criteria`, `time_limit_s`, `language`, `second_language`, `mode`); the take must be ≤ 7:00 and ≤ 1 GiB |
| `GET` | `/auth/github/login` · `/auth/me` · `/github/repos` | GitHub login (C11), who's logged in, their repos |
| `GET` | `/runs` | Runs, newest first |
| `GET` | `/runs/{id}` | Status, config, finished stages |
| `GET` | `/runs/{id}/events` | Live progress (Server-Sent Events) until the run ends |
| `GET` | `/runs/{id}/files/{path}` | Any file in the run, e.g. `final.mp4`, `decisions.json` |

Languages: English (`en`) and Greek (`el`). Modes: `cheap` (plain cards, fewer judge calls)
and `full` (best of 4 cards, music, full judging).

## Repo layout

```
backend/
  run.py            orchestrator: stages in order, fake mode, resume, --only
  app.py            FastAPI: runs, live events, files, board
  mcp_server.py     make_demo_video for Claude Code
  cache.py          .cache/ for every Gemini and fal answer
  events.py         progress log (events.jsonl), streamed by the API
  contracts.py      one Pydantic model per run file
  clients/          Gemini and fal clients
  pipeline/         one file per stage
web/index.html      the board: one page, plain JavaScript, no build step
fixtures/run/       one complete example run (a made-up app, PantryPal)
samples/            test recordings
fonts/              Noto Sans Bold (Latin + Greek) for captions and cards
docs/RUNBOOK.md     the team's plan, tickets and timeline
```

## Team

| | Owns |
|---|---|
| A · Brains | Gemini and fal clients; understand, judge, script, assets |
| B · Render | ffmpeg helpers; ingest, candidates, timeline, captions, render; fonts |
| C · App and pitch | run.py, events, cache, API, MCP server, board, samples, README, slides |

Built in 20 hours at a hackathon, Nov 14–15, 2026.
