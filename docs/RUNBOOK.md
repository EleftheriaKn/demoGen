# DemoCut Runbook

Nov 14, 2026 · @thanos

## What we're building

DemoCut turns a GitHub repo, one rough screen recording and the judging brief into a 2–3 minute
demo video, cut from the real footage. Gemini decides what to show, fal makes the voice, music and
title cards, and ffmpeg cuts it. We are an editor, not a generator: the real footage is the backbone
(at least 70% of the runtime). Where footage is weak, fal adds title cards and infographics **built
from the real frames**, and every fact on them is truth-checked.

**The demo is a mobile app.** The user gives it three things and gets the video back:

1. **A GitHub repo**: log in with GitHub, the app lists your repos, tap one.
2. **A screen recording**: up to 7 minutes, recorded on the phone (or picked from the gallery).
3. **A judging brief**: a few lines typed (or pasted) from the hackathon page.

**Why we win:** other tools take only one of these. A repo-only tool can't show the product
working; a video-only editor doesn't know what the code really does; neither knows what the
judges score. With all three, DemoCut cuts the moments the judges care about and the truth check
proves every claim against the code.

Start Sat 22:00. Code freeze Sun 18:00. The starter code is in `democut-starter.zip`; full code and
fixtures are on the DemoCut Architecture page.

**Languages:** for now everything is Greek and English only (`en`, `el`): voice, captions, cards, prompts.

## Golden rules

1. **Own your files.** Only edit files in your column of the ownership table. Need a change in
   someone else's file? Ask them.
2. **Files are the interface.** Each stage reads and writes JSON in `runs/<id>/`. Test your stage
   against `fixtures/`, never wait for a teammate.
3. **Contracts are frozen after 23:00.** Changing a JSON field means telling the other two first.
4. **Small commits to `main`, often.** Pull before you push. No long branches tonight.
5. **Re-run, never hand-edit the video.** The pitch line "nobody edited this" must stay true.

## Tech stack

Python does everything except the board, which is one plain HTML page. No React, no Remotion, no database.

| Layer | Tool | Why |
|---|---|---|
| Language | Python 3.11+ | One language for the whole team |
| Brains | Gemini via `google-genai`, current Flash model | Watches video, reads images, returns JSON to a schema |
| Media | fal via `fal-client` | Voice, music, title cards, transcription; fal track |
| Video | ffmpeg 5+ with libass and freetype | Cut, fit, caption, mix, render |
| Scene cuts | ffmpeg `scdet` (replaced PySceneDetect on Nov 15: same cuts, exact on variable-frame-rate phone takes, ~10x faster on HEVC/HDR) | Snaps Gemini's timestamps to real cuts |
| API | FastAPI + uvicorn, events streamed with SSE | Starts runs, streams progress, serves files |
| Board / mobile app | One `web/index.html`, plain JavaScript, mobile-first and installable (PWA: manifest + "Add to Home Screen") | Login, repo picker, record/upload, brief, progress, player; no build step, no app store |
| Login | GitHub OAuth app (FastAPI routes) | Lists the user's repos; the token also lets ingest clone private repos |
| Agent tool | `mcp` 2.x (`MCPServer`) | `make_demo_video` for Claude Code |
| Storage | Plain files in `runs/<id>/`, cache in `.cache/` | Resume any run; re-runs cost nothing |
| Repo | One GitHub repo, everyone on `main` | Fewest moving parts |

## How the pieces connect

The board, the API and the MCP tool all start the same thing: `run.py`, which runs ten stages in order.

![DemoCut pipeline · 10 stages, colour = owner](pipeline.png)

Inputs: GitHub repo (picked after GitHub login, or a pasted URL) · screen recording (≤ 7 min) ·
judging brief (free text) and time limit
→ **Mobile app / board, API or MCP tool, then `run.py`** (C): writes `run.json`, runs the 10 stages in order,
and skips any stage whose output file already exists.

| # | Stage | Owner | Tool | Writes |
|---|---|---|---|---|
| 01 | ingest | B | git, ffmpeg | `repo_files.json` |
| 02 | understand | A | Gemini: video | `moments.json` |
| 03 | candidates | B | PySceneDetect | `candidates.json` |
| 04 | rank | A | Gemini: images | `rankings.json` |
| 05 | script | A | Gemini | `script.json` |
| 06 | truth | A | Gemini + code | `claims.json` |
| 07 | assets | A | fal | `assets.json` |
| 08 | timeline | B | ffmpeg | `timeline.json` |
| 09 | captions | B | Python | `captions.srt` |
| 10 | render | B | ffmpeg | `final.mp4` |

Outputs: `final.mp4` · `captions.srt` · `decisions.json`. Every stage reads and writes files in
`runs/<id>/`. Every Gemini and fal answer is cached in `.cache/`.

> Note: the repo's `backend/contracts.py` and `fixtures/run/` define the frozen file contracts
> (11 stages: ingest, transcribe, understand, candidates, judge_frames, script, assets, timeline,
> rubric, captions, render). Where this table differs, the contracts win.

Each stage is one Python file with a `run(run_dir, cfg)` function. It reads the files before it and
writes one new file; that file existing means the stage is done.

## The mobile app

The phone is the only thing the user touches. Three screens, then a progress screen and the player.

| Step | Screen | What happens |
|---|---|---|
| 1 | **Log in with GitHub** | GitHub OAuth. The API calls `GET /user/repos?sort=updated` and shows the list; the user taps one. Fallback: a "paste a repo URL" field (no login). |
| 2 | **Recording** | The user picks a screen recording made with the phone's own screen recorder (iOS Control Centre / Android quick settings), or films with the camera. The app checks the length (≤ 7 min) before uploading. |
| 3 | **Brief** | A text box: paste the judging brief; time limit (default 3 min) and language (English or Greek). |
| 4 | **Making your video** | Live progress from `/runs/{id}/events` (SSE), one line per stage. |
| 5 | **Done** | Plays `final.mp4`, with buttons to download and share. |

A web page can't screen-record other apps on a phone (no screen capture API on mobile
browsers), and a native app would need ReplayKit / MediaProjection plus an app store build. So the
phone's built-in screen recorder makes the take and the app uploads it: same result, no native code.

### How big is a 7-minute recording?

Size = bitrate × 420 s ÷ 8. Phones record at very different bitrates:

| Recording | Bitrate | 7 minutes |
|---|---|---|
| Screen recording, 720p | ~4 Mbit/s | **210,000,000 bytes** (~210 MB) |
| Camera 1080p30, HEVC (iPhone default, ~60 MB/min) | ~8 Mbit/s | **420,000,000 bytes** (~420 MB) |
| Camera 1080p30, H.264 ("most compatible" / many Androids) | ~17 Mbit/s | **892,500,000 bytes** (~890 MB) |
| Camera 4K30, HEVC (~170 MB/min) | ~23 Mbit/s | **1,207,500,000 bytes** (~1.2 GB): too big |

**Limits we set:** max **7:00** (420 s, checked in the app and again by the API with ffprobe) and max
**1 GiB = 1,073,741,824 bytes** per upload. Ask users to record at 1080p or lower (screen recordings
already are). A typical take is 200–450 MB.

What this means for the build:

- **Upload in chunks.** One 400 MB POST over phone Wi-Fi fails too often. The app sends 8 MB parts
  and the API appends them, so a dropped connection resumes instead of restarting (C).
- **The API must be reachable from the phone**: a tunnel (`cloudflared` / `ngrok`) to the laptop is
  enough for the demo (C).
- **Phone footage is portrait, HEVC, variable frame rate, often `.mov` with a rotation flag.** The
  render must handle all of that: portrait footage goes into a 16:9 frame over a blurred copy of
  itself instead of black bars (B).
- **Gemini** takes the whole take in one call: the Files API allows 2 GB per file, and 7 minutes is
  roughly 125k tokens at default media resolution (about 40k at low). A long take makes the run slower,
  not impossible; use low media resolution in cheap mode (A).

### The brief is free text

The user types or pastes the brief. Today `run.json` has `brief.criteria` (a list) and `time_limit_s`.
Proposal: add an **optional** `brief.text` (the brief as typed) to `contracts.py`. A's understand
stage extracts `criteria` from it when the app sends only text. This only adds an optional field,
so nothing breaks, but rule 3 still applies: **A, B and C agree first**.

### The GitHub token

The token stays on the server (in the login session) and is **never written to `run.json`, events
or logs**. Ingest gets it from the environment (`GITHUB_TOKEN`) to clone private repos; public
repos need no token. Scopes: `read:user` and `repo` (for private repos), or no `repo` scope if we only
demo public repos.

### Starting the server (today, until B12–B14)

The board, the API and the phone app are one FastAPI process (`backend/app.py`, run by uvicorn).
Each run it starts is a separate `python -m backend.run runs/<id>` process, logging to
`runs/<id>/pipeline.log`.

```bash
cd demoGen
DEMOCUT_FAKE=transcribe,understand,judge_frames,script,assets,rubric \
  .venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

- `.env` (keys, GitHub login) is loaded by the app at start (C11); real environment variables win.
- `--host 0.0.0.0` lets the phone reach it; `127.0.0.1` keeps it laptop-only.
- `DEMOCUT_FAKE` lists the stages not built yet; take each one off as it becomes real. The list above
  runs B's stages for real on the uploaded take (tested Nov 15 with C's TodoMVC take: 12 s).
- Linux with ufw: open the port to the Wi-Fi once, e.g. `sudo ufw allow from 172.20.0.0/20 to any port 8000 proto tcp`
  (use your own network). macOS asks the first time.
- The laptop's address: `ip -4 -br addr` (Linux) or `ipconfig getifaddr en0` (macOS). Phone:
  `http://<address>:8000`. Stopgap QR: `qrencode -t ansiutf8 http://<address>:8000`.
- Venue Wi-Fi that keeps devices apart: join the laptop to the phone's hotspot, or
  `cloudflared tunnel --url http://localhost:8000` (anyone with the link can start runs: close it after).
- Every request is in the uvicorn log with the device's address (`172.20.x.x - "POST /runs"`), but
  nothing says *which* device that is or that it is new: that is B13.
- GitHub login (C11) sends the phone back to `http://<the address you opened>:8000/auth/github/callback`,
  and GitHub only accepts the callback registered in the OAuth app. A new Wi-Fi address or a new tunnel
  link means updating the OAuth app's callback URL (B12 prints the one to use).

## Generated shots: infographics from the real footage

Gemini and fal can do more than title cards. A shot can be an **infographic** made from the
recording: fal gets real frames as reference images, so the picture uses the app's real screens
and colours. It's still an editor: generated shots explain the footage, they never replace it.

**fal draws, we write.** Image models still garble text (Greek most of all), and text inside a
generated image would skip the truth check. So fal makes only the picture (background, illustration,
layout), and render puts every word on top in our font. Every word on screen then comes from
`script.json`, is spelled right and has passed the truth check.

| Kind of shot | What fal makes | What render adds | Where it goes |
|---|---|---|---|
| Title card | Background in the app's style (frames as reference) | Title, one-liner | Hook, ask |
| Infographic | Diagram or layout, e.g. "how it works" in 3 steps, with real screens inside | Step titles, bullets, a number | Problem, how, impact |
| Framed real frame | Nothing, or a styled background | A real frame inside a phone/laptop mockup or picture-in-picture, slow zoom | Anywhere footage is too small or busy |
| Motion b-roll (optional) | A short clip animated from a real frame | Captions only | Hook; at most 1–2 per video |

**Rules**

1. **Demo beats stay real footage.** Generated shots only go in hook, problem, how, impact and ask.
2. **At least 70% of the runtime is real footage**, counting framed real frames. The script prompt
   asks for it; the rubric checks it in code from `timeline.json`.
3. **Every fact on an infographic is a backed claim.** Overlay text goes in the shot's `claims`,
   so the truth check covers it like a voice line.
4. **Generated shots are marked.** `Shot.generated` already exists; the board shows a badge, and the
   pitch says it out loud: "the footage is real, the explainer is generated from it".
5. **Best of 4** drafts per generated image, picked by the judge (as in A7). Cheap mode: no fal,
   plain colour background plus render's overlay, so the whole flow works without fal credit.
6. **Motion b-roll is slow and costly** (minutes per clip): 1–2 max, run in parallel, cached, and
   first in the cut order.

**Contract proposal (optional fields only: A, B and C agree first, rule 3)**

An infographic stays `kind: "card"`, so no existing value changes. New optional fields:

```python
class Overlay(Contract):
    layout: Literal["title", "bullets", "steps", "stat"]
    title: Optional[str] = None
    items: list[str] = []          # bullets or steps, max 4, each a backed claim's wording
    stat: Optional[str] = None     # one big number, e.g. "3 recipes in 5 s"

class ShotVisual(Contract):        # added fields
    reference_frames: list[str] = []   # frames/... from candidates.json, sent to fal as references
    inset_frame: Optional[str] = None  # a real frame shown inside the shot (mockup / picture-in-picture)
    overlay: Optional[Overlay] = None  # text render draws on top

class TimelineVisual(Contract):    # added fields, copied from the script by timeline
    inset_frame: Optional[str] = None
    overlay: Optional[Overlay] = None
```

## Who owns which files

No file has two owners, so three people can push to `main` all night without merge conflicts.

| Person | Owns (only they edit) | Writes in each run | Reads from others |
|---|---|---|---|
| A · Brains | `backend/clients/gemini.py`, `backend/clients/fal.py`, `backend/pipeline/understand.py`, `judge.py`, `script.py`, `assets.py` | `summary.json`, `moments.json`, `transcript.json`, `rankings.json`, `script.json`, `claims.json`, `assets.json`, `decisions.json` | `repo_files.json`, `candidates.json` (B) |
| B · Render | `backend/pipeline/ff.py`, `ingest.py`, `candidates.py`, `timeline.py`, `captions.py`, `render.py`, `fonts/`, `backend/serve.py`, `backend/devices.py` | `repo_files.json`, `candidates.json`, `timeline.json`, `captions.srt`, `final.mp4` | `moments.json`, `script.json`, `assets.json` (A) |
| C · App and pitch | `backend/run.py`, `events.py`, `cache.py`, `app.py`, `mcp_server.py`, `web/`, `samples/`, `README.md`, slides | `run.json`, `events.jsonl` | Everything, read only |
| Shared | `fixtures/`: change only after telling the other two | | |

## Tickets · A · Brains (Gemini and fal)

A makes every decision and every generated asset. Test each stage alone with
`python -m backend.run runs/dev --only <stage>`.

| ID | Ticket | Done when | By | Status |
|---|---|---|---|---|
| A1 | Keys and clients: `gemini.py`, `fal.py` | One Gemini JSON call and one fal voice call both return | 23:00 | To do |
| A2 | Check fal field names for scribe, voice, FLUX 2, Nano Banana 2, music | Every line marked *check field names* fixed | 00:00 | To do |
| A3 | `understand.py`: summary and moments | `moments.json` from the test take, on-screen text copied word for word | 01:30 | To do |
| A4 | Voice in `assets.py` | One MP3 per shot from fixture `script.json`, lengths in `assets.json` | 02:00 | To do |
| A5 | `script.py` and the hook pick | `script.json` from real moments, uses only real moment ids, one hook chosen | 04:00 | To do |
| A6 | `judge.py`: ranking and truth check | `rankings.json`; `claims.json` with quotes verified by code; failed claims rewritten | 05:30 | To do |
| A7 | Title cards and music | Best of 4 cards picked by the judge; plain cards in cheap mode; music bed | 11:00 | To do |
| A8 | Zoom boxes (should have) | One call per chosen frame writes a zoom box into `script.json` | 11:00 | To do |
| A9 | Our own video | Version 1 at 12:00; fixes only through prompts or code; version 2 by 14:00 | 14:00 | To do |
| A10 | Brief from free text (mobile app) | `understand` fills `brief.criteria` from `brief.text` when the app sends only text; 7-minute take works (low media resolution in cheap mode) | 05:30 | Proposed |
| A11 | Infographic shots | `script.py` picks which non-demo shots are infographics, writes `overlay` + `reference_frames` (overlay text only from backed claims); `assets.py` sends the frames to fal as references, best of 4; rubric checks ≥ 70% real footage | 11:00 | Proposed |

## Tickets · B · Render (ffmpeg)

B turns decisions into pixels. B needs no keys until B4, so B can start immediately.

| ID | Ticket | Done when | By | Status |
|---|---|---|---|---|
| B1 | Setup: ffmpeg 5+, font, fake media | `scripts/make_fake_media.sh` runs; `NotoSans-Bold.ttf` in `fonts/` | 23:00 | Done |
| B2 | Render half on fixtures | timeline → captions → render gives `runs/dev/final.mp4` | 23:30 | Done |
| B3 | Render on the real test take | Works on C's take in `samples/`, whatever its size and frame rate | 00:30 | Done on 7 test takes; recheck on C's take |
| B4 | `ingest.py` | Clones a real repo; `repo_files.json`; `audio.wav` when the take has sound | 01:00 | Done (also private repos via `GITHUB_TOKEN`; silent `audio.wav` when the take has no sound) |
| B5 | `candidates.py` | Scene cuts and 3 frames per moment from A's `moments.json` | 02:00 | Done (7-min 1080p take: ~28 s, max 40 scene clips) |
| B6 | Checkpoint 1, with A | Test take + fixture script + real voices → captioned MP4 | 02:00 | Done (Nov 15: TodoMVC 3-min take, fal voices, 22 s; captions on a dark box for light screens) |
| B7 | First full run, with A, then speed | A full run takes under 15 minutes; add a 720p preview mode if not | 05:30 | Done (Nov 15: 3-min TodoMVC take, all real but transcribe + rubric: 5.0 min full/1080p, 5.4 min cheap/720p; script is 50–60% of it) |
| B8 | Robustness | Handles no audio, very short or long moments, a failed fal call; resumes mid-run | 09:00 | Done for B (Nov 15): no-audio takes, edge moments, missing card/music files, atomic `final.mp4`, resume after a crash. Waiting on A: plain card + silent music when fal fails; a failed voice stops the run with a clear message (team decision) |
| B9 | Final video | Version 1 checked frame by frame, bugs fixed, version 2 uploaded | 15:30 | To do |
| B10 | Phone footage (mobile app) | A portrait, HEVC, variable-frame-rate `.mov` take of 7 minutes renders correctly: rotation applied, portrait over a blurred fill, no A/V drift. Ingest clones private repos with `GITHUB_TOKEN` | 05:30 | Proposed |
| B11 | Overlays, mockups, picture-in-picture | Render draws `overlay` text (title / bullets / steps / stat, Greek and English) over cards; `inset_frame` inside a device mockup or picture-in-picture with a slow zoom; stills get gentle motion; works with plain backgrounds in cheap mode | 11:00 | Proposed |
| B12 | One command starts the server | `python -m backend.serve` (new file, B; Linux and macOS) runs C's app unchanged on `0.0.0.0:8000` and prints `DemoCut ready` with every phone address on the Wi-Fi, which stages run real or fake (from `DEMOCUT_FAKE`), a warning for real stages missing a key (never printing keys), and the GitHub callback URL to register for this address; `--tunnel` starts cloudflared when installed and prints its `https` link; Ctrl-C stops it and any tunnel cleanly. README's phone section points to it | 09:00 | Proposed |
| B13 | Log new devices | `backend/devices.py` (new file, B), attached to C's app by `serve.py` (no change to `app.py`): the first request from a device prints one line, e.g. `new device: iPhone · Safari · 172.20.1.42`, and appends it to `runs/devices.jsonl` (time, address, device, browser: nothing else). A device id cookie (address + browser as fallback) keeps reloads and later requests quiet. Starting a run logs which device started it | 09:00 | Proposed |
| B14 | QR code to open the app | At start the terminal shows a QR code of the phone address (or the tunnel link); `serve.py` adds `GET /qr.svg` (made in Python, e.g. `segno`, so no system tool is needed). On the board C adds one `<img src="/qr.svg">` for laptop screens (C's file: ask C). Done when scanning the laptop screen opens the board on the phone, on Linux and macOS | 09:00 | Proposed |

## Tickets · C · App, board, MCP and pitch

C builds what people see and owns the story. C sets up the repo first, so A and B can clone it.

| ID | Ticket | Done when | By | Status |
|---|---|---|---|---|
| C1 | Repo from the starter zip | GitHub repo up, A and B invited, `.env` template, `.gitignore` | 22:15 | To do |
| C2 | Test take | Rough 3-minute recording of any app in `samples/`, with its repo URL | 23:00 | To do |
| C3 | Orchestrator and API | `POST /runs` starts a run; `/runs/{id}/events` streams a fixture run | 00:30 | To do |
| C4 | Board | `web/index.html` uploads, shows progress, plays `final.mp4` | 01:30 | To do |
| C5 | MCP tool | `make_demo_video` listed in Claude Code and returns a cached run | 02:00 | To do |
| C6 | Board shows the decisions | Rankings, rejected claims and facts from `decisions.json` | 08:00 | To do |
| C7 | DemoCut's own take | Rough 4-minute recording of DemoCut in use | 10:00 | To do |
| C8 | 20-second test | 5–10 people: our cut vs a video another team made by hand | 13:00 | To do |
| C9 | Pitch, slides, README | 3-minute script, 3 slides, cached live demo; manual OBS video by 15:00 if checkpoint 3 fails | 14:00 | To do |
| C10 | Mobile app flow | `web/index.html` works on a phone: repo → recording → brief → progress → player; installable (manifest); repo-URL field works without login | 05:30 | Done |
| C11 | GitHub login | OAuth login; repo list from `GET /user/repos`; tap a repo fills `repo_url`; token kept server-side only | 09:00 | Done |
| C12 | Big uploads from the phone | Chunked, resumable upload of a 7-minute take (≤ 1 GiB); API rejects > 420 s or > 1 GiB; phone reaches the API through a tunnel | 05:30 | Done |
| C13 | Generated badges | Board marks generated shots (`Shot.generated`) and shows the real-footage share from the rubric | 11:00 | Proposed |

## Make runs fast

A run's time goes to Gemini, fal and ffmpeg, not to Python, so these five changes matter more than the language.

| # | Do this | Who | When |
|---|---|---|---|
| 1 | **Run fal calls in parallel.** In `assets.py`, run voice, card and music calls 4–8 at a time with a thread pool instead of one by one. Biggest single saving. | A | With A4 |
| 2 | **Cache everything.** Already in `cache.py`: a repeat run of the same take costs nothing and only re-renders. Never clear `.cache/` before the pitch. | All | Always |
| 3 | **Cheap mode while developing.** Plain cards, no best-of-4; switch to full mode only for real runs. | All | Until 11:00 |
| 4 | **720p previews.** A preview flag in `ff.py` that scales to 1280×720, about half the encode time. 1080p for the final video only. | B | With B7 |
| 5 | **Short test takes.** Use a 1-minute take while debugging; the full 3–4 minute take only at the checkpoints. | C | From C2 |

## Timeline, Sat 22:00 to Sun 18:00

Three checkpoints decide what gets cut. Each person sleeps once, staggered, so two people are always building.

| Time | A · Brains | B · Render | C · App and pitch |
|---|---|---|---|
| 22:00–23:00 | A1 | B1 | C1, C2 |
| 23:00–02:00 | A2, A3, A4 | B2, B3, B4, B5 | C3, C4, C5 |
| **02:00 · Checkpoint 1** | Test take + fixture script + real voices → captioned MP4. Fails: A and B pair on it; music waits. | | |
| 02:00–05:30 | A5, A6 | B6, B7 | Sleep |
| **05:30 · Checkpoint 2** | Repo URL + test take → board → MP4, no hand steps, under 20 min. Fails: cheap mode, plain cards, fix speed before features. | | |
| 05:30–09:00 | Sleep | B8 | C6 |
| 09:00–11:00 | A7, A8 | Sleep until 12:00 | C7 at 10:00, start C8 |
| **11:00 · Feature freeze** | Only fixes from here. | | |
| 11:00–14:00 | A9 | B9 from 12:00 | C8, C9 |
| **14:00 · Checkpoint 3** | Version 2 is the submission, or C finishes a manual OBS video by 15:00. | | |
| 14:00–15:30 | README with architecture | Upload the video early | Rehearse |
| 15:30–16:30 | All: submit repo, video and details | | |
| 16:30–17:30 | All: rehearse the pitch 3 times, test the projector | | |
| 17:30–18:00 | Buffer | | |

**Cut order when a checkpoint fails:** animated zoom and B-roll first, then best-of-4 cards (use plain
cards), then fal infographic backgrounds (keep render's text overlays on plain backgrounds), then music, then the MCP tool, then GitHub login (keep the paste-a-repo-URL field). Never cut
the truth check: it is the pitch. Never cut the three inputs on the phone: they are the pitch too.

> The "Proposed" tickets come from decisions on Nov 15: the mobile app (A10, B10, C10–C12), starting the
> server, device log and QR code (B12–B14) and
> infographics from the real footage (A11, B11, C13). Owners
> confirm them and set the times; they don't change who owns which files.

## Pitch: 3 minutes

Open on the video itself, then reveal that nobody edited it.

| Time | Beat | What's on screen |
|---|---|---|
| 0:00–0:15 | Our submission video | First 15 seconds, full screen |
| 0:15–0:30 | The pain | "Every team here spent its last hours on this video." |
| 0:30–0:50 | Reveal | Raw take next to the final cut; time and cost to make it |
| 0:50–1:40 | How it works | On the phone: tap a repo, pick the take, paste the brief. Then a cached run: moments ranked, one claim rejected |
| 1:40–2:00 | Proof | 20-second test result and time to video |
| 2:00–2:25 | Agents can call it | Claude Code calls `make_demo_video` on a cached run |
| 2:25–3:00 | Close | Who needs it next: every hackathon, launch and open-source release |

**Numbers to measure:** wall-clock time from repo URL to MP4, human minutes spent, fal cost per video,
claims made vs rejected, and the 20-second test score.

**Questions to rehearse:**

- *"How is this different from Proof & Demo?"* It's hands-off from one take, an agent can call it,
  and fal generates takes the judge chooses between.
- *"Lots of tools already make demo videos. Why yours?"* None takes all three: the repo (what the
  code really does), the take (the product really working) and the brief (what the judges score).
  The three together let us pick what matters to these judges and prove every claim.
- *"Isn't this just Higgsfield?"* No: Higgsfield generates new pixels; DemoCut edits your real
  footage and won't let the video claim what the product can't do. Our few generated shots are
  built from your real frames, marked as generated, and every fact on them is checked.

## When things break

| Problem | Quick fix |
|---|---|
| A run failed halfway | Fix it and run the same command again; it resumes from the last finished stage |
| One stage is wrong | Delete that stage's output file, then `--only <stage>` |
| Gemini rate limit | Wait; calls retry with back-off and are cached, so nothing is paid twice |
| A fal output field is wrong | Print the result once, fix the marked line in `assets.py` |
| No fal at all | Cheap mode: plain cards, no music; the video still renders |
| Gemini timestamps look off | Check `candidates.json`; they snap to scene cuts within 1.5 s |
| The join fails | A shot is missing audio or has another frame rate; every shot must go through `render.shot()` |
| Captions show boxes instead of letters | The font in `fonts/` is missing or has no Greek letters |
| Merge conflict | Someone edited a file they don't own; the owner's version wins |
| Stuck for 30 minutes | Say it out loud, then cut the feature using the cut order above |
