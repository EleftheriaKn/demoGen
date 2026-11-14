# DemoCut

DemoCut turns a sloppy screen recording of a hackathon app, plus its GitHub repo, into a short judged demo video that fits the hackathon's brief.

## Inputs

**Run**:
One attempt at making a video from one recording, repo and brief. Everything it produces lives in its run folder.

**Recording**:
The raw screen capture the team uploads. Every time in every run file is a time in the recording, unless it says "in the final video".
_Avoid_: footage file, input video

**Brief**:
The hackathon's judging rules for this run: criteria, time limit, languages.

**Criterion**:
One word from the brief that judges score on, such as `usefulness`, `craft`, `aura`.
_Avoid_: metric, category

## Understanding the app

**Feature**:
One thing the app does for its user, backed by a README section or a repo file.

**Moment**:
A stretch of the recording with one meaning: `setup`, `action`, `result` or `dead`. Moments are found by watching the recording, not by cutting it.
_Avoid_: scene, clip, highlight

**Segment**:
A candidate stretch of the recording that may be cut into the video, found by scene detection or taken from a moment.
_Avoid_: candidate clip, moment (a moment is meaning; a segment is a cuttable span)

## The script

**Shot**:
One unit of the final video: one voice line over one visual, serving one beat.
_Avoid_: scene, clip, segment

**Beat**:
The story role a shot plays: hook, problem, demo, how, impact, ask.

**Hook**:
A candidate opening line. Several are written; one is chosen.

**Claim**:
A factual statement a voice line makes about the app.

**Evidence**:
Proof for a claim: a segment of footage, a README section, or a repo file. A reference that doesn't exist is not evidence.

**Truth check**:
A separate pass after drafting in which each claim is judged against its evidence alone, then kept, rewritten to match the evidence, or dropped from the voice line.
_Avoid_: fact check, self-check

## Judging

**Judge question**:
A fixed, hand-written yes/no question that turns one criterion into something scorable. The same questions are asked every run; an unplanned criterion gets a generic question.
_Avoid_: prompt, rubric (the rubric is about the whole cut)

**Decision**:
One recorded keep/cut choice by the judge: what was chosen between, what won, the scores and a one-line reason.

**Take**:
One of several generated versions of the same asset (a card draft, a voice take). The judge picks the best take.
_Avoid_: draft (except for cards), variant

**Rubric**:
The checklist the finished cut is judged against, such as "problem stated by 0:20".

**Recut**:
A request to redo exactly one shot, made when a rubric item fails because of that shot. A failure of the whole video (e.g. too long) is recorded but never triggers a recut. A run has at most one recut.
