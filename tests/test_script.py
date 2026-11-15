"""Stage script: draft with hooks (ticket A4) and the truth check (ticket A5)."""

from __future__ import annotations

import shutil

import pytest

from backend import events
from backend.clients import gemini
from backend.contracts import Candidates, Claim, Decisions, Evidence, RepoFiles, Script, Shot, ShotVisual
from backend.pipeline import script
from backend.run import FIXTURE_RUN, load_run

SEGMENTS = Candidates.model_validate_json((FIXTURE_RUN / "candidates.json").read_text()).segments
REPO = RepoFiles.model_validate_json((FIXTURE_RUN / "repo_files.json").read_text(encoding="utf-8"))
BEATS = ["hook", "problem", "demo", "demo", "how", "impact", "ask"]


def _footage(seg: str, start: float, end: float, evidence=()) -> script.FootageDraft:
    claims = [script.ClaimDraft(text="it works", evidence=list(evidence))] if evidence else []
    line = "it works" if claims else "Is it?"  # a question needs no claim
    return script.FootageDraft(voice_line=line, segment_id=seg, source_start=start, source_end=end, claims=claims)


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

    # a factual sentence the draft left unlisted becomes a claim backed by the shot's own footage
    problem = script.FootageDraft(voice_line="Dinner is hard.", segment_id="c02", source_start=5, source_end=9, claims=[])
    unlisted = script.build_shots(_draft(problem=problem), "Hook line", SEGMENTS, REPO)[1].claims
    assert [(c.claim_id, c.text, [e.ref for e in c.evidence]) for c in unlisted] == [("cl1", "Dinner is hard.", ["c02"])]


def test_build_shots_rejects_unknown_segment():
    with pytest.raises(ValueError, match="c99"):
        script.build_shots(_draft(impact=_footage("c99", 1.0, 2.0)), "x", SEGMENTS, REPO)


def test_draft_needs_three_hooks_and_two_to_four_demos():
    with pytest.raises(ValueError):
        _draft(hooks=["a", "b"])
    with pytest.raises(ValueError):
        _draft(demos=[_footage("c04", 12.0, 18.0)])


def _judgement(rows) -> script.HookJudgement:
    return script.HookJudgement(verdicts=[
        script.HookVerdict(hook_id=h, states_fact=fact,
                           answers=[script.Answer(score=a, reason="r"), script.Answer(score=b, reason="r")])
        for h, a, b, fact in rows
    ])


def test_judge_hooks_picks_highest_mean():
    verdicts = _judgement([("h1", 0.4, 0.4, False), ("h2", 0.9, 0.8, False), ("h3", 0.9, 0.5, False)])
    hooks, best = script.score_hooks(["a", "b", "c"], verdicts, "App")
    assert [h.score for h in hooks] == [0.4, 0.85, 0.7]
    assert best.hook_id == "h2"
    with pytest.raises(ValueError, match="h3"):
        script.score_hooks(["a", "b", "c"], verdicts.model_copy(update={"verdicts": verdicts.verdicts[:2]}), "App")


def test_hook_that_states_a_fact_never_wins():
    hooks, best = script.score_hooks(
        ["a", "b", "c"], _judgement([("h1", 0.4, 0.4, False), ("h2", 0.9, 0.8, True), ("h3", 0.9, 0.5, False)]), "App")
    assert best.hook_id == "h3" and len(hooks) == 3
    hooks, best = script.score_hooks(
        ["a", "b", "c"], _judgement([(h, 0.9, 0.9, True) for h in ("h1", "h2", "h3")]), "App")
    assert best is None and hooks[-1].hook_id == "h4" and hooks[-1].text == "App"


# --------------------------------------------------------------------------- #
# Truth check
# --------------------------------------------------------------------------- #


def test_only_cuts_allows_cutting_words_but_not_adding():
    claim = "It writes three recipes in under one second"
    assert script.only_cuts("writes three recipes", claim)
    assert script.only_cuts("It Writes recipes.", claim)  # case and punctuation don't count
    assert not script.only_cuts("writes four recipes", claim)
    assert not script.only_cuts("writes three three recipes", claim)  # a word used more often is added
    assert not script.only_cuts("", claim)


def test_edit_line_rewrites_in_place_and_drops_the_sentence():
    line = "Snap your fridge. It writes three recipes in under one second! Then cook."
    claim = "writes three recipes in under one second"
    assert script.edit_line(line, claim, "writes three recipes") == "Snap your fridge. It writes three recipes! Then cook."
    assert script.edit_line(line, claim, "") == "Snap your fridge. Then cook."
    assert script.edit_line(line, "It writes three", "Writes three.") == \
        "Snap your fridge. Writes three recipes in under one second! Then cook."
    assert script.edit_line(line, "makes coffee", "") is None


def _shot(line, *claims):
    return Shot(shot_id="s02", beat="demo", voice_line=line, generated=False,
                visual=ShotVisual(kind="footage", segment_id="c04", source_start=12, source_end=14),
                claims=[Claim(claim_id=f"cl{i}", text=t, evidence=[], status="pending") for i, t in enumerate(claims, 1)])


def test_apply_checks_edits_lines_and_removes_shots_left_empty():
    shots = [
        _shot("It ranks recipes. It is fast.", "ranks recipes", "is fast"),
        _shot("It is instant.", "is instant"),  # its only sentence goes: shot removed
        _shot("It reads labels.", "reads the labels"),  # not worded as in the line: whole line goes
        _shot("It sees eggs and milk. Then cook.", "sees eggs and milk", "eggs"),  # 2nd claim cut with the 1st
    ]
    checked = {
        "cl1": Claim(claim_id="cl1", text="ranks recipes", evidence=[], status="backed"),
        "cl2": Claim(claim_id="cl2", text="is fast", evidence=[], status="dropped"),
    }
    out = script.apply_checks(shots[:1], checked)
    assert out[0].voice_line == "It ranks recipes." and [c.status for c in out[0].claims] == ["backed", "dropped"]

    dropped = {"cl1": Claim(claim_id="cl1", text="x", evidence=[], status="dropped"),
               "cl2": Claim(claim_id="cl2", text="x", evidence=[], status="dropped")}
    assert script.apply_checks(shots[1:3], dropped) == []
    assert script.apply_checks(shots[3:], dropped)[0].voice_line == "Then cook."
    mixed = {"cl1": Claim(claim_id="cl1", text="sees eggs", evidence=[], status="rewritten"),
             "cl2": Claim(claim_id="cl2", text="eggs", evidence=[], status="backed")}
    assert script.apply_checks(shots[3:], mixed)[0].voice_line == "It sees eggs. Then cook."


def test_unclaimed_sentences_are_facts_with_no_claim():
    line = "Is dinner hard? PantryPal reads your fridge. It suggests three recipes"
    assert script.unclaimed_sentences(line, ["PantryPal reads your fridge"]) == ["It suggests three recipes"]
    assert script.unclaimed_sentences(line, ["reads your fridge", "suggests three recipes"]) == []


def test_readme_section_stops_at_the_next_heading_of_its_level():
    readme = "# App\nintro\n## Use\nstep\n### Detail\nmore\n```\n# not a heading\n```\n## Next\nother"
    assert script.readme_section(readme, "Use") == "## Use\nstep\n### Detail\nmore\n```\n# not a heading\n```"
    assert script.readme_section(readme, "Missing") == ""


def test_evidence_that_cannot_be_shown_counts_as_none(tmp_path):
    segs = {s.segment_id: s for s in SEGMENTS}
    ev = [Evidence(type="file", ref=p) for p in REPO.tree if p not in {k.path for k in REPO.key_files}][:1]
    ev.append(Evidence(type="footage", ref="c02"))  # no frames in tmp_path
    assert script.evidence_shown(ev, tmp_path, segs, REPO) == ([], [])
    blocks, images = script.evidence_shown([Evidence(type="footage", ref="c02")], FIXTURE_RUN, segs, REPO)
    assert len(blocks) == 1 and [p.name for p in images] == [f.split("/")[1] for f in segs["c02"].frames]


def test_check_claim_drops_without_evidence_and_when_a_rewrite_adds_words_or_is_unsupported(monkeypatch):
    claim = Claim(claim_id="cl1", text="writes three recipes in one second", evidence=[], status="pending")

    def no_call(*a, **k):
        raise AssertionError("no evidence must mean no Gemini call")

    monkeypatch.setattr(gemini, "generate", no_call)
    out, v = script.check_claim(claim, [], [])
    assert out.status == "dropped" and out.text == claim.text and v.score == 0

    def answer(rewrite, supported=True):
        verdict = script.ClaimVerdict(score=0.5, reason="r", action="rewrite", rewrite=rewrite,
                                      rewrite_supported=supported)
        monkeypatch.setattr(gemini, "generate", lambda *a, **k: verdict)
        return script.check_claim(claim, ["README section:\n## X\ny"], [])[0]

    assert (answer("writes three recipes").status, answer("writes three recipes").text) == ("rewritten", "writes three recipes")
    assert answer("writes four recipes").status == "dropped"
    assert answer("Writes three recipes in one second.").status == "backed"
    assert answer("writes three recipes", supported=False).status == "dropped"  # judge: still partly unsupported


def test_script_stage_writes_checked_script_and_decisions(tmp_path):
    for name in ("run.json", "repo_files.json", "summary.json", "moments.json", "candidates.json",
                 "frame_scores.json"):
        shutil.copy2(FIXTURE_RUN / name, tmp_path / name)
    shutil.copytree(FIXTURE_RUN / "frames", tmp_path / "frames")
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
    assert beats[0] == "hook" and beats[-1] == "ask"  # hook has no claims; ask may lose its shot only if emptied
    assert beats == sorted(beats, key=["hook", "problem", "demo", "how", "impact", "ask"].index)
    chosen = next(h for h in out.hooks if h.hook_id == out.chosen_hook_id)
    assert out.shots[0].voice_line == chosen.text and out.shots[0].claims == []
    claims = [c for s in out.shots for c in s.claims]
    assert claims and all(c.status in ("backed", "rewritten", "dropped") for c in claims)
    for s in out.shots:
        said = " ".join(script.words(s.voice_line))
        for c in s.claims:
            if c.status != "dropped":
                assert " ".join(script.words(c.text)) in said

    log = Decisions.model_validate_json((tmp_path / "decisions.json").read_text())
    assert log.decisions[:len(frame_picks)] == frame_picks
    mine = log.decisions[len(frame_picks):]
    assert [d.kind for d in mine] == ["hook_pick"] + ["claim_check"] * len(mine[1:])
    assert mine[0].chosen == out.chosen_hook_id
    by_claim = {d.subject: d for d in mine[1:]}
    assert len(by_claim) == len(mine) - 1
    for c in claims:  # claims of a removed shot are only in the log
        assert by_claim[c.claim_id].chosen == {"backed": "keep", "rewritten": "rewrite", "dropped": "drop"}[c.status]
