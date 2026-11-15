# DemoCut pitch · 3 minutes

**Owner: C.** Draft, **v2** (C16): the repo tells the flow, real footage first, labelled illustrations for the
weak or missing steps, every claim checked. Fill every `[__]` with a measured number before rehearsal (16:30).
Never say a number we didn't measure, and never describe a feature that didn't ship (see *Say only if shipped*).

Slides: [DemoCut Pitch deck](https://claude.ai/artifact/8EDbTfrsKYdWJpFxU33WV7) (3 slides; the speaker notes repeat these lines).

## Before going on stage

- [ ] Laptop: board running (`python -m backend.serve`), the cached run open at `http://localhost:8000/#<run_id>`
- [ ] That run has at least one **illustration** shot and one **rejected claim** (check The cut and Truth check)
- [ ] Claude Code open, `democut` MCP tool added, the same repo, take and brief ready so `make_demo_video` returns **cached**
- [ ] Our submission video loaded, full screen, sound on
- [ ] Slides open on slide 1, in a second window
- [ ] `.cache/` NOT cleared

## Script

| Time | Beat | On screen | Say |
|---|---|---|---|
| 0:00–0:15 | Our video | Submission video, first 15 s, full screen | Nothing. Let it play. |
| 0:15–0:30 | The pain | Slide 1 (title hidden behind the video until now) | "Every team here spent its last hours on this video. Cutting the take, writing a voice-over, making title cards, fighting the export." |
| 0:30–0:50 | Reveal | Slide 1: *Nobody edited it.* + three numbers | "Nobody edited the video you just watched. We gave DemoCut one rough take, our repo URL and the judging brief. It took **[__ min]** and cost **[$__]**. Our human time: **[__ min]**, pressing one button." |
| 0:50–1:40 | How it works | **Live board**, the cached run (backup: slide 2) | "It starts from the repo: it reads the README and the code and writes the flow a user goes through, step by step. Then it watches the take and matches it to that flow. **Real footage first**: every step the take shows well stays real, zoomed to the action. Where the take is weak or a step is missing, it fills the gap with an illustration built from your repo and your real screens, and labels it. This strip shows it: green is real footage, striped is illustration.<br>And this is the part we care about: **every claim is still checked**. Footage claims against the footage, illustration claims against the README and the code. Here it wanted to say **[rejected claim]**. Nothing backs it, so it's gone. The video can't claim what the product can't do." |
| 1:40–2:00 | Proof | Slide 3 | "We showed **[__]** people our cut next to a video another team made by hand, 20 seconds each. **[__ of __]** picked ours. Repo URL to video: **[__ min]**." |
| 2:00–2:25 | Agents can call it | **Live Claude Code** | "And agents can call it." Type: *make a demo video of our repo from this take*. It calls `make_demo_video` and the cached run comes back at once. |
| 2:25–3:00 | Close | Slide 3, last line | "Every hackathon, every launch, every open-source release needs this video. Nobody should spend their last hours making it. Thank you." |

## The one-line version (v2)

> Real footage first; weak or missing steps are filled with labelled illustrations built from your repo, and every claim is still checked.

Use it in the How-it-works beat, on slide 2, and when a judge asks "what does it do?" in the hallway.

## Say only if shipped

Check `main` on the morning of the pitch. Drop a line if its ticket isn't done.

| Line | Needs |
|---|---|
| "…an illustration built from your repo and your real screens…" | A14 + A16 (generated panels) and B15 (comic look + "illustration" tag) |
| "…or a short animation…" | A17 + B16 (if time) |
| "…matches the take to the flow, step by step…" | A12 + A13 |
| "No recording? Repo and brief alone give you a generated launch video; the take is the upgrade." | A20 + C17 (recording optional) |
| "At least half of every video is real footage." | A18 (rubric checks ≥ 50%) |

If A14/A16 didn't ship, fall back to the v1 line: "It cuts your real footage, and every claim needs proof."

## Numbers to measure (runbook)

All but fal cost, human minutes and the 20-second test come from one command on a real run:
`python -m backend.stats runs/<run_id>` (it refuses to pass off a fake or resumed run as real).

| Number | Where it comes from | Value |
|---|---|---|
| Repo URL → MP4, wall clock | First and last line of the run's `events.jsonl` | [__ min] |
| Human minutes | Stopwatch: recording + filling the board | [__ min] |
| fal cost per video | fal dashboard, one full-mode run | [$__] |
| Claims made / rejected | Board's truth check, or `script.json` | [__ / __] |
| Real footage share | Board's The cut, or `backend.stats` | [__ %] |
| 20-second test | C8: 5–10 people, ours vs a hand-made video | [__ of __] |

## If something breaks on stage

| Problem | Do this |
|---|---|
| Board won't load | Slide 2 instead of the live board; same words |
| Claude Code call is slow or fails | Say "it returns the cached run" and show the run on the board |
| Video has no sound | Keep going; the captions carry it |

## Questions to rehearse

**"How is this different from Proof & Demo?"**
It's hands-off from one take, an agent can call it, and fal generates options that the judge chooses between.

**"Isn't this just Higgsfield?"**
No. Higgsfield generates new pixels. DemoCut edits your real footage and won't let the video claim what the product can't do. Our few generated shots are built from your real frames, labelled as illustrations, and every fact on them is checked against your repo.

**"Lots of tools already make demo videos. Why yours?"**
Each takes one input. Screen Studio and Clueso polish a recording; RepoClip makes a video from the repo alone, with no real footage and no truth check. DemoCut takes all three: the repo (what's true), the take (proof it works) and the brief (what these judges score), and checks every claim.

**"So part of the video is fake?"**
No: it's labelled. Real footage covers every step the take shows well; illustrations only fill what the take shows badly or not at all, they carry an "illustration" tag, and their claims must be backed by the README or the code. The strip on the board shows exactly which seconds are real.

**"What if the take is bad?"**
It only needs the app to work on screen once. Dead time, mistakes and fumbling are cut, and a step the take shows badly is redrawn as a labelled illustration from that same frame.

**"What does a run cost?"**
[$__] in full mode. A repeat run costs nothing: every answer is cached.
