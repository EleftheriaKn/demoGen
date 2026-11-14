"""Validates fixtures/run/ against backend/contracts.py, file by file and across files.

Run: bash fixtures/make_media.sh && pytest -q
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.contracts import (
    MODELS,
    STAGE_OUTPUTS,
    STAGES,
    Assets,
    Candidates,
    Decisions,
    Event,
    FrameScores,
    Moments,
    RepoFiles,
    RunConfig,
    Script,
    Summary,
    Timeline,
    Transcript,
    frame_time,
)

RUN = Path(__file__).resolve().parent.parent / "fixtures" / "run"
TOL = 0.1  # seconds

requires_ffprobe = pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")


def load(name: str):
    return MODELS[name].model_validate_json((RUN / name).read_text())


def ffprobe_duration(rel: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(RUN / rel)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


@pytest.fixture(scope="module")
def f():
    """All fixture models, by short name."""
    return {
        "run": load("run.json"),
        "repo": load("repo_files.json"),
        "transcript": load("transcript.json"),
        "summary": load("summary.json"),
        "moments": load("moments.json"),
        "candidates": load("candidates.json"),
        "frame_scores": load("frame_scores.json"),
        "script": load("script.json"),
        "decisions": load("decisions.json"),
        "assets": load("assets.json"),
        "timeline": load("timeline.json"),
    }


# --------------------------------------------------------------------------- #
# Per-file
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(MODELS))
def test_json_parses_with_model(name):
    load(name)


def test_events_jsonl_parses():
    lines = [l for l in (RUN / "events.jsonl").read_text().splitlines() if l.strip()]
    events = [Event.model_validate_json(l) for l in lines]
    run_id = load("run.json").run_id
    assert all(e.run_id == run_id for e in events)
    progress = [e.progress for e in events]
    assert progress == sorted(progress), "progress must be non-decreasing"
    assert progress[0] == 0.0 and progress[-1] == 1.0
    assert set(STAGES) <= {e.stage for e in events}, "every stage must emit at least one event"


def test_stage_maps():
    assert list(STAGE_OUTPUTS) == STAGES
    for name in MODELS:
        writers = [s for s, outs in STAGE_OUTPUTS.items() if name in outs]
        assert writers or name == "run.json", f"{name} is not produced by any stage"


@pytest.mark.parametrize("stage", STAGES)
def test_stage_outputs_exist(stage):
    for out in STAGE_OUTPUTS[stage]:
        p = RUN / out
        if out.endswith("/"):
            assert p.is_dir() and any(p.iterdir()), f"{stage}: {out} missing or empty"
        else:
            assert p.is_file(), f"{stage}: {out} missing"


# --------------------------------------------------------------------------- #
# Cross-file ids
# --------------------------------------------------------------------------- #


def test_ids_resolve(f):
    features = {x.feature_id for x in f["summary"].features}
    moments = {m.moment_id for m in f["moments"].moments}
    segments = {s.segment_id: s for s in f["candidates"].segments}
    shots = [s.shot_id for s in f["script"].shots]

    for m in f["moments"].moments:
        assert m.feature_id is None or m.feature_id in features, m
    for s in segments.values():
        assert s.moment_id is None or s.moment_id in moments, s

    for sc in f["frame_scores"].scores:
        assert sc.segment_id in segments and sc.frame in segments[sc.segment_id].frames, sc.frame
    scored = {sc.frame for sc in f["frame_scores"].scores}
    assert scored == {fr for s in segments.values() for fr in s.frames}, "every frame must be scored"

    for sh in f["script"].shots:
        v = sh.visual
        if v.kind == "footage":
            seg = segments[v.segment_id]
            assert seg.start - 1e-6 <= v.source_start < v.source_end <= seg.end + 1e-6, sh.shot_id
            kinds = {m.moment_id: m.kind for m in f["moments"].moments}
            assert kinds.get(seg.moment_id) != "dead", f"{sh.shot_id} uses a dead moment"

    # assets / timeline follow the script's shot order
    assert [v.shot_id for v in f["assets"].voice] == shots
    assert [s.shot_id for s in f["timeline"].shots] == shots
    card_shots = [s.shot_id for s in f["script"].shots if s.visual.kind == "card"]
    broll_shots = [s.shot_id for s in f["script"].shots if s.visual.kind == "broll"]
    assert [c.shot_id for c in f["assets"].cards] == card_shots
    assert [b.shot_id for b in f["assets"].broll] == broll_shots

    for d in f["decisions"].decisions:
        if d.kind == "frame_pick":
            assert d.subject in segments and set(d.options) == set(segments[d.subject].frames)
        elif d.kind == "hook_pick":
            assert d.chosen == f["script"].chosen_hook_id
            assert set(d.options) == {h.hook_id for h in f["script"].hooks}
        elif d.kind == "claim_check":
            assert d.subject in {c.claim_id for s in f["script"].shots for c in s.claims}
        elif d.kind == "take_pick":
            assert d.subject in shots
    if f["decisions"].recut:
        assert f["decisions"].recut.shot_id in shots


def test_hook_is_first_voice_line(f):
    s = f["script"]
    hook = next(h for h in s.hooks if h.hook_id == s.chosen_hook_id)
    assert s.shots[0].beat == "hook" and s.shots[0].voice_line == hook.text


def test_evidence_resolves(f):
    tree = set(f["repo"].tree)
    headings = {m.group(1).strip() for m in re.finditer(r"^#+\s+(.+)$", f["repo"].readme, re.M)}
    segments = {s.segment_id for s in f["candidates"].segments}

    def ok(ref: str) -> bool:
        if ref.startswith("README#"):
            return ref[len("README#"):] in headings
        return ref in tree

    for kf in f["repo"].key_files:
        assert kf.path in tree, kf.path
    for feat in f["summary"].features:
        for e in feat.evidence:
            assert ok(e), f"{feat.feature_id}: evidence {e!r} not found"
    for sh in f["script"].shots:
        for c in sh.claims:
            for e in c.evidence:
                if e.type == "footage":
                    assert e.ref in segments, e
                elif e.type == "readme":
                    assert e.ref.startswith("README#") and ok(e.ref), e
                else:
                    assert e.ref in tree, e
            if c.status == "backed":
                assert c.evidence, f"{c.claim_id} backed without evidence"
            if c.status in ("backed", "rewritten"):
                assert c.text.lower() in sh.voice_line.lower(), f"{c.claim_id} not in voice line"
            if c.status == "dropped":
                assert c.text.lower() not in sh.voice_line.lower(), f"dropped {c.claim_id} still spoken"


def test_decision_stages_and_kinds(f):
    d = f["decisions"]
    kinds = {x.kind for x in d.decisions}
    assert kinds == {"frame_pick", "hook_pick", "take_pick", "claim_check", "rubric_check"}
    # stages that write decisions.json are the ones that list it in STAGE_OUTPUTS
    for x in d.decisions:
        assert "decisions.json" in STAGE_OUTPUTS[x.stage], f"{x.decision_id}: {x.stage} doesn't own decisions.json"
    assert any(r.question == "Problem stated by 0:20?" for r in d.rubric)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #


@requires_ffprobe
def test_timestamps_within_recording(f):
    dur = ffprobe_duration(f["run"].recording)
    spans = [(w.start, w.end) for w in f["transcript"].words]
    spans += [(m.start, m.end) for m in f["moments"].moments]
    spans += [(s.start, s.end) for s in f["candidates"].segments]
    spans += [(frame_time(fr), frame_time(fr)) for s in f["candidates"].segments for fr in s.frames]
    spans += [
        (s.visual.source_start, s.visual.source_end) for s in f["script"].shots if s.visual.kind == "footage"
    ]
    spans += [
        (s.visual.source_start, s.visual.source_end)
        for s in f["timeline"].shots
        if s.visual.path == f["run"].recording
    ]
    for a, b in spans:
        assert 0 <= a <= b <= dur + 1e-6, f"span {a}-{b} outside recording (0-{dur})"


@requires_ffprobe
def test_voice_durations_match_files(f):
    for v in f["assets"].voice:
        assert abs(ffprobe_duration(v.path) - v.duration_s) <= TOL, v.path
    for b in f["assets"].broll:
        assert abs(ffprobe_duration(b.path) - b.duration_s) <= TOL, b.path
    if f["assets"].music:
        assert abs(ffprobe_duration(f["assets"].music.path) - f["assets"].music.duration_s) <= TOL


def test_timeline_consistent(f):
    tl, script = f["timeline"], f["script"]
    assert abs(sum(s.duration_s for s in tl.shots) - tl.duration_s) < 1e-3
    assert tl.duration_s <= f["run"].brief.time_limit_s
    voice = {v.shot_id: v for v in f["assets"].voice}
    cards = {c.shot_id: c.path for c in f["assets"].cards}
    by_id = {s.shot_id: s for s in script.shots}
    for s in tl.shots:
        sv = by_id[s.shot_id].visual
        assert s.voice_path == voice[s.shot_id].path
        # voice may overrun by mp3 padding only
        assert voice[s.shot_id].duration_s <= s.duration_s + TOL, f"{s.shot_id}: voice longer than shot"
        assert s.caption == by_id[s.shot_id].voice_line
        assert s.visual.kind == sv.kind
        if s.visual.kind == "card":
            assert s.visual.path == cards[s.shot_id]
        if s.visual.kind == "footage":
            assert s.visual.path == f["run"].recording
            assert (s.visual.source_start, s.visual.source_end) == (sv.source_start, sv.source_end)
            assert abs(s.visual.play_s() - s.duration_s) < 1e-3, f"{s.shot_id}: clip length != duration"
            assert (s.visual.zoom.box if s.visual.zoom else None) == sv.focus_box
    if tl.music and f["assets"].music:
        assert tl.music.path == f["assets"].music.path


@requires_ffprobe
def test_rendered_durations(f):
    tl = f["timeline"]
    for s in tl.shots:
        assert abs(ffprobe_duration(f"shots/{s.shot_id}.mp4") - s.duration_s) <= TOL, s.shot_id
    assert abs(ffprobe_duration("final.mp4") - tl.duration_s) <= TOL


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #


def _srt_t(t: str) -> float:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _ass_t(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_captions_match_timeline(f):
    tl = f["timeline"]
    blocks = [b for b in (RUN / "captions.srt").read_text().strip().split("\n\n") if b.strip()]
    assert len(blocks) == len(tl.shots)
    for b, s in zip(blocks, tl.shots):
        _, times, *text = b.splitlines()
        a, z = (_srt_t(x.strip()) for x in times.split("-->"))
        assert abs(a - s.start) < 0.01 and abs(z - (s.start + s.duration_s)) < 0.01
        assert " ".join(text) == s.caption

    dialogues = [l for l in (RUN / "captions.ass").read_text().splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == len(tl.shots)
    for line, s in zip(dialogues, tl.shots):
        parts = line[len("Dialogue:"):].split(",", 9)
        assert abs(_ass_t(parts[1]) - s.start) < 0.01
        assert abs(_ass_t(parts[2]) - (s.start + s.duration_s)) < 0.01
        assert parts[9] == s.caption


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def _paths(f) -> list[str]:
    p = [f["run"].recording, *f["repo"].screenshots]
    p += [fr for s in f["candidates"].segments for fr in s.frames]
    p += [sc.frame for sc in f["frame_scores"].scores]
    a = f["assets"]
    p += [v.path for v in a.voice] + [b.path for b in a.broll]
    p += [c.path for c in a.cards] + [d for c in a.cards for d in c.drafts]
    if a.music:
        p.append(a.music.path)
    tl = f["timeline"]
    p += [s.voice_path for s in tl.shots] + [s.visual.path for s in tl.shots]
    if tl.music:
        p.append(tl.music.path)
    for d in f["decisions"].decisions:  # take/frame picks choose between files
        if d.kind in ("frame_pick", "take_pick"):
            p += d.options
    return p


def test_every_referenced_path_exists(f):
    for p in _paths(f):
        assert not Path(p).is_absolute(), f"absolute path {p}"
        assert (RUN / p).is_file(), f"missing {p}"


def test_chosen_card_is_copy_of_chosen_draft(f):
    for c in f["assets"].cards:
        assert (RUN / c.path).read_bytes() == (RUN / c.drafts[c.chosen_index]).read_bytes()


def test_fixture_story_shape(f):
    """The spec for the fixture itself (keeps it a useful example)."""
    assert f["run"].brief.criteria == ["usefulness", "craft", "aura"]
    assert len(f["moments"].moments) == 4
    assert len(f["candidates"].segments) == 6
    assert len(list((RUN / "frames").glob("*.jpg"))) == 18
    s = f["script"]
    assert len(s.hooks) == 3 and len(s.shots) == 3
    assert [x.visual.kind for x in s.shots] == ["card", "footage", "footage"]
    assert s.shots[2].visual.focus_box is not None
    statuses = sorted(c.status for sh in s.shots for c in sh.claims)
    assert statuses == ["backed", "backed", "dropped", "rewritten"]
    assert len(f["decisions"].rubric) == 4 and f["decisions"].recut is None
    json.loads((RUN / "run.json").read_text())  # plain JSON, no comments
