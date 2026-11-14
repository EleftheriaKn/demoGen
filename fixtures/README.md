# Fixtures

`fixtures/run/` is one complete, consistent example of `runs/<run_id>/`. It's a made-up
app, **PantryPal** ("snap a photo of your fridge, get 3 recipes"), cut into a 15 s, 3-shot video.
Each file validates against its model in `backend/contracts.py` (see `MODELS`), and
`tests/test_contracts.py` checks that the files agree with each other.

Use it to build your stage before the upstream stages exist: read your inputs from here
and write outputs that look like these. **Fake mode** (`run.py`, written later by C) copies
`fixtures/run/<STAGE_OUTPUTS[stage]>` into a real run folder for any stage that isn't built yet.

```bash
pip install -r requirements/common.txt
bash fixtures/make_media.sh   # regenerate media (optional: it's committed)
pytest -q
```

Owners: **A** = Gemini (understand, script, judge) · **B** = ffmpeg (ingest, candidates,
timeline, captions, render) · **C** = fal + app (transcribe, assets, orchestrator, API, web).

## Files

| File | Model | Written by (stage) | Owner | What it is |
|---|---|---|---|---|
| `run.json` | `RunConfig` | API, at run creation | C | run_id, repo URL, recording path, brief (criteria, time limit, languages), mode |
| `recording.mp4` | (media) | uploaded input | C | the raw "sloppy" screen recording: 30 s, 1080p30. Every timestamp in every file is within 0–30 s |
| `repo_files.json` | `RepoFiles` | ingest | B | README, file tree, ≤20 key files, downloaded screenshots |
| `audio.wav` | (media) | ingest | B | recording audio, 16 kHz mono (input to transcribe) |
| `screenshots/` | (media) | ingest | B | images from the repo README |
| `transcript.json` | `Transcript` | transcribe | C | word-level speech-to-text of `audio.wav` |
| `summary.json` | `Summary` | understand | A | name, pitch, problem, audience, features (with README/file evidence) |
| `moments.json` | `Moments` | understand | A | 4 moments: setup / action / result / dead |
| `candidates.json` | `Candidates` | candidates | B | 6 segments (3 scene + 3 moment), each with 3 frames |
| `frames/` | (media) | candidates | B | 18 JPEGs, 640 px wide, named `<segment_id>_<t>s.jpg` (t = recording time) |
| `frame_scores.json` | `FrameScores` | judge_frames | A | judge answers + heuristic + total for all 18 frames |
| `script.json` | `Script` | script | A | 3 hooks (h2 chosen), 3 shots (card / footage / zoomed footage), 4 claims: 2 backed, 1 rewritten, 1 dropped |
| `decisions.json` | `Decisions` | judge_frames, script, assets, rubric (each appends) | A | audit log: every kind of decision, 4-question rubric, `recut: null` |
| `assets.json` | `Assets` | assets | C | voice takes (exact durations), music, cards (4 drafts, index 2 chosen), no b-roll |
| `voice/` | (media) | assets | C | `s01..s03.mp3`, sine "voices" of ~4/5/6 s |
| `music.wav` | (media) | assets | C | 15 s quiet music bed |
| `cards/` | (media) | assets | C | `s01.png` (the chosen card) + `drafts/s01_{0..3}.png` |
| `timeline.json` | `Timeline` | timeline | B | 1280×720@30, 15 s: card 4 s → footage 5–9.5 s + 0.5 s freeze → footage 12–18 s with zoom |
| `captions.srt`, `captions.ass` | (text) | captions | B | 3 cues = the voice lines, timed to the timeline shots |
| `shots/` | (media) | render | B | `s01..s03.mp4`, one per timeline shot |
| `final.mp4` | (media) | render | B | concatenated shots, music ducked at −18 dB, burned-in captions |
| `events.jsonl` | `Event` per line | orchestrator | C | example progress log: run start, start/done for each stage, run done (progress 0 → 1). The same JSON goes over SSE |

## Rules worth remembering

- Paths are relative to the run folder. Times are in seconds. Boxes are normalized `[x, y, w, h]`.
- Contracts only grow: add new fields as **optional**. Never rename or remove a field.
  If you change a fixture or a contract, run `pytest -q` before pushing.
- B's timeline takes voice durations from `assets.json` and never runs ffprobe on C's files.
  C writes the exact ffprobe duration there. mp3 padding makes the fixture takes ~0.06 s
  longer than nominal, so the timeline trims them.
- `decisions.json` is read, appended to and written back by each judging stage. `decision.stage` says which stage made each decision.
- Media is placeholder (test patterns and sine tones). `make_media.sh` derives frame times
  from `candidates.json`. Shot timings are hard-coded to match `timeline.json`, so update both together.
