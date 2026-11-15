"""DemoCut file contracts.

Every pipeline stage talks to the others ONLY through files in ``runs/<run_id>/``.
This module defines one Pydantic v2 model per file, plus the stage -> outputs map.

Conventions (apply to every model below):
- Times are seconds (float) unless the field name says otherwise (``*_ms``, ``ts``...).
- Bounding boxes are normalized ``[x, y, w, h]`` in 0-1 (origin top-left).
- Paths are relative to the run folder (``runs/<run_id>/``), never absolute.
- Evolution rule: fields may later be ADDED as optional. Never rename or remove one.

Owners: A = Gemini (understand, script, judge), B = ffmpeg (ingest, candidates,
timeline, captions, render), C = fal + app (transcribe, assets, orchestrator, API, web).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_serializer, model_validator

# --------------------------------------------------------------------------- #
# Shared types
# --------------------------------------------------------------------------- #

Stage = Literal[
    "ingest",
    "transcribe",
    "understand",
    "candidates",
    "judge_frames",
    "script",
    "assets",
    "timeline",
    "rubric",
    "captions",
    "render",
]
STAGES: list[str] = list(get_args(Stage))


def _check_box(v: list[float]) -> list[float]:
    x, y, w, h = v
    if not all(0.0 <= c <= 1.0 for c in v):
        raise ValueError(f"box values must be in 0-1, got {v}")
    if w <= 0 or h <= 0:
        raise ValueError(f"box width/height must be > 0, got {v}")
    if x + w > 1.0 + 1e-6 or y + h > 1.0 + 1e-6:
        raise ValueError(f"box must stay inside the frame (x+w<=1, y+h<=1), got {v}")
    return v


Box = Annotated[
    list[float],
    Field(min_length=4, max_length=4, description="Normalized [x, y, w, h] in 0-1."),
    AfterValidator(_check_box),
]
Score = Annotated[float, Field(ge=0.0, le=1.0)]
Seconds = Annotated[float, Field(ge=0.0)]


class Contract(BaseModel):
    """Base for every contract model: unknown keys are an error (catches typos)."""

    model_config = ConfigDict(extra="forbid")


def _check_span(start: float, end: float, what: str) -> None:
    if end <= start:
        raise ValueError(f"{what}: end ({end}) must be > start ({start})")


def _drop_none(data: dict, *keys: str) -> dict:
    """Leave optional fields added later out of the JSON while they are unset, so older files, fixtures and
    recorded prompts stay byte-for-byte the same."""
    for k in keys:
        if isinstance(data, dict) and data.get(k) is None:
            data.pop(k, None)
    return data


def _unique(ids: list[str], what: str) -> None:
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate {what}: {sorted(dupes)}")


# --------------------------------------------------------------------------- #
# run.json  (written by the API / C at run creation)
# --------------------------------------------------------------------------- #


class Brief(Contract):
    """The hackathon judging brief."""

    criteria: list[str] = Field(
        min_length=1, description="Judging criteria, verbatim from the brief, e.g. ['usefulness','craft']."
    )
    time_limit_s: int = Field(gt=0, description="Max length of the final video, in seconds.")
    language: str = Field("en", description="BCP-47 code of the main voice-over / caption language.")
    second_language: Optional[str] = Field(
        None, description="Optional BCP-47 code for a second caption/voice language (e.g. 'el'), or null."
    )


class RunConfig(Contract):
    """run.json: inputs and settings for one run."""

    run_id: str = Field(description="Run id; equals the run folder name under runs/.")
    repo_url: str = Field(description="GitHub URL of the app being demoed.")
    recording: str = Field(description="Path of the raw screen recording, e.g. 'recording.mp4'.")
    brief: Brief = Field(description="The judging brief.")
    mode: Literal["full", "cheap"] = Field(
        description="'full' = best models, more judge calls; 'cheap' = fewer/cheaper calls for iteration."
    )


# --------------------------------------------------------------------------- #
# repo_files.json  (B, ingest)
# --------------------------------------------------------------------------- #


class KeyFile(Contract):
    path: str = Field(description="Repo-relative path; must appear in RepoFiles.tree.")
    content: str = Field(description="File content (may be truncated by ingest).")


class RepoFiles(Contract):
    """repo_files.json: what ingest pulled from the GitHub repo."""

    repo_url: str = Field(description="Same as RunConfig.repo_url.")
    readme: str = Field(description="Full README markdown. Sections are referenced as 'README#<heading>'.")
    tree: list[str] = Field(description="Repo-relative paths of every file in the repo (files only).")
    key_files: list[KeyFile] = Field(
        max_length=20, description="Up to 20 most informative source files with their content."
    )
    screenshots: list[str] = Field(
        description="Images found in the repo, downloaded into the run folder (paths like 'screenshots/x.png')."
    )


# --------------------------------------------------------------------------- #
# transcript.json  (C, transcribe)
# --------------------------------------------------------------------------- #


class Word(Contract):
    word: str = Field(description="One word as spoken (punctuation allowed).")
    start: Seconds = Field(description="Start time in the recording.")
    end: Seconds = Field(description="End time in the recording.")

    @model_validator(mode="after")
    def _span(self) -> "Word":
        _check_span(self.start, self.end, f"word {self.word!r}")
        return self


class Transcript(Contract):
    """transcript.json: speech-to-text of the recording's own audio (audio.wav)."""

    language: str = Field(description="Detected BCP-47 language code.")
    text: str = Field(description="Full transcript text.")
    words: list[Word] = Field(description="Word-level timestamps, in order.")


# --------------------------------------------------------------------------- #
# summary.json  (A, understand)
# --------------------------------------------------------------------------- #


class Feature(Contract):
    feature_id: str = Field(description="Stable id, e.g. 'f1'. Referenced by moments.")
    name: str = Field(description="Short feature name.")
    description: str = Field(description="One or two sentences on what it does for the user.")
    evidence: list[str] = Field(
        description="Where the repo shows it: 'README#<heading>' or a repo path from RepoFiles.tree."
    )


class Summary(Contract):
    """summary.json: what the app is, from README + code + transcript."""

    name: str = Field(description="App name.")
    one_liner: str = Field(description="One-sentence pitch.")
    problem: str = Field(description="The problem the app solves, in one or two sentences.")
    audience: str = Field(description="Who it is for.")
    features: list[Feature] = Field(description="Features, most important first.")

    @model_validator(mode="after")
    def _ids(self) -> "Summary":
        _unique([f.feature_id for f in self.features], "feature_id")
        return self


# --------------------------------------------------------------------------- #
# moments.json  (A, understand)
# --------------------------------------------------------------------------- #


class Moment(Contract):
    moment_id: str = Field(description="Stable id, e.g. 'm1'.")
    start: Seconds = Field(description="Start time in the recording.")
    end: Seconds = Field(description="End time in the recording.")
    description: str = Field(description="What happens on screen.")
    feature_id: Optional[str] = Field(description="Feature shown (Summary.features), or null.")
    kind: Literal["result", "action", "setup", "dead"] = Field(
        description="result = payoff on screen; action = user doing something; setup = navigation/loading; "
        "dead = nothing useful (never cut in)."
    )
    confidence: Score = Field(description="Model confidence 0-1.")

    @model_validator(mode="after")
    def _span(self) -> "Moment":
        _check_span(self.start, self.end, f"moment {self.moment_id}")
        return self


class Moments(Contract):
    """moments.json: interesting (and dead) stretches of the recording."""

    moments: list[Moment] = Field(description="Moments in time order.")

    @model_validator(mode="after")
    def _ids(self) -> "Moments":
        _unique([m.moment_id for m in self.moments], "moment_id")
        return self


# --------------------------------------------------------------------------- #
# candidates.json  (B, candidates)
# --------------------------------------------------------------------------- #


class Segment(Contract):
    segment_id: str = Field(description="Stable id, e.g. 'c01'.")
    start: Seconds = Field(description="Start time in the recording.")
    end: Seconds = Field(description="End time in the recording.")
    source: Literal["scene", "moment"] = Field(
        description="'scene' = found by scene detection; 'moment' = cut from a moments.json entry."
    )
    moment_id: Optional[str] = Field(description="The moment it came from (required when source='moment').")
    frames: list[str] = Field(
        min_length=3,
        max_length=3,
        description="3 sample frames, named 'frames/<segment_id>_<t>s.jpg' where <t> is the "
        "recording time in seconds (e.g. 'frames/c02_6.25s.jpg'). 640px wide JPEG.",
    )

    @model_validator(mode="after")
    def _check(self) -> "Segment":
        _check_span(self.start, self.end, f"segment {self.segment_id}")
        if self.source == "moment" and self.moment_id is None:
            raise ValueError(f"segment {self.segment_id}: source='moment' needs moment_id")
        for f in self.frames:
            t = frame_time(f)
            if not (self.start <= t <= self.end):
                raise ValueError(f"segment {self.segment_id}: frame {f} at {t}s is outside the segment")
        return self


def frame_time(frame_path: str) -> float:
    """Recording time encoded in a frame filename: 'frames/c02_6.25s.jpg' -> 6.25."""
    stem = frame_path.rsplit("/", 1)[-1]
    if not stem.endswith("s.jpg") or "_" not in stem:
        raise ValueError(f"frame name must look like 'frames/<segment_id>_<t>s.jpg', got {frame_path!r}")
    return float(stem[: -len("s.jpg")].rsplit("_", 1)[1])


class Candidates(Contract):
    """candidates.json: every clip the judge may pick from."""

    segments: list[Segment] = Field(description="Candidate segments in time order.")

    @model_validator(mode="after")
    def _ids(self) -> "Candidates":
        _unique([s.segment_id for s in self.segments], "segment_id")
        return self


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #


class JudgeAnswer(Contract):
    """One judge question answered for one subject."""

    question: str = Field(description="The question asked, verbatim.")
    score: Score = Field(description="0 = no, 1 = yes / best.")
    reason: str = Field(description="One-line justification.")


class FrameScore(Contract):
    segment_id: str = Field(description="Segment the frame belongs to (candidates.json).")
    frame: str = Field(description="Frame path, one of that segment's frames.")
    answers: list[JudgeAnswer] = Field(description="One answer per FrameScores.questions, same order.")
    heuristic_score: Optional[Score] = Field(
        description="Non-LLM score (sharpness, motion, text density...) 0-1, or null if not computed."
    )
    total: Score = Field(description="Combined score used for ranking, 0-1.")


class FrameScores(Contract):
    """frame_scores.json: judge scores for every candidate frame."""

    questions: list[str] = Field(description="Questions asked of every frame (derived from brief.criteria).")
    scores: list[FrameScore] = Field(description="One entry per scored frame.")

    @model_validator(mode="after")
    def _answers(self) -> "FrameScores":
        for s in self.scores:
            if [a.question for a in s.answers] != self.questions:
                raise ValueError(f"{s.frame}: answers must follow FrameScores.questions in order")
        return self


# --------------------------------------------------------------------------- #
# script.json  (A, script)
# --------------------------------------------------------------------------- #

VisualKind = Literal["footage", "card", "broll"]


class Hook(Contract):
    hook_id: str = Field(description="Stable id, e.g. 'h1'.")
    text: str = Field(description="Opening line candidate.")
    score: Score = Field(description="Judge score 0-1.")


class ShotVisual(Contract):
    kind: VisualKind = Field(
        description="footage = clip of the recording; card = generated title card; broll = generated video."
    )
    segment_id: Optional[str] = Field(None, description="Candidate segment used (footage only).")
    source_start: Optional[Seconds] = Field(None, description="Clip start in the recording (footage only).")
    source_end: Optional[Seconds] = Field(None, description="Clip end in the recording (footage only).")
    card_prompt: Optional[str] = Field(None, description="Prompt for the card/b-roll generator (card/broll).")
    focus_box: Optional[Box] = Field(None, description="Region to zoom into (footage only), or null.")

    @model_validator(mode="after")
    def _check(self) -> "ShotVisual":
        if self.kind == "footage":
            if self.segment_id is None or self.source_start is None or self.source_end is None:
                raise ValueError("footage visual needs segment_id, source_start and source_end")
            _check_span(self.source_start, self.source_end, "footage visual")
        elif self.card_prompt is None:
            raise ValueError(f"{self.kind} visual needs card_prompt")
        return self


class Evidence(Contract):
    type: Literal["footage", "readme", "file"] = Field(description="What kind of proof.")
    ref: str = Field(
        description="footage: a segment_id; readme: 'README#<heading>'; file: a repo path from RepoFiles.tree."
    )


class Claim(Contract):
    claim_id: str = Field(description="Stable id, e.g. 'cl1'.")
    text: str = Field(description="The factual claim (as it now reads in the voice line, unless dropped).")
    evidence: list[Evidence] = Field(description="Proof found for it (empty if none).")
    status: Literal["pending", "backed", "rewritten", "dropped"] = Field(
        description="pending = not checked; backed = evidence found; rewritten = softened to match evidence; "
        "dropped = removed from the voice line."
    )


class Shot(Contract):
    shot_id: str = Field(description="Stable id, e.g. 's01'. Used by assets, timeline, captions.")
    beat: Literal["hook", "problem", "demo", "how", "impact", "ask"] = Field(description="Story beat.")
    voice_line: str = Field(description="Exactly what the voice-over says for this shot.")
    visual: ShotVisual = Field(description="What is on screen.")
    claims: list[Claim] = Field(description="Factual claims made in voice_line, with truth-check status.")
    generated: bool = Field(description="True if the visual is generated (card/broll), not real footage.")
    callout: Optional[str] = Field(
        None, description="On-screen callout, at most 5 words (a benefit or an on-screen fact); a claim, so it is "
        "truth-checked like the voice line. Optional (v2, B15)."
    )

    @model_serializer(mode="wrap")
    def _no_empty_callout(self, handler):
        return _drop_none(handler(self), "callout")


class Script(Contract):
    """script.json: the cut, shot by shot."""

    language: str = Field(description="BCP-47 language of the voice lines.")
    hooks: list[Hook] = Field(min_length=1, description="Candidate opening hooks.")
    chosen_hook_id: str = Field(description="hook_id of the hook used in the first shot.")
    shots: list[Shot] = Field(min_length=1, description="Shots in playback order.")

    @model_validator(mode="after")
    def _check(self) -> "Script":
        _unique([h.hook_id for h in self.hooks], "hook_id")
        _unique([s.shot_id for s in self.shots], "shot_id")
        _unique([c.claim_id for s in self.shots for c in s.claims], "claim_id")
        if self.chosen_hook_id not in {h.hook_id for h in self.hooks}:
            raise ValueError(f"chosen_hook_id {self.chosen_hook_id!r} is not in hooks")
        return self


# --------------------------------------------------------------------------- #
# decisions.json  (A; appended to by judge_frames, script, assets, rubric)
# --------------------------------------------------------------------------- #


class Decision(Contract):
    decision_id: str = Field(description="Stable id, e.g. 'd01'.")
    stage: Stage = Field(description="Stage that made the decision.")
    kind: Literal["frame_pick", "hook_pick", "take_pick", "claim_check", "rubric_check"] = Field(
        description="What was decided."
    )
    subject: str = Field(description="Id of the thing decided about (segment_id, shot_id, claim_id, 'final'...).")
    options: list[str] = Field(min_length=1, description="What the judge chose between (ids, paths or verbs).")
    chosen: str = Field(description="The option picked; one of options.")
    answers: list[JudgeAnswer] = Field(description="Judge answers behind the pick.")
    heuristic_score: Optional[Score] = Field(None, description="Non-LLM score of the chosen option, or null.")
    judge_score: Score = Field(description="Judge's overall score of the chosen option, 0-1.")
    reason: str = Field(description="One-line human-readable reason (shown in the UI).")

    @model_validator(mode="after")
    def _chosen(self) -> "Decision":
        if self.chosen not in self.options:
            raise ValueError(f"decision {self.decision_id}: chosen {self.chosen!r} not in options")
        return self


class RubricItem(Contract):
    question: str = Field(description="Rubric question asked of the final cut, e.g. 'Problem stated by 0:20?'.")
    passed: bool = Field(description="Whether the cut passes.")
    reason: str = Field(description="One-line justification.")


class Recut(Contract):
    shot_id: str = Field(description="Shot to redo.")
    reason: str = Field(description="Why it must be redone.")


class Decisions(Contract):
    """decisions.json: audit log of every judge decision + final rubric."""

    decisions: list[Decision] = Field(description="All decisions, in the order they were made.")
    rubric: list[RubricItem] = Field(description="Final-cut rubric (written by the rubric stage; [] before).")
    recut: Optional[Recut] = Field(None, description="Shot the rubric wants redone, or null if the cut passes.")

    @model_validator(mode="after")
    def _ids(self) -> "Decisions":
        _unique([d.decision_id for d in self.decisions], "decision_id")
        return self


# --------------------------------------------------------------------------- #
# assets.json  (C, assets)
# --------------------------------------------------------------------------- #


class VoiceAsset(Contract):
    shot_id: str = Field(description="Shot this voice line belongs to.")
    path: str = Field(description="Audio file, e.g. 'voice/s01.mp3'.")
    duration_s: float = Field(gt=0, description="Exact audio duration (ffprobe). B uses this; never re-probes.")
    language: str = Field(description="BCP-47 language of the take.")


class MusicAsset(Contract):
    path: str = Field(description="Music bed file, e.g. 'music.wav'.")
    duration_s: float = Field(gt=0, description="Exact duration.")


class CardAsset(Contract):
    shot_id: str = Field(description="Shot this card belongs to.")
    path: str = Field(description="Chosen card image, e.g. 'cards/s01.png' (a copy of the chosen draft).")
    drafts: list[str] = Field(min_length=1, description="All generated drafts, e.g. 'cards/drafts/s01_0.png'.")
    chosen_index: int = Field(ge=0, description="Index into drafts of the chosen one.")

    @model_validator(mode="after")
    def _idx(self) -> "CardAsset":
        if self.chosen_index >= len(self.drafts):
            raise ValueError(f"card {self.shot_id}: chosen_index out of range")
        return self


class BrollAsset(Contract):
    shot_id: str = Field(description="Shot this b-roll belongs to.")
    path: str = Field(description="Video file, e.g. 'broll/s04.mp4'.")
    duration_s: float = Field(gt=0, description="Exact duration.")


class Assets(Contract):
    """assets.json: generated media. B's timeline reads durations from here (never ffprobes C's files)."""

    voice: list[VoiceAsset] = Field(description="One voice take per shot (per language).")
    music: Optional[MusicAsset] = Field(None, description="Music bed, or null for none.")
    cards: list[CardAsset] = Field(description="One entry per card shot.")
    broll: list[BrollAsset] = Field(description="One entry per broll shot.")


# --------------------------------------------------------------------------- #
# timeline.json  (B, timeline)
# --------------------------------------------------------------------------- #


class Zoom(Contract):
    box: Box = Field(description="Region to zoom into.")
    ease_s: Seconds = Field(description="Seconds to ease from full frame into the box.")


class TimelineVisual(Contract):
    kind: VisualKind = Field(description="Same meaning as in script.json.")
    path: str = Field(description="Source file: 'recording.mp4', a card png or a broll mp4.")
    source_start: Optional[Seconds] = Field(None, description="Clip start in the source (footage/broll).")
    source_end: Optional[Seconds] = Field(None, description="Clip end in the source (footage/broll).")
    speed: float = Field(1.0, gt=0, le=1.5, description="Playback speed, max 1.5.")
    freeze_s: Seconds = Field(0.0, description="Seconds to hold the last frame after the clip.")
    zoom: Optional[Zoom] = Field(None, description="Zoom-in, or null.")

    @model_validator(mode="after")
    def _check(self) -> "TimelineVisual":
        if self.kind == "footage" and (self.source_start is None or self.source_end is None):
            raise ValueError("footage needs source_start and source_end")
        if self.source_start is not None and self.source_end is not None:
            _check_span(self.source_start, self.source_end, "timeline visual")
        return self

    def play_s(self) -> Optional[float]:
        """On-screen seconds the clip covers: (end-start)/speed + freeze_s. None for stills."""
        if self.source_start is None or self.source_end is None:
            return None
        return (self.source_end - self.source_start) / self.speed + self.freeze_s


class TimelineShot(Contract):
    shot_id: str = Field(description="Shot id from script.json.")
    start: Seconds = Field(description="Start in the final video.")
    duration_s: float = Field(gt=0, description="Length in the final video.")
    voice_path: str = Field(description="Voice file for this shot (from assets.json).")
    visual: TimelineVisual = Field(description="What to render.")
    caption: str = Field(description="Caption text (normally the voice line).")
    callout: Optional[str] = Field(None, description="Callout from script.json drawn on screen, or null. Optional (B15).")

    @model_serializer(mode="wrap")
    def _no_empty_callout(self, handler):
        return _drop_none(handler(self), "callout")


class TimelineMusic(Contract):
    path: str = Field(description="Music file.")
    gain_db: float = Field(description="Gain applied to the music, in dB (negative = quieter).")
    duck: bool = Field(description="Duck music under the voice.")


class Timeline(Contract):
    """timeline.json: the exact edit render executes. Shots are back to back."""

    fps: int = Field(gt=0, description="Output frame rate.")
    width: int = Field(gt=0, description="Output width in px.")
    height: int = Field(gt=0, description="Output height in px.")
    duration_s: float = Field(gt=0, description="Total length = sum of shot durations.")
    shots: list[TimelineShot] = Field(min_length=1, description="Shots in playback order.")
    music: Optional[TimelineMusic] = Field(None, description="Music bed, or null.")

    @model_validator(mode="after")
    def _check(self) -> "Timeline":
        _unique([s.shot_id for s in self.shots], "shot_id")
        t = 0.0
        for s in self.shots:
            if abs(s.start - t) > 1e-3:
                raise ValueError(f"shot {s.shot_id} starts at {s.start}, expected {t:.3f} (no gaps/overlaps)")
            t += s.duration_s
        if abs(t - self.duration_s) > 1e-3:
            raise ValueError(f"shot durations sum to {t:.3f}, duration_s is {self.duration_s}")
        return self


# --------------------------------------------------------------------------- #
# events.jsonl  (C, orchestrator) - also the SSE payload
# --------------------------------------------------------------------------- #


def _check_iso(v: str) -> str:
    datetime.fromisoformat(v)
    return v


class Event(Contract):
    """One progress event. One per line in events.jsonl; same JSON is sent over SSE."""

    run_id: str = Field(description="Run id.")
    stage: Literal[Stage, "run"] = Field(description="Stage name, or 'run' for run-level events (start/done/error).")
    msg: str = Field(description="Human-readable status line.")
    progress: Score = Field(description="Overall run progress 0-1 (non-decreasing).")
    ts: Annotated[str, AfterValidator(_check_iso)] = Field(description="ISO-8601 timestamp with timezone.")
    data: dict = Field(default_factory=dict, description="Optional extra payload (e.g. a decision, a file path).")


# --------------------------------------------------------------------------- #
# Maps
# --------------------------------------------------------------------------- #

# Files/dirs (trailing '/') each stage writes. Fake mode copies these from fixtures/run/.
# decisions.json is shared: each judging stage reads it, appends, and writes it back.
STAGE_OUTPUTS: dict[str, list[str]] = {
    "ingest": ["repo_files.json", "audio.wav", "screenshots/"],
    "transcribe": ["transcript.json"],
    "understand": ["summary.json", "moments.json"],
    "candidates": ["candidates.json", "frames/"],
    "judge_frames": ["frame_scores.json", "decisions.json"],
    "script": ["script.json", "decisions.json"],
    "assets": ["assets.json", "voice/", "music.wav", "cards/", "decisions.json"],
    "timeline": ["timeline.json"],
    "rubric": ["decisions.json"],
    "captions": ["captions.srt", "captions.ass"],
    "render": ["shots/", "final.mp4"],
}
assert list(STAGE_OUTPUTS) == STAGES

# JSON file -> model. events.jsonl is validated line by line with Event.
MODELS: dict[str, type[BaseModel]] = {
    "run.json": RunConfig,
    "repo_files.json": RepoFiles,
    "transcript.json": Transcript,
    "summary.json": Summary,
    "moments.json": Moments,
    "candidates.json": Candidates,
    "frame_scores.json": FrameScores,
    "script.json": Script,
    "decisions.json": Decisions,
    "assets.json": Assets,
    "timeline.json": Timeline,
}
