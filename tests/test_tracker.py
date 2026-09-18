#!/usr/bin/env python3
"""Checks on the parts that would fail quietly.

Run them with `python tests/test_tracker.py` or with `pytest`. They use no
network and no engine, so they finish in about a second.

The things worth testing here are the ones where a bug produces a plausible
wrong answer rather than a crash: an evaluation scale that makes a won game
look like a blunder, a sacrifice test that cannot tell a sacrifice from a
capture, a caption that quietly starts saying "platform".
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess  # noqa: E402
import chess.engine  # noqa: E402

from olympiad.captions import BANNED_SPELLINGS, BANNED_WORDS, lint_caption  # noqa: E402
from olympiad.config import WatchList, normalize_name  # noqa: E402
from olympiad.engine import material_swing  # noqa: E402
from olympiad.evalscale import (  # noqa: E402
    cp_to_winning_chances, format_score, score_to_winning_chances)
from olympiad.pgnfeed import _boards_from_round_tag, parse_round_pgn  # noqa: E402
from olympiad.state import State  # noqa: E402


# --- the evaluation scale --------------------------------------------------

def test_equality_is_fifty():
    assert abs(cp_to_winning_chances(0) - 50.0) < 0.01


def test_a_won_game_getting_more_won_is_not_a_swing():
    """The whole reason we do not threshold on centipawns."""
    quiet = cp_to_winning_chances(1200) - cp_to_winning_chances(500)   # 700 cp
    decisive = cp_to_winning_chances(0) - cp_to_winning_chances(-300)  # 300 cp
    assert quiet < decisive, "a 700cp swing in a won game outranked a real one"
    assert quiet < 15


def test_mate_scores_do_not_need_special_cases():
    assert score_to_winning_chances(chess.engine.Mate(3)) == 100.0
    assert score_to_winning_chances(chess.engine.Mate(-1)) == 0.0


def test_score_formatting():
    assert format_score(chess.engine.Cp(135)) == "+1.35"
    assert format_score(chess.engine.Cp(-42)) == "-0.42"
    assert format_score(chess.engine.Mate(4)) == "M4"
    assert format_score(chess.engine.Mate(-2)) == "-M2"


# --- the sacrifice test ----------------------------------------------------

def test_a_quiet_move_gives_up_nothing():
    assert material_swing(chess.Board(), chess.Move.from_uci("e2e4")) == 0


def test_bishop_for_a_pawn_reads_as_a_sacrifice():
    board = chess.Board(
        "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4")
    swing = material_swing(board, chess.Move.from_uci("c4f7"))
    assert swing < -200, "Bxf7+ should read as giving up roughly a bishop for a pawn"


def test_winning_a_free_pawn_is_not_a_sacrifice():
    board = chess.Board(
        "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    assert material_swing(board, chess.Move.from_uci("f3e5")) > 0


def test_a_defended_square_is_not_a_free_capture():
    """Taking a defended pawn with a knight loses material, and must read that
    way, or every recapture in the event becomes a brilliancy."""
    # After 1.e4 e5 2.Nf3 Nc6, the e5 pawn is held by the knight on c6, so
    # Nxe5 is knight for pawn, not a free pawn.
    board = chess.Board(
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    assert material_swing(board, chess.Move.from_uci("f3e5")) < 0


# --- the watch list --------------------------------------------------------

def test_names_survive_different_spellings_and_word_order():
    assert normalize_name("Sargissian, Gabriel") != normalize_name("Gabriel Sargsyan"), \
        "these really are different names; only the FIDE id ties them together"
    assert normalize_name("Laohawirapap, Prin") == normalize_name("Prin Laohawirapap")
    assert normalize_name("Martirosyan, Haik M.") == normalize_name("haik m martirosyan")


def test_the_open_and_womens_events_have_different_floors():
    wl = WatchList.load()
    woman = {"name": "Assaubayeva, Bibisara", "rating": 2536,
             "fide_id": 13709437, "team": "Kazakhstan"}
    assert wl.match(woman, women_event=True), \
        "the strongest woman in the event must not be filtered out"
    assert not wl.match(woman, women_event=False)


def test_the_named_list_beats_the_rating_floor():
    wl = WatchList.load()
    prin = {"name": "Laohawirapap, Prin", "rating": 2363,
            "fide_id": 6205003, "team": "Thailand"}
    assert wl.match(prin), "Prin is rated below the floor and must still match"


def test_an_unknown_amateur_is_ignored():
    wl = WatchList.load()
    assert wl.match({"name": "Nobody, A", "rating": 1850,
                     "fide_id": 999, "team": "Nowhere"}) is None


# --- PGN details -----------------------------------------------------------

def test_board_number_folds_back_into_one_to_four():
    """The Round header counts boards across the whole event, not the match."""
    assert _boards_from_round_tag("2.169") == (169, 1)
    assert _boards_from_round_tag("2.170") == (170, 2)
    assert _boards_from_round_tag("2.172") == (172, 4)
    assert _boards_from_round_tag("2.173") == (173, 1)
    assert _boards_from_round_tag("") == (0, 0)


SAMPLE_PGN = """[Event "Olymp 2026 Open"]
[Round "2.169"]
[White "Test, Player"]
[Black "Other, Person"]
[Result "*"]
[WhiteElo "2700"]
[WhiteTitle "GM"]
[WhiteTeam "Testland"]
[WhiteFideId "12345"]
[BlackElo "2500"]
[BlackTeam "Otherland"]
[BlackFideId "67890"]
[GameURL "https://lichess.org/broadcast/x/round-2/HnCuRMmB/EQVqQ7iM"]

1. e4 { [%eval 0.18] [%clk 1:30:48] } 1... e5 { [%eval 0.22] } 2. Qh5?? { [%eval -3.5] } { Blunder. Nf3 was best. } *
"""


def test_the_pgn_gives_us_everything_we_need():
    games = parse_round_pgn(SAMPLE_PGN, "Round 2", "Open | Matches 1-12")
    assert len(games) == 1
    game = games[0]
    assert game.game_id == "EQVqQ7iM"
    assert game.white.rating == 2700 and game.white.fide_id == 12345
    assert game.white.short == "Player Test"
    assert game.board == 1
    assert game.is_live, "a game with result * is still being played"
    assert len(game.moves) == 3

    last = game.moves[-1]
    assert last.numbered_san == "2.Qh5"
    assert last.eval_before is not None and last.eval_after is not None
    assert "Nf3 was best" in last.nag_text


def test_detection_finds_the_obvious_blunder():
    from olympiad.config import Thresholds
    from olympiad.detect import BLUNDER, Detector

    games = parse_round_pgn(SAMPLE_PGN, "Round 2", "Open | Matches 1-12")
    game = games[0]
    game.watch_reasons = {"white": "rated 2700"}
    findings = Detector(Thresholds(), analyst=None).scan_game(game)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == BLUNDER
    assert finding.move.san == "Qh5"
    assert finding.better_move == "Nf3", "should be read out of Lichess's own comment"


def test_moves_already_seen_are_not_re_reported():
    from olympiad.config import Thresholds
    from olympiad.detect import Detector

    games = parse_round_pgn(SAMPLE_PGN, "Round 2", "Open | Matches 1-12")
    game = games[0]
    game.watch_reasons = {"white": "rated 2700"}
    detector = Detector(Thresholds(), analyst=None)
    assert detector.scan_game(game, from_ply=0)
    assert not detector.scan_game(game, from_ply=3), "ply 3 was already read"


def test_we_stay_quiet_about_the_opponents_moves_by_default():
    from olympiad.config import Thresholds
    from olympiad.detect import Detector

    games = parse_round_pgn(SAMPLE_PGN, "Round 2", "Open | Matches 1-12")
    game = games[0]
    game.watch_reasons = {"black": "rated 2500"}   # we watch Black, White blunders
    assert not Detector(Thresholds(), analyst=None).scan_game(game)
    assert Detector(Thresholds(), analyst=None, alert_both_sides=True).scan_game(game)


# --- captions --------------------------------------------------------------

def test_every_caption_template_stays_on_brand():
    """Renders every template with real-looking values and lints the result."""
    from olympiad.captions import _BRILLIANCY, _THREW_GAME, _THREW_WIN

    fields = {
        "name": "Haik Martirosyan", "name_last": "Martirosyan",
        "rating_phrase": "A 2600", "rating": 2664, "move": "Nf5", "move_no": 21,
        "board": 1, "eval_before": "+1.78", "eval_after": "-4.61",
        "opponent": "Ilja Sirosh", "team": "Armenia", "material": "a rook",
    }
    for template in _THREW_GAME + _THREW_WIN + _BRILLIANCY:
        text = template.format(**fields)
        assert not lint_caption(text), "off-brand words in: %s" % text[:60]
        assert 40 < len(text) < 420, "caption is the wrong length: %d" % len(text)
        assert text.count("\n") >= 1, "every caption is a hook plus a line or two"


def test_the_linter_actually_catches_things():
    assert "platform" in lint_caption("Our platform helps you improve")
    assert "centre" in lint_caption("Control the centre")
    assert lint_caption("A GM-guided path built around your gaps.") == []


def test_banned_lists_are_lowercase():
    for word in BANNED_WORDS + BANNED_SPELLINGS:
        assert word == word.lower(), "%r would never match" % word


# --- state -----------------------------------------------------------------

def test_state_survives_a_round_trip(tmp_path=None):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "seen.json"
        state = State(path)
        state.set_last_ply("rnd1", "gameA", 42)
        state.mark_alerted("gameA:42:blunder")
        state.save()

        reloaded = State(path)
        assert reloaded.last_ply("rnd1", "gameA") == 42
        assert reloaded.already_alerted("gameA:42:blunder")
        assert not reloaded.already_alerted("gameA:43:blunder")
        assert reloaded.last_ply("rnd1", "unknown") == 0


def test_a_corrupt_state_file_does_not_stop_the_job():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "seen.json"
        path.write_text("{not json at all", encoding="utf-8")
        state = State(path)
        assert state.last_ply("r", "g") == 0


# --- slack -----------------------------------------------------------------

def test_slack_is_dry_by_default_and_never_prints_the_url():
    from olympiad.slack import Slack

    saved = os.environ.pop("SLACK_WEBHOOK_URL", None)
    try:
        assert Slack().dry_run, "with no webhook set, nothing may be sent"
    finally:
        if saved is not None:
            os.environ["SLACK_WEBHOOK_URL"] = saved


def test_the_slack_message_has_the_things_the_brief_asked_for():
    from olympiad.config import Thresholds
    from olympiad.detect import Detector
    from olympiad.slack import build_message

    games = parse_round_pgn(SAMPLE_PGN, "Round 2", "Open | Matches 1-12")
    game = games[0]
    game.watch_reasons = {"white": "rated 2700"}
    finding = Detector(Thresholds(), analyst=None).scan_game(game)[0]

    payload = build_message(finding)
    blob = str(payload)
    for needed in ["Test, Player", "Testland", "Board 1", "Qh5",
                   "lichess.org", "Caption draft"]:
        assert needed in blob, "the Slack message is missing %r" % needed


# --- runner ----------------------------------------------------------------

def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print("  ok    %s" % name)
        except AssertionError as exc:
            failures.append((name, exc))
            print("  FAIL  %s\n          %s" % (name, exc))
        except Exception as exc:
            failures.append((name, exc))
            print("  ERROR %s\n          %s: %s" % (name, type(exc).__name__, exc))
    print("\n%d passed, %d failed, out of %d"
          % (len(tests) - len(failures), len(failures), len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
