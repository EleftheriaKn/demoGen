"""Stage script: draft with hooks (ticket A4) and the truth check (ticket A5)."""

from __future__ import annotations

import shutil

import pytest

from backend import events
from backend.clients import gemini
from backend.contracts import (Candidates, Claim, Decisions, Evidence, FlowStep, Moments, RepoFiles, Script, Shot,
                               ShotVisual, Summary)
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


def test_build_shots_illustrates_unknown_segment(tmp_path):
    # B24: footage on a segment that doesn't exist becomes a generated shot instead of failing the run
    events.bind(tmp_path, "t")  # it logs the swap
    shots = script.build_shots(_draft(impact=_footage("c99", 1.0, 2.0)), "x", SEGMENTS, REPO)
    impact = next(s for s in shots if s.beat == "impact")
    assert impact.generated and impact.visual.kind == "card"


def test_draft_needs_three_hooks_and_two_to_four_demos():
    with pytest.raises(ValueError):
        _draft(hooks=["a", "b"])
    with pytest.raises(ValueError):
        _draft(demos=[_footage("c04", 12.0, 18.0)])


def _judgement(rows) -> script.HookJudgement:
    """rows: (hook_id, curious, specific, states_fact[, about_user=1.0])."""
    return script.HookJudgement(verdicts=[
        script.HookVerdict(hook_id=r[0], states_fact=r[3],
                           answers=[script.Answer(score=s, reason="r") for s in (r[1], r[2], *(r[4:] or (1.0,)))])
        for r in rows
    ])


def test_judge_hooks_picks_highest_mean():
    verdicts = _judgement([("h1", 0.4, 0.4, False), ("h2", 0.9, 0.8, False), ("h3", 0.9, 0.5, False)])
    hooks, best = script.score_hooks(["a", "b", "c"], verdicts, "App")
    assert [h.score for h in hooks] == [0.6, 0.9, 0.8]
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


def test_hook_off_the_users_topic_never_wins():
    rows = [("h1", 0.5, 0.5, False), ("h2", 1.0, 1.0, False, 0.2), ("h3", 0.6, 0.6, False, 0.5)]
    hooks, best = script.score_hooks(["a", "b", "c"], _judgement(rows), "App")
    assert best.hook_id == "h1"  # h2 scores highest but isn't about what the app does for its user
    hooks, best = script.score_hooks(["a", "b", "c"], _judgement([(h, 1, 1, False, 0.1) for h in ("h1", "h2", "h3")]), "App")
    assert best is None and hooks[-1].text == "App"


def test_problem_shot_comes_back_fact_free_when_emptied():
    drafted = script.build_shots(_draft(), "Hook line", SEGMENTS, REPO)
    kept = [s for s in drafted if s.beat != "problem"]
    out = script.keep_problem(kept, drafted, "Dinner is hard.")
    assert [s.beat for s in out] == BEATS and out[1].voice_line == "Dinner is hard." and out[1].claims == []
    assert out[1].visual == drafted[1].visual
    assert script.keep_problem(drafted, drafted, "x") == drafted


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


def test_reworded_claims_edit_their_sentence_not_the_whole_line():
    # Coach Platform A9 v3: claims drafted in other words than the line blanked every demo shot,
    # taking kept claims with them.
    line = "Add a player as CB. Schedule a tactical session on Wednesday 11. Then cook."
    assert script.edit_line(line, "Add Farasia as CB", "") == "Schedule a tactical session on Wednesday 11. Then cook."
    assert script.edit_line(line, "Schedules your tactical session for Wednesday 11", "Schedules your tactical session") \
        == "Add a player as CB. Schedules your tactical session. Then cook."
    assert script.edit_line(line, "makes coffee", "") is None  # under half its words in any sentence

    shots = [_shot("Add a player as CB. Schedule a tactical session.", "Add Farasia as CB", "Schedule a tactical session"),
             _shot("It is great.", "Displays the weekly calendar")]
    checked = {"cl1": Claim(claim_id="cl1", text="x", evidence=[], status="dropped"),
               "cl2": Claim(claim_id="cl2", text="Schedule a tactical session", evidence=[], status="backed")}
    assert script.apply_checks(shots[:1], checked)[0].voice_line == "Schedule a tactical session."
    rewritten = {"cl1": Claim(claim_id="cl1", text="Displays the calendar", evidence=[], status="rewritten")}
    assert script.apply_checks(shots[1:], rewritten)[0].voice_line == "Displays the calendar"  # the checked words


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
    assert len(blocks) == 1 and "before frames/c01_3s.jpg" in blocks[0] and "after frames/c03_10.5s.jpg" in blocks[0]
    assert [p.name for p in images] == ["c01_3s.jpg", *(f.split("/")[1] for f in segs["c02"].frames), "c03_10.5s.jpg"]


def test_ordered_frames_add_non_overlapping_neighbours_in_time_order(tmp_path):
    segs = {s.segment_id: s for s in SEGMENTS}
    # first segment: nothing before it
    assert [lab for lab, _ in script.ordered_frames(segs["c01"], segs, FIXTURE_RUN)] == ["during"] * 3 + ["after"]
    # last segment: nothing after it
    last = script.ordered_frames(segs["c06"], segs, FIXTURE_RUN)
    assert last[0] == ("before", "frames/c05_21.25s.jpg") and last[-1][0] == "during"
    # an overlapping segment is not a neighbour: c04 (12-19) spans the long one's start, so c03 ends before
    long = segs["c04"].model_copy(update={"segment_id": "cx", "start": 12.5, "end": 21.0})
    with_long = {**segs, "cx": long}
    out = script.ordered_frames(long, with_long, FIXTURE_RUN)
    assert out[0] == ("before", "frames/c03_11.5s.jpg") and out[-1] == ("after", "frames/c06_23.5s.jpg")
    # no own frames on disk: nothing, even though neighbours exist
    assert script.ordered_frames(segs["c02"], segs, tmp_path) == []


def test_generated_shot_claims_never_use_footage():
    segs = {s.segment_id: s for s in SEGMENTS}
    heading = sorted(script.readme_headings(REPO.readme))[0]
    ev = [Evidence(type="footage", ref="c02"), Evidence(type="readme", ref=f"README#{heading}")]
    blocks, images = script.evidence_shown(ev, FIXTURE_RUN, segs, REPO, generated=True)
    assert images == [] and len(blocks) == 1 and blocks[0].startswith("README section:")
    # footage alone on a generated shot is no evidence, so the claim drops without a call
    assert script.evidence_shown(ev[:1], FIXTURE_RUN, segs, REPO, generated=True) == ([], [])


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


def test_focus_box_converts_and_rejects_bad_boxes():
    assert script.focus_box([100, 200, 600, 700]) == pytest.approx([0.2, 0.1, 0.5, 0.5])
    assert script.focus_box([0, 0, 1000, 1000]) == [0, 0, 1, 1]
    assert script.focus_box([100, 200, 600, 350]) is None  # 15% wide: too narrow
    assert script.focus_box([100, 800, 600, 1100]) is None  # leaves the frame
    assert script.focus_box([600, 200, 100, 700]) is None  # upside down
    assert script.focus_box([1, 2, 3]) is None


def test_add_zoom_one_call_per_chosen_frame(tmp_path, monkeypatch):
    """Replays tests/recordings/ (record with `pytest --live`)."""
    shutil.copytree(FIXTURE_RUN / "frames", tmp_path / "frames")
    shutil.copy2(FIXTURE_RUN / "decisions.json", tmp_path / "decisions.json")
    events.bind(tmp_path, "test")
    shots = Script.model_validate_json((FIXTURE_RUN / "script.json").read_text(encoding="utf-8")).shots
    demo = shots[2]
    demo = demo.model_copy(update={"visual": demo.visual.model_copy(update={"focus_box": None})})
    impact = demo.model_copy(update={"shot_id": "s04", "beat": "impact", "voice_line": "Dinner is planned in seconds.", "claims": []})
    shots = [shots[0], shots[1], demo, impact]  # demo and impact share c04's chosen frame

    calls = []
    real = gemini.generate
    monkeypatch.setattr(gemini, "generate", lambda *a, **k: calls.append(k["images"]) or real(*a, **k))
    out = script.add_zoom(tmp_path, shots)

    assert calls == [[tmp_path / "frames/c04_15.5s.jpg"]]  # one call for the one chosen frame
    assert out[0] == shots[0] and out[1] == shots[1]  # card and problem shot: no zoom
    box = out[2].visual.focus_box
    assert box is not None and box[2] >= script.MIN_ZOOM_W and box[0] + box[2] <= 1 and box[1] + box[3] <= 1
    assert out[3].visual.focus_box == box
    Script(language="en", hooks=[{"hook_id": "h1", "text": "x", "score": 0}], chosen_hook_id="h1", shots=out)


def test_problem_comeback_is_the_first_sentence_fitted_to_its_clip():
    drafted = script.build_shots(_draft(), "Hook line", SEGMENTS, REPO)
    kept = [s for s in drafted if s.beat != "problem"]
    problem = "Coaches juggle " + " ".join(["tools"] * 20) + ". The app fixes it all in one place."
    out = script.keep_problem(kept, drafted, problem, SEGMENTS)[1]
    assert out.voice_line == "Coaches juggle " + " ".join(["tools"] * 20) + "." and out.claims == []
    seg = next(g for g in SEGMENTS if g.segment_id == out.visual.segment_id)
    assert seg.start <= out.visual.source_start and out.visual.source_end <= seg.end
    assert (out.visual.source_end - out.visual.source_start
            >= min(seg.end - seg.start, 22 / script.WORDS_PER_S - script.HOLD_S) - 0.01)  # grew inside its segment


def test_voice_fits_its_clip_plus_hold_by_growing_the_clip_then_dropping_sentences():
    seg = next(g for g in SEGMENTS if g.segment_id == "c02")  # 5-10 s
    assert script.fit_voice("Add todos.", seg, 6.0, 7.0) == (6.0, 7.0, "Add todos.")  # cap 9 (3 x (1 + 2 s))
    # 15 words want 3 s of clip (5 s - 2 s hold): the clip grows inside the segment instead of trimming
    assert script.fit_voice(" ".join(["w"] * 14) + " end.", seg, 6.0, 7.0)[:2] == (6.0, 9.0)
    # 30 words want 8 s but the segment has 5 s (cap 21): whole sentences go from the end
    line = " ".join(["a"] * 9 + ["b."] + ["c"] * 9 + ["d."] + ["e"] * 9 + ["f."])
    assert script.fit_voice(line, seg, 6.0, 7.0)[2] == " ".join(["a"] * 9 + ["b."] + ["c"] * 9 + ["d."])
    # one sentence longer than the cap stays whole: never cut mid-sentence, never below one sentence
    long = " ".join(["word"] * 30) + ". Second sentence."
    assert script.fit_voice(long, seg, 5.0, 10.0)[2] == " ".join(["word"] * 30) + "."


def test_trimmed_claim_goes_and_the_rest_is_still_checked():
    claim = "It also plans every meal for the whole week ahead."
    line = "It syncs your pantry across devices. " + claim  # 16 words; c03 is 10-12 s: cap 12
    impact = script.FootageDraft(voice_line=line, segment_id="c03", source_start=10, source_end=12,
                                 claims=[script.ClaimDraft(text=claim, evidence=[{"type": "readme", "ref": "README#How it works"}])])
    shot = script.build_shots(_draft(impact=impact), "x", SEGMENTS, REPO)[5]
    assert shot.voice_line == "It syncs your pantry across devices."
    assert [(c.text, [e.ref for e in c.evidence]) for c in shot.claims] == [("It syncs your pantry across devices.", ["c03"])]


@pytest.mark.parametrize("limit,target_s,total,model,beats", [
    (30, 26, 65, script.DraftShort, ["hook", "problem", "demo", "ask"]),
    (45, 38, 95, script.DraftMid, ["hook", "problem", "demo", "impact", "ask"]),
    (180, 75, 190, script.Draft, ["hook", "problem", "demo", "how", "impact", "ask"]),
])
def test_target_words_and_beats_follow_the_time_limit(limit, target_s, total, model, beats):
    assert script.target(limit) == (target_s, total)
    assert script.draft_model(target_s) is model
    assert script.beats_of(model) == beats
    plan = script.budget(beats, total)
    assert list(plan) == beats and sum(plan.values()) == total


def test_180_s_keeps_todays_budget_and_shots():
    assert script.budget(script.beats_of(script.Draft), 190) == script.BUDGET
    assert script.budget(["hook", "problem", "demo", "impact", "ask"], 95) == {
        "hook": 7, "problem": 16, "demo": 57, "impact": 9, "ask": 6}  # same proportions as BUDGET


def test_short_drafts_build_shots_in_beat_order():
    base = _draft().model_dump()
    short = script.DraftShort(**{k: base[k] for k in script.DraftShort.model_fields} | {"demos": base["demos"][:1]})
    assert [s.beat for s in script.build_shots(short, "h", SEGMENTS, REPO)] == ["hook", "problem", "demo", "ask"]
    mid = script.DraftMid(**{k: base[k] for k in script.DraftMid.model_fields})
    assert [s.beat for s in script.build_shots(mid, "h", SEGMENTS, REPO)] == ["hook", "problem", "demo", "demo",
                                                                              "impact", "ask"]
    with pytest.raises(ValueError):
        script.DraftShort(**{k: base[k] for k in script.DraftShort.model_fields})  # 2 demos > 1


# --------------------------------------------------------------------------- #
# A14: generated shots fill weak and missing flow steps
# --------------------------------------------------------------------------- #

FLOW = [FlowStep(step_id="s1", text="Open the camera.", evidence=["frontend/src/Camera.tsx"]),
        FlowStep(step_id="s2", text="See recipes.", evidence=["backend/recipes.py"]),
        FlowStep(step_id="s3", text="Shop the list.", evidence=["backend/recipes.py", "ghost.py"])]
MOMENTS = Moments.model_validate_json((FIXTURE_RUN / "moments.json").read_text())
MOMENTS = MOMENTS.model_copy(update={"moments": [
    m.model_copy(update={"m2": {"quality": "weak", "flow_step": "s1"},
                         "m3": {"quality": "wow", "flow_step": "s2"}}.get(m.moment_id, {}))
    for m in MOMENTS.moments]})  # c02 = weak s1, c04 = wow s2, s3 missing


def _shot_draft(seg="", gen=False, step="", line="It works.", evidence=()):
    return script.ShotDraft(voice_line=line, segment_id=seg, source_start=0, source_end=0, generated=gen,
                            flow_step=step, card_prompt="a flat panel" if gen else "",
                            claims=[script.ClaimDraft(text=line, evidence=list(evidence))])


def _gdraft(**over) -> script.DraftG:
    base = dict(hooks=["a", "b", "c"], hook_card_prompt="t",
                problem=_shot_draft("c04", line="Dinner is hard.", evidence=[{"type": "file", "ref": "backend/vision.py"}]),
                demos=[_shot_draft("c04", step="s2"), _shot_draft("c02", step="s1")],
                how=_shot_draft("c04", gen=True, step="step s3", evidence=[{"type": "footage", "ref": "c04"}]),
                impact=_shot_draft("c04"),
                ask=script.CardDraft(voice_line="Try it.", card_prompt="end", claims=[]))
    return script.DraftG(**{**base, **over})


def test_treatment_weak_and_missing_are_generated_wow_stays_footage():
    shots = script.build_shots(_gdraft(), "h", SEGMENTS, REPO, MOMENTS, FLOW)
    by = {s.shot_id: s for s in shots}
    assert by["s03"].visual.kind == "footage"  # wow step s2
    weak = by["s04"]  # footage asked on a weak moment -> a panel of its own frames
    assert weak.generated and weak.visual.kind == "card"
    assert weak.visual.reference_frames == next(g for g in SEGMENTS if g.segment_id == "c02").frames
    missing = by["s05"]  # s3 never shown: frames of the nearest shown step (s2 -> c04)
    assert missing.generated and missing.visual.card_prompt == "a flat panel"
    assert missing.visual.reference_frames == next(g for g in SEGMENTS if g.segment_id == "c04").frames
    # a generated demo on a step a wow moment shows goes back to footage on that moment
    back = script.build_shots(_gdraft(demos=[_shot_draft("", gen=True, step="s2"), _shot_draft("c04")]),
                              "h", SEGMENTS, REPO, MOMENTS, FLOW)[2]
    assert not back.generated and back.visual.segment_id == "c04"


def test_evidence_is_forced_by_shot_type():
    shots = script.build_shots(_gdraft(), "h", SEGMENTS, REPO, MOMENTS, FLOW)
    refs = {s.shot_id: [[e.ref for e in c.evidence] for c in s.claims] for s in shots}
    assert refs["s02"] == [["backend/vision.py"]]  # a repo-backed problem claim isn't judged on frames
    assert refs["s03"] == [["c04"]]  # an on-screen claim drafted with no evidence carries its own segment
    readme_demo = _gdraft(demos=[_shot_draft("c04", step="s2", evidence=[{"type": "file", "ref": "backend/vision.py"}]),
                                 _shot_draft("c04")])
    demo = script.build_shots(readme_demo, "h", SEGMENTS, REPO, MOMENTS, FLOW)[2]
    assert [e.ref for e in demo.claims[0].evidence] == ["backend/vision.py", "c04"]  # a demo claim always does
    assert refs["s05"] == [["backend/recipes.py"]]  # generated: footage stripped, step evidence added
    assert refs["s04"] == [["frontend/src/Camera.tsx"]]


def test_cap_keeps_half_the_runtime_real_footage(monkeypatch):
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    long = " ".join(["word"] * 20) + "."
    draft = _gdraft(problem=_shot_draft("c04", line=" ".join(["word"] * 30) + "."),
                    demos=[_shot_draft("c04", step="s2", line="Short."), _shot_draft("c02", line=long)],
                    how=_shot_draft("", gen=True, line=long), impact=_shot_draft("", gen=True, line=long))
    shots = script.build_shots(draft, "Hook.", SEGMENTS, REPO, MOMENTS, FLOW)
    assert script.real_share(shots) < script.MIN_REAL
    out = script.cap_generated(shots)
    assert script.real_share(out) >= script.MIN_REAL
    assert out[0].beat == "hook" and out[-1].beat == "ask"  # cards stay
    assert [s.shot_id for s in shots if s not in out] == ["s05", "s06"]  # the last generated middle shots go
    assert any(s.generated and s.beat == "demo" for s in out)  # the earlier one stays once the cap holds
    assert script.cap_generated(out) == out


def test_old_summary_without_flow_keeps_todays_draft():
    summary = Summary.model_validate_json((FIXTURE_RUN / "summary.json").read_text(encoding="utf-8"))
    assert not summary.flow and script.flow_block(summary, MOMENTS) == ""
    assert script.draft_model(75) is script.Draft and script.draft_model(75, True) is script.DraftG
    assert script.draft_model(26, True) is script.DraftShortG
    shots = script.build_shots(_draft(), "h", SEGMENTS, REPO)  # no moments, no flow: all middle shots footage
    assert [s.generated for s in shots] == [True, False, False, False, False, False, True]


# --------------------------------------------------------------------------- #
# A19: callouts ride on a claim and live or die with it
# --------------------------------------------------------------------------- #


def _called(seg, line, callout):
    return script.FootageDraft(voice_line=line, segment_id=seg, source_start=12.0, source_end=18.0,
                               claims=[script.ClaimDraft(text=line.rstrip("."), evidence=[], callout=callout)])


def test_callout_survives_with_its_claim_and_goes_with_it(monkeypatch):
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    draft = _draft(demos=[_called("c04", "It lists two items left.", "Two items left"),
                          _called("c04", "It edits titles in place.", "Edits titles in place"),
                          _called("c04", "It filters done tasks.", "One click filters every task here")],
                   impact=_called("c02", "It saves time.", "Saves time"),
                   ask=script.CardDraft(voice_line="Try it.", card_prompt="end",
                                        claims=[script.ClaimDraft(text="Try it", evidence=[], callout="Try it")]))
    ties: dict = {}
    shots = script.build_shots(draft, "h", SEGMENTS, REPO, ties=ties)
    assert set(ties) == {"s03", "s04", "s07"}  # over 5 words (s05) and the ask get no tie
    status = {"s03": ("backed", "lists two items left"), "s04": ("rewritten", "edits titles"),
              "s07": ("dropped", "x")}
    checked = {c.claim_id: c.model_copy(update=dict(zip(("status", "text"), status.get(s.shot_id, ("backed", c.text)))))
               for s in shots for c in s.claims}
    out = {s.shot_id: s for s in script.add_callouts(script.apply_checks(shots, checked), ties)}
    assert out["s03"].callout == "Two items left"
    assert out["s04"].callout is None  # rewrite lost "in place"
    assert "s07" not in out  # its only claim dropped: no shot, no callout
    assert all(s.callout is None for s in out.values() if s.beat in ("hook", "ask"))
    kept = script.add_callouts([out["s04"].model_copy(update={"claims": [
        out["s04"].claims[0].model_copy(update={"text": "edits titles in place"})]})], ties)
    assert kept[0].callout == "Edits titles in place"  # every callout word still in the rewrite
    problem = script.keep_problem([], [shots[1]], "A plain problem.")  # fact-free comeback: no claim
    assert script.add_callouts(problem, {"s02": ("cl1", "Hard problem")})[0].callout is None
    count = {"s03": (out["s03"].claims[0].claim_id, "4 items left")}  # "4" is in no checked claim
    assert script.add_callouts([out["s03"].model_copy(update={"callout": None})], count)[0].callout is None


def test_draft_without_callouts_gives_todays_script():
    ties: dict = {}
    shots = script.build_shots(_draft(), "h", SEGMENTS, REPO, ties=ties)
    assert ties == {} and script.add_callouts(shots, ties) == shots == script.build_shots(_draft(), "h", SEGMENTS, REPO)
    assert all("callout" not in s.model_dump() for s in shots)


def test_kept_claims_callout_is_made_only_of_its_words(monkeypatch):
    # Coach Platform: "Unified application specification" was shown on a kept claim that never says "unified".
    said = []
    monkeypatch.setattr(events, "emit", lambda stage, msg, **k: said.append(msg))
    shot = _shot("Read the application specification.", "Read the application specification").model_copy(
        update={"shot_id": "s03"})
    kept = [shot.model_copy(update={"claims": [c.model_copy(update={"status": "backed"}) for c in shot.claims]})]
    assert script.add_callouts(kept, {"s03": ("cl1", "Application specification")})[0].callout == \
        "Application specification"
    assert script.add_callouts(kept, {"s03": ("cl1", "Your app specification")})[0].callout is None
    assert script.add_callouts(kept, {"s03": ("cl1", "Unified application specification")})[0].callout is None
    assert said[-1].startswith('Callout s03 dropped: "Unified application specification", unified not in its claim cl1')
    # stop words need not be in the claim
    assert script.add_callouts(kept, {"s03": ("cl1", "The specification of your application")})[0].callout == \
        "The specification of your application"


def test_kept_claim_worded_differently_replaces_its_sentence():
    # Coach Platform: the line said "Add Farasia as CB" while the kept claim said "Add a player as CB".
    kept = {"cl1": Claim(claim_id="cl1", text="Add a player as CB", evidence=[], status="backed"),
            "cl2": Claim(claim_id="cl2", text="Schedule a tactical session", evidence=[], status="backed")}
    shot = _shot("Add Farasia as CB. Schedule a tactical session! Then cook.",
                 "Add a player as CB", "Schedule a tactical session")
    assert script.apply_checks([shot], kept)[0].voice_line == \
        "Add a player as CB. Schedule a tactical session! Then cook."  # exact match and unclaimed sentence untouched
    lower = {"cl1": kept["cl1"].model_copy(update={"text": "add a player as CB."})}
    assert script.apply_checks([_shot("Then cook. Add Farasia as CB", "add a player as CB.")], lower)[0].voice_line == \
        "Then cook. Add a player as CB"  # the line's capital and (missing) end punctuation
