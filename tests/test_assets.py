"""Stage assets: voice per shot (A4), title cards best of 4 and music bed (A7). fal and Gemini are faked."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from backend import events
from backend.clients import fal, gemini
from backend.contracts import Assets, Decisions, Script
from backend.pipeline import assets, ff, judge
from backend.run import FIXTURE_RUN, load_run

DATA = Path(__file__).resolve().parent / "data"
DRAFTS = [DATA / f"s01_{i}.png" for i in range(4)]  # committed, so recording keys match on every machine


def _verdict(n_drafts, n_questions, best):
    frame = lambda i: judge._Frame(note=f"draft {i}", answers=[judge._Answer(score=0.9 if i == best else 0.3, reason="r")] * n_questions)
    return judge._Verdict(frames=[frame(i) for i in range(n_drafts)])


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMOCUT_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("FAL_KEY", "test")
    calls = {"tts": 0, "card": [], "music": [], "gemini": 0}

    def fake_tts(text, language, out_path):
        calls["tts"] += 1
        ff.ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.5", "-c:a", "libmp3lame", "-f", "mp3", out_path)

    def fake_card(prompt, out_path):
        calls["card"].append(prompt)
        shutil.copyfile(DRAFTS[len(calls["card"]) % 4], out_path)

    def fake_music(prompt, seconds, out_path):
        calls["music"].append(seconds)
        ff.ff("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", str(seconds), "-c:a", "libmp3lame", "-f", "mp3", out_path)

    def fake_generate(text, schema, images=(), video=None):
        calls["gemini"] += 1
        return calls.get("verdict") or _verdict(len(images), len(assets.CARD_QUESTIONS), best=2)

    monkeypatch.setattr(fal, "tts", fake_tts)
    monkeypatch.setattr(fal, "card_image", fake_card)
    monkeypatch.setattr(fal, "music", fake_music)
    monkeypatch.setattr(gemini, "generate", fake_generate)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for f in ("run.json", "script.json"):
        shutil.copy2(FIXTURE_RUN / f, run_dir / f)
    events.bind(run_dir, "test")
    return run_dir, calls


def _load(run_dir):
    shots = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8")).shots
    out = Assets.model_validate_json((run_dir / "assets.json").read_text(encoding="utf-8"))
    log = Decisions.model_validate_json((run_dir / "decisions.json").read_text(encoding="utf-8")) \
        if (run_dir / "decisions.json").exists() else Decisions(decisions=[], rubric=[])
    return shots, out, [d for d in log.decisions if d.kind == "take_pick"]


def test_full_mode_cards_best_of_4_and_music(fake):
    run_dir, calls = fake
    cfg = load_run(run_dir).model_copy(update={"mode": "full"})
    assets.run(run_dir, cfg)

    shots, out, picks = _load(run_dir)
    card_shots = [s.shot_id for s in shots if s.visual.kind == "card"]
    assert [v.shot_id for v in out.voice] == [s.shot_id for s in shots]
    assert all(v.duration_s > 0 and (run_dir / v.path).exists() for v in out.voice)
    assert [c.shot_id for c in out.cards] == card_shots
    for c in out.cards:
        assert len(c.drafts) == 4 and 0 <= c.chosen_index < len(c.drafts) and c.chosen_index == 2
        assert (run_dir / c.path).read_bytes() == (run_dir / c.drafts[c.chosen_index]).read_bytes()
    assert all(assets.NO_TEXT in p and assets.FAL_STYLE in p for p in calls["card"])
    assert [d.subject for d in picks] == card_shots
    assert all(d.chosen == c.drafts[c.chosen_index] for d, c in zip(picks, out.cards))
    assert out.music is not None and out.music.duration_s > 0 and (run_dir / "music.wav").exists()
    assert calls["music"] == [round(max(3.0, sum(v.duration_s for v in out.voice)))]

    assets.run(run_dir, cfg)  # second run: all from the cache, decisions replaced not doubled
    assert (calls["tts"], len(calls["card"]), len(calls["music"]), calls["gemini"]) == (len(shots), 4 * len(card_shots), 1, len(card_shots))
    assert len(_load(run_dir)[2]) == len(card_shots)


def test_cheap_mode_plain_cards_silent_music(fake):
    run_dir, calls = fake
    assets.run(run_dir, load_run(run_dir))  # fixture run.json is cheap

    shots, out, picks = _load(run_dir)
    assert [c.shot_id for c in out.cards] == [s.shot_id for s in shots if s.visual.kind == "card"]
    assert all(len(c.drafts) == 1 and c.chosen_index == 0 and (run_dir / c.path).exists() for c in out.cards)
    assert out.music is not None and out.music.duration_s > 0 and (run_dir / out.music.path).exists() and picks == []
    assert sorted(f.name for f in (run_dir / "cards" / "drafts").iterdir()) == [f"{c.shot_id}_0.png" for c in out.cards]
    assert calls["card"] == [] and calls["music"] == [] and calls["gemini"] == 0


def test_pick_best_judges_image_drafts():
    """Replays tests/recordings/ (record with `pytest --live`). Draft 0 has text in it and should lose."""
    drafts = DRAFTS
    r = judge.pick_best(drafts, assets.CARD_QUESTIONS, "a title card for a todo-list app demo")
    assert 0 <= r["index"] < 4 and len(r["scores"]) == 4
    assert r["judge_score"] == r["scores"][r["index"]] == max(r["scores"])
    assert [a["question"] for a in r["answers"]] == assets.CARD_QUESTIONS and r["reason"]
    assert r["index"] != 0


def test_card_prompt_drops_quoted_titles_keeps_look():
    p = assets.card_prompt("Bold title card: 'PantryPal' over a warm orange background, in the app's style")
    assert "PantryPal" not in p and "warm orange background" in p and "app's style" in p and assets.NO_TEXT in p and assets.FAL_STYLE in p


def _rows(*rows):
    """One draft per row of (no-text, readable, polished) scores."""
    return judge._Verdict(frames=[judge._Frame(note=f"draft {i}", answers=[judge._Answer(score=x, reason=f"r{i}") for x in r])
                                  for i, r in enumerate(rows)])


def test_draft_with_drawn_text_cannot_win(fake):
    run_dir, calls = fake
    # draft 1 has the best mean but drawn text; draft 2 is the best text-free one
    calls["verdict"] = _rows((0.1, 1, 1), (0.2, 1, 1), (0.6, 0.5, 0.5), (0.5, 0.3, 0.3))
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    _, out, picks = _load(run_dir)
    assert all(c.chosen_index == 2 and len(c.drafts) == 4 for c in out.cards)
    assert all(d.chosen.endswith("_2.png") for d in picks)


def test_all_drafts_with_text_fall_back_to_plain_card(fake):
    run_dir, calls = fake
    calls["verdict"] = _rows(*[(0.2, 1, 1)] * 4)
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    _, out, picks = _load(run_dir)
    for c, d in zip(out.cards, picks):
        assert c.drafts[-1] == f"cards/drafts/{c.shot_id}_plain.png" and c.chosen_index == 4
        assert (run_dir / c.path).read_bytes() == (run_dir / c.drafts[4]).read_bytes()
        assert d.chosen == c.drafts[4] and d.options == c.drafts and "plain card" in d.reason
    assert len(picks) == len(out.cards)


def _events(run_dir):
    return [l for l in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _fail(monkeypatch, name, when=lambda *a: True):
    real = getattr(fal, name)

    def flaky(*args):
        if when(*args):
            raise TimeoutError("fal down")
        return real(*args)

    monkeypatch.setattr(fal, name, flaky)


@pytest.fixture
def no_sleep(monkeypatch):
    from backend import cache
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)


def test_missing_fal_key_falls_back_and_says_so_once(fake, monkeypatch):
    run_dir, calls = fake
    for k in ("FAL_KEY", "FAL_KEY_ID", "FAL_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for name in ("tts", "card_image", "music"):
        monkeypatch.setattr(fal, name, lambda *a, name=name: pytest.fail(f"fal.{name} called without FAL_KEY"))
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    shots, out, picks = _load(run_dir)
    assert out.voice == [] and picks == []
    assert [c.shot_id for c in out.cards] == [s.shot_id for s in shots if s.visual.kind == "card"]
    assert all((run_dir / c.path).exists() for c in out.cards) and (run_dir / out.music.path).exists()
    assert sum(assets.NO_FAL_KEY in e for e in _events(run_dir)) == 1


def test_failed_voice_take_is_left_out_with_an_event(fake, monkeypatch, no_sleep):
    run_dir, _ = fake
    first = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8")).shots[0]
    _fail(monkeypatch, "tts", lambda text, lang, out: text == first.voice_line)
    assets.run(run_dir, load_run(run_dir))
    shots, out, _ = _load(run_dir)
    assert [v.shot_id for v in out.voice] == [s.shot_id for s in shots[1:]]
    assert any(f"Voice {first.shot_id}: fal failed" in e for e in _events(run_dir))


def test_failed_card_falls_back_to_plain_card(fake, monkeypatch, no_sleep):
    run_dir, _ = fake
    _fail(monkeypatch, "card_image")
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    shots, out, picks = _load(run_dir)
    assert [c.shot_id for c in out.cards] == [s.shot_id for s in shots if s.visual.kind == "card"]
    for c in out.cards:
        assert c.drafts == [f"cards/drafts/{c.shot_id}_plain.png"] and (run_dir / c.path).exists()
        assert any(f"Card {c.shot_id}: every fal draft failed" in e for e in _events(run_dir))
    assert picks == []  # nothing was judged


def test_some_failed_drafts_judge_only_the_rest(fake, monkeypatch, no_sleep):
    run_dir, calls = fake
    doomed, lock = set(), threading.Lock()  # the first two drafts asked for fail on every retry

    def two_fail(prompt, out):
        with lock:
            if len(doomed) < 2:
                doomed.add(out)
            return out in doomed

    _fail(monkeypatch, "card_image", two_fail)
    calls["verdict"] = _rows((0.9, 0.9, 0.9), (0.6, 0.6, 0.6))
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    _, out, picks = _load(run_dir)
    assert all(len(c.drafts) == 2 and c.chosen_index == 0 for c in out.cards) and len(picks) == len(out.cards)


def test_failed_music_falls_back_to_silence(fake, monkeypatch, no_sleep):
    run_dir, _ = fake
    _fail(monkeypatch, "music")
    assets.run(run_dir, load_run(run_dir).model_copy(update={"mode": "full"}))
    _, out, _ = _load(run_dir)
    assert out.music is not None and out.music.duration_s > 0 and (run_dir / "music.wav").exists()
    assert any("of silence (fal failed" in e for e in _events(run_dir))


# --- A16: illustrated panels from reference frames (Ideogram Vector, faked) ---

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 36" width="64" height="36"><rect width="64" height="36" fill="#3a7"/></svg>'


@pytest.fixture
def panel(fake, monkeypatch):
    """s01 (the fixture's card) gets reference frames; fal's vector calls write a tiny SVG."""
    run_dir, calls = fake
    calls.update(redraw=[], scene=[])
    (run_dir / "frames").mkdir()
    for i in range(3):
        shutil.copyfile(DRAFTS[i], run_dir / "frames" / f"f{i}.png")

    def set_frames(frames):
        script = Script.model_validate_json((run_dir / "script.json").read_text(encoding="utf-8"))
        script.shots[0].visual.reference_frames = frames
        (run_dir / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

    def fake_redraw(image, out_path):
        calls["redraw"].append(Path(image).name)
        Path(out_path).write_text(SVG, encoding="utf-8")

    def fake_scene(prompt, refs, out_paths, seed):
        calls["scene"].append((prompt, [Path(r).name for r in refs], len(out_paths), seed))
        for o in out_paths:
            Path(o).write_text(SVG, encoding="utf-8")

    monkeypatch.setattr(fal, "vector_redraw", fake_redraw)
    monkeypatch.setattr(fal, "vector_scene", fake_scene)
    return run_dir, calls, set_frames


FULL = {"mode": "full"}


def test_one_frame_also_makes_a_scene_not_a_redraw(panel):
    run_dir, calls, set_frames = panel
    set_frames(["frames/f0.png"])
    assets.run(run_dir, load_run(run_dir).model_copy(update=FULL))
    _, out, picks = _load(run_dir)
    card = out.cards[0]
    assert [c[1] for c in calls["scene"]] == [["f0.png"]] and calls["redraw"] == [] and calls["card"] == []
    assert len(card.drafts) == 4 and card.chosen_index == 2
    from PIL import Image
    assert Image.open(run_dir / card.path).size == ff.size_for("full")  # render reads a full-size PNG
    assert [d.subject for d in picks] == ["s01"]


def test_several_frames_make_a_scene_best_of_4(panel):
    run_dir, calls, set_frames = panel
    set_frames(["frames/f0.png", "frames/f1.png", "frames/f2.png"])
    cfg = load_run(run_dir).model_copy(update=FULL)
    assets.run(run_dir, cfg)
    _, out, picks = _load(run_dir)
    (prompt, refs, n, seed), = calls["scene"]
    assert refs == ["f0.png", "f1.png", "f2.png"] and n == 4 and seed == assets.PANEL_SEED
    assert assets.FAL_STYLE in prompt and calls["redraw"] == [] and calls["card"] == []
    assert len(out.cards[0].drafts) == 4 and out.cards[0].chosen_index == 2 and len(picks) == 1
    assets.run(run_dir, cfg)  # second run: the panel comes from the cache
    assert len(calls["scene"]) == 1


def test_frames_missing_on_disk_keep_the_old_card_path(panel):
    run_dir, calls, set_frames = panel
    set_frames(["frames/nope.png"])
    assets.run(run_dir, load_run(run_dir).model_copy(update=FULL))
    assert calls["redraw"] == calls["scene"] == [] and len(calls["card"]) == 4


def test_failed_panel_falls_back_to_plain_card(panel, monkeypatch, no_sleep):
    run_dir, calls, set_frames = panel
    set_frames(["frames/f0.png", "frames/f1.png"])
    _fail(monkeypatch, "vector_scene")
    assets.run(run_dir, load_run(run_dir).model_copy(update=FULL))
    _, out, picks = _load(run_dir)
    assert out.cards[0].drafts == ["cards/drafts/s01_plain.png"] and picks == [] and calls["card"] == []
    assert sum("s01: every fal draft failed, plain card" in e for e in _events(run_dir)) == 1


@pytest.mark.parametrize("why", ["cheap", "no FAL_KEY"])
def test_panels_never_call_fal_in_cheap_mode_or_without_key(panel, monkeypatch, why):
    run_dir, calls, set_frames = panel
    set_frames(["frames/f0.png", "frames/f1.png"])
    if why == "no FAL_KEY":
        monkeypatch.delenv("FAL_KEY")
    cfg = load_run(run_dir) if why == "cheap" else load_run(run_dir).model_copy(update=FULL)
    assets.run(run_dir, cfg)
    _, out, _ = _load(run_dir)
    assert calls["redraw"] == calls["scene"] == [] and out.cards[0].drafts == ["cards/drafts/s01_0.png"]
    assert any(f"plain card ({why})" in e for e in _events(run_dir))
