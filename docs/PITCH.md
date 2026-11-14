# DemoCut pitch · 3 minutes

**Owner: C.** Draft. Fill every `[__]` with a measured number before rehearsal (16:30).
Never say a number we didn't measure.

Slides: [DemoCut Pitch deck](https://claude.ai/artifact/8EDbTfrsKYdWJpFxU33WV7) (3 slides; the speaker notes repeat these lines).

## Before going on stage

- [ ] Laptop: board running (`uvicorn backend.app:app`), the cached run open at `http://127.0.0.1:8000/#<run_id>`
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
| 0:50–1:40 | How it works | **Live board**, the cached run (backup: slide 2) | "It watches the take and reads the repo. It ranks frames, hooks and title cards against the brief: here's the frame it picked, and the hook. Then it cuts the real footage: this strip shows it, green is real, the striped shots are generated. The footage is real; the explainer is generated from it, and it's marked.<br>And this is the part we care about: every claim in the voice-over needs proof in the footage, the README or the code. Here it wanted to say **[rejected claim]**. Nothing backs it, so it's gone. The video can't claim what the product can't do." |
| 1:40–2:00 | Proof | Slide 3 | "We showed **[__]** people our cut next to a video another team made by hand, 20 seconds each. **[__ of __]** picked ours. Repo URL to video: **[__ min]**." |
| 2:00–2:25 | Agents can call it | **Live Claude Code** | "And agents can call it." Type: *make a demo video of our repo from this take*. It calls `make_demo_video` and the cached run comes back at once. |
| 2:25–3:00 | Close | Slide 3, last line | "Every hackathon, every launch, every open-source release needs this video. Nobody should spend their last hours making it. Thank you." |

## Numbers to measure (runbook)

| Number | Where it comes from | Value |
|---|---|---|
| Repo URL → MP4, wall clock | First and last line of the run's `events.jsonl` | [__ min] |
| Human minutes | Stopwatch: recording + filling the board | [__ min] |
| fal cost per video | fal dashboard, one full-mode run | [$__] |
| Claims made / rejected | Board's truth check, or `script.json` | [__ / __] |
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
No. Higgsfield generates new pixels. DemoCut edits your real footage and won't let the video claim what the product can't do.

**"What if the take is bad?"**
It only needs the app to work on screen once. Dead time, mistakes and fumbling are cut: that's what the moments are for.

**"What does a run cost?"**
[$__] in full mode. A repeat run costs nothing: every answer is cached.
