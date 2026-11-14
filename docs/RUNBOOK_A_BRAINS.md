# Runbook · Track A · Brains

Nov 15, 2026 · Track A. Branch: `a/brains`.

Track A covers every Gemini decision in a run: what the app does, which moments matter, which frames
to show, what the voice says, whether it's true, and whether the finished cut passes the rubric.
Terms follow [`CONTEXT.md`](../CONTEXT.md). File shapes follow `backend/contracts.py` and
`fixtures/run/`. Where the shared [`RUNBOOK.md`](RUNBOOK.md) disagrees, the contracts win.

## My stages

| Stage | File | Reads | Writes |
|---|---|---|---|
| `understand` | `backend/pipeline/understand.py` | `repo_files.json`, recording, `transcript.json` | `summary.json`, `moments.json` |
| `judge_frames` | `backend/pipeline/judge.py` (`run_frames`) | `candidates.json`, `frames/` | `frame_scores.json`, `frame_pick` decisions |
| `script` | `backend/pipeline/script.py` | `summary.json`, `moments.json`, `frame_scores.json`, brief | `script.json`, hook-pick and claim-check decisions |
| `rubric` | `backend/pipeline/judge.py` (`run_rubric`) | `timeline.json`, `script.json`, brief | rubric and optional `recut` in `decisions.json` |
| `pick_best()` | `backend/pipeline/judge.py` | N take files + judge questions | `PickResult` (C's assets stage calls it) |

Shared client: `backend/clients/gemini.py`.

## Working rules

- Work on `a/brains`. Merge `main` in often; never commit straight to `main`.
- Test each stage alone against fixtures, with the stages I haven't built yet faked:
  `DEMOCUT_FAKE=all python -m backend.run`, then drop my stage from the list once it's real.
- From ticket 1 on, tests replay recorded Gemini responses by default. `pytest --live` calls Gemini for real and
  refreshes the recordings.
- Gemini settings: one model (`GEMINI_MODEL`), temperature 0, schema output, two retries and then raise.

## Tickets

| # | Ticket | Blocked by | Done when | Status |
|---|---|---|---|---|
| 0 | Decision log helper | none | One function appends to `decisions.json`: assigns the next `decision_id`, checks that the chosen option is one of the options and that answers follow the question order, and writes back a file that still passes the contract | Done |
| 1 | Summary from the repo (tracer bullet) | none | `understand` writes a real `summary.json` from the README, key files and transcript. Brings in the Gemini client, the prompt files and the record/replay harness with `--live`. Every feature's evidence is checked against real README headings and repo files | Done |
| 2 | Moments from the recording | 1 | `understand` also writes real `moments.json` from Gemini watching the recording. The upload happens once per run and later stages reuse it. Code clamps times to the recording length, sorts the moments and fixes overlaps | To do |
| 3 | Frame judging | 0, 1 | `judge_frames` scores candidate frames with the fixed questions per criterion (generic fallback), combines judge 80 / sharpness 20, writes `frame_scores.json` and one `frame_pick` decision per segment. Cheap mode scores fewer frames | Done |
| 4 | Script draft with hooks | 0, 1 | `script.json` follows hook, problem, 2–4 demo, how, impact, ask; targets 75 s and about 190 words with a word budget per beat. Three hooks are written, judged and the pick is logged. Only hook and ask shots may be generated visuals. Footage spans sit inside real segments. Every claim starts as `pending` | Done |
| 5 | Truth check | 4 | A second pass judges each claim against its evidence alone, then keeps, rewrites or drops it. A rewrite never adds words; missing evidence counts as none. Voice lines are updated; one claim-check decision per claim | To do |
| 6 | Zoom on demo and impact shots | 4 | Demo and impact footage shots get a focus box from their best-scored frame. Code checks the box stays inside the frame and is at least 20% of the width | To do |
| 7 | Rubric and recut | 0, 1 | `rubric` checks the four fixed items. Code computes "under the time limit" and "problem by 0:20" from the timeline; Gemini judges the other two. A recut is requested only when a failure comes from one shot. Test: a timeline that fails on one shot makes the orchestrator re-run timeline once (the re-run already exists in `run.py`) | To do |
| 8 | Real `pick_best` | 1 | The stub becomes a real judge for image, audio and video takes, with the same return shape | To do |
| 9 | All of Track A live | 2, 3, 5, 6, 7, 8 | Full Track A on a real app recording, B and C faked, real Gemini calls. Recordings refreshed, breakages fixed | To do |

## Order

1 → 0 → 4 → 5 and 6 → 3 and 7 → 2 → 8 → 9.

- **1 first.** It unblocks five tickets and proves out the client and replay harness.
- **4 early.** It sits on the longest chain (1 → 4 → 5 → 9).
- **2 late.** `fixtures/run/moments.json` keeps later stages working until then, and the video upload is the slowest call to test.
- **8 last.** The stub's return shape is final, so C isn't blocked by it.
- If 1 gets heavy, commit the client and replay harness first, then the summary logic.

## Open questions

| Question | Ask |
|---|---|
| Who owns fal (`backend/clients/fal.py`) and `assets.py`? The shared runbook gives them to A; `judge.py` calls assets "C's stage" | C |
| The shared runbook still lists `rank` → `rankings.json` and `truth` → `claims.json`; the contracts use `judge_frames` → `frame_scores.json` and keep claims in `script.json` | Team |
