"""Stage script: draft with hooks (ticket A4)."""

from __future__ import annotations

import shutil

import pytest

from backend import events
from backend.contracts import Candidates, Decisions, RepoFiles, Script
from backend.pipeline import script
from backend.run import FIXTURE_RUN, load_run

SEGMENTS = Candidates.model_validate_json((FIXTURE_RUN / "candidates.json").read_text()).segments
REPO = RepoFiles.model_validate_json((FIXTURE_RUN / "repo_files.json").read_text(encoding="utf-8"))
BEATS = ["hook", "problem", "demo", "demo", "how", "impact", "ask"]


def _footage(seg: str, start: float, end: float, evidence=()) -> script.FootageDraft:
    claims = [script.ClaimDraft(text="it works", evidence=list(evidence))] if evidence else []
    return script.FootageDraft(voice_line="line", segment_id=seg, source_start=start, source_end=end, claims=claims)


def _draft(**over) -> script.Draft:
    base = dict(
        hooks=["a", "b", "c"],
        hook_card_prompt="title",
        problem=_footage("c02", 3.0, 9.0),  # starts before c02 (5-10)
        demos=[
            _footage("c04", 12.0, 18.0, [{"type": "footage", "ref": "c04"}, {"type": "file", "ref": "ghost.py"}]),
            _footage("c03", 30.0, 40.0),  # wholly outside c03 (10-12)
        ],
        how=_footage("c04", 13.0, 14.0, [{"type": "readme", "ref": "README#How it works"}]),
        impact=_footage("c02", 6.0, 7.0),
        ask=script.CardDraft(voice_line="Try it.", card_prompt="end card", claims=[]),
    )
    return script.Draft(**{**base, **over})


def test_build_shots_order_visuals_spans_and_pending_claims():
    shots = script.build_shots(_draft(), "Hook line", SEGMENTS, REPO)
    assert [s.beat for s in shots] == BEATS
    assert [s.shot_id for s in shots] == [f"s{i:02d}" for i in range(1, 8)]
    assert shots[0].voice_line == "Hook line"
    for s in shots:
        assert s.generated == (s.beat in ("hook", "ask")) == (s.visual.kind == "card")
        if s.visual.kind == "footage":
            seg = next(g for g in SEGMENTS if g.segment_id == s.visual.segment_id)
            assert seg.start <= s.visual.source_start < s.visual.source_end <= seg.end
    assert (shots[1].visual.source_start, shots[1].visual.source_end) == (5.0, 9.0)  # clamped
    assert (shots[3].visual.source_start, shots[3].visual.source_end) == (10.0, 12.0)  # whole segment
    claims = [c for s in shots for c in s.claims]
    assert [c.claim_id for c in claims] == ["cl1", "cl2"]
    assert {c.status for c in claims} == {"pending"}
    assert [e.ref for e in claims[0].evidence] == ["c04"]  # ghost.py is not in the repo


def test_build_shots_rejects_unknown_segment():
    with pytest.raises(ValueError, match="c99"):
        script.build_shots(_draft(impact=_footage("c99", 1.0, 2.0)), "x", SEGMENTS, REPO)


def test_draft_needs_three_hooks_and_two_to_four_demos():
    with pytest.raises(ValueError):
        _draft(hooks=["a", "b"])
    with pytest.raises(ValueError):
        _draft(demos=[_footage("c04", 12.0, 18.0)])


def test_judge_hooks_picks_highest_mean():
    verdicts = script.HookJudgement(verdicts=[
        script.HookVerdict(hook_id=h, answers=[script.Answer(score=a, reason="r"), script.Answer(score=b, reason="r")])
        for h, a, b in [("h1", 0.4, 0.4), ("h2", 0.9, 0.8), ("h3", 0.9, 0.5)]
    ])
    hooks, best = script.score_hooks(["a", "b", "c"], verdicts)
    assert [h.score for h in hooks] == [0.4, 0.85, 0.7]
    assert best.hook_id == "h2"
    with pytest.raises(ValueError, match="h3"):
        script.score_hooks(["a", "b", "c"], verdicts.model_copy(update={"verdicts": verdicts.verdicts[:2]}))


def test_script_stage_writes_draft_and_hook_pick(tmp_path):
    for name in ("run.json", "repo_files.json", "summary.json", "moments.json", "candidates.json",
                 "frame_scores.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    old = Decisions.model_validate_json((FIXTURE_RUN / "decisions.json").read_text())
    frame_picks = [d for d in old.decisions if d.stage == "judge_frames"]
    # the fixture log also holds old script decisions; the stage must replace them
    keep = [d for d in old.decisions if d.stage in ("judge_frames", "script")]
    (tmp_path / "decisions.json").write_text(old.model_copy(update={"decisions": keep}).model_dump_json())
    cfg = load_run(tmp_path)
    events.bind(tmp_path, cfg.run_id)
    script.run(tmp_path, cfg)
    script.run(tmp_path, cfg)  # re-run replaces this stage's decisions instead of piling up

    out = Script.model_validate_json((tmp_path / "script.json").read_text(encoding="utf-8"))
    beats = [s.beat for s in out.shots]
    assert beats[:2] == ["hook", "problem"] and beats[-3:] == ["how", "impact", "ask"]
    assert 2 <= beats.count("demo") <= 4 and len(beats) == 5 + beats.count("demo")
    assert len(out.hooks) == 3
    chosen = next(h for h in out.hooks if h.hook_id == out.chosen_hook_id)
    assert out.shots[0].voice_line == chosen.text
    assert all(c.status == "pending" for s in out.shots for c in s.claims)

    log = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    picks = [d for d in log.decisions if d.kind == "hook_pick"]
    assert len(picks) == 1 and picks[0].chosen == out.chosen_hook_id
    assert [d for d in log.decisions if d.stage == "judge_frames"] == frame_picks
    assert not [d for d in log.decisions if d.stage not in ("judge_frames", "script")]
