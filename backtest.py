#!/usr/bin/env python3
"""Run the detector over a finished round and print what it would have posted.

This exists so the thresholds can be judged on real games before anything goes
to Slack. Nothing here can post: there is no Slack client in this file at all.

Usage:
    python backtest.py --round "Round 2"
    python backtest.py --round "Round 2" --sensitivity strict
    python backtest.py --round "Round 2" --compare        # counts per preset
    python backtest.py --round "Round 2" --no-engine      # blunders only, fast
    python backtest.py --round "Round 2" --group Open     # one half of the event
"""

from __future__ import annotations

import argparse
import sys
import time

from olympiad.captions import draft_caption, lint_caption
from olympiad.config import SENSITIVITY_PRESETS, Thresholds, WatchList
from olympiad.detect import BLUNDER, BRILLIANCY, Detector
from olympiad.engine import Analyst, find_stockfish
from olympiad.lichess import Lichess
from olympiad.pgnfeed import parse_round_pgn, select_watched


def load_round(lichess, round_name: str, group_filter: str):
    """Every watched game of `round_name`, across the groups we want."""
    rounds = lichess.round_by_name(round_name)
    if group_filter:
        needle = group_filter.strip().lower()
        rounds = [r for r in rounds if needle in r.tour_name.lower()]
    return rounds


def collect_games(lichess, rounds, watchlist):
    games, watched = [], []
    for rnd in rounds:
        pgn = lichess.round_pgn(rnd.id)
        parsed = parse_round_pgn(pgn, rnd.name, rnd.tour_name)
        keep = select_watched(parsed, watchlist)
        games += parsed
        watched += keep
        print("  %-22s %-9s %3d games, %2d watched"
              % (rnd.tour_name, rnd.name, len(parsed), len(keep)), flush=True)
    return games, watched


def show(finding, index: int):
    game = finding.game
    label = {"threw_game": "BLUNDER",
             "threw_win": "THREW AWAY A WIN",
             "only_move_sac": "BRILLIANCY"}.get(finding.subkind, finding.kind.upper())

    print()
    print("=" * 78)
    print("%d. %s   %s" % (index, label, finding.player.short))
    print("   %s  ·  %s  ·  Board %d  ·  %s vs %s"
          % (game.tour_name, game.round_name, game.board,
             finding.player.team, finding.opponent.team))
    print("   %s (%d)  vs  %s (%d)"
          % (game.white.display, game.white.rating,
             game.black.display, game.black.rating))
    print()
    print("   move    %s" % finding.move.numbered_san)
    print("   eval    %s -> %s   (winning chances %.0f%% -> %.0f%%, drop %.0f)"
          % (finding.eval_before_text, finding.eval_after_text,
             finding.chances_before, finding.chances_after, finding.drop))
    if finding.better_move:
        print("   best    %s" % finding.better_move)
    if finding.kind == BRILLIANCY:
        print("   sac     %.1f pawns, next best move %.0f points worse"
              % (finding.sacrifice_cp / 100.0, finding.only_move_gap))
    print()
    print("   %s" % finding.detail)
    print()
    caption = draft_caption(finding)
    print("   CAPTION DRAFT")
    for line in caption.split("\n"):
        print("     %s" % line)
    problems = lint_caption(caption)
    if problems:
        print("   ! off-brand words: %s" % problems)
    print()
    print("   %s" % game.url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", default="Round 2", help='e.g. "Round 2"')
    parser.add_argument("--sensitivity", default="balanced",
                        choices=sorted(SENSITIVITY_PRESETS))
    parser.add_argument("--group", default="", help='filter, e.g. "Open" or "Women"')
    parser.add_argument("--compare", action="store_true",
                        help="count findings for every preset, then stop")
    parser.add_argument("--no-engine", action="store_true",
                        help="skip Stockfish: blunders only, but much faster")
    parser.add_argument("--both-sides", action="store_true",
                        help="also count the opponents' blunders")
    parser.add_argument("--limit", type=int, default=0,
                        help="show at most this many findings")
    args = parser.parse_args()

    lichess = Lichess()
    watchlist = WatchList.load()

    print("Loading %s ..." % args.round)
    rounds = load_round(lichess, args.round, args.group)
    if not rounds:
        print("No round called %r was found." % args.round)
        return 1

    started = time.time()
    all_games, watched = collect_games(lichess, rounds, watchlist)
    print("\n%d games in the round, %d of them watched "
          "(players rated %d+, plus the named list)."
          % (len(all_games), len(watched), watchlist.min_rating))

    if args.compare:
        print("\nHow many alerts each setting would have produced:")
        print("  %-10s %-8s %s" % ("preset", "alerts", "rule"))
        for name, (drop, was, now) in sorted(SENSITIVITY_PRESETS.items()):
            t = Thresholds()
            t.blunder_min_drop, t.blunder_was_at_least, t.blunder_now_at_most = (
                drop, was, now)
            detector = Detector(t, analyst=None, alert_both_sides=args.both_sides)
            count = sum(len(detector.scan_game(g)) for g in watched)
            print("  %-10s %-8d drop %.0f+, from %.0f%%+, to %.0f%% or worse"
                  % (name, count, drop, was, now))
        print("\n(Blunders only. Brilliancy detection needs the engine; "
              "run without --compare to include it.)")
        return 0

    thresholds = Thresholds()
    drop, was, now = SENSITIVITY_PRESETS[args.sensitivity]
    thresholds.blunder_min_drop = drop
    thresholds.blunder_was_at_least = was
    thresholds.blunder_now_at_most = now
    # A backtest is not racing a live round, so let the engine look at as many
    # candidate positions as the round throws up.
    thresholds.max_engine_positions_per_poll = 100000

    stockfish = None if args.no_engine else find_stockfish()
    if not args.no_engine and not stockfish:
        print("\n! Stockfish was not found, so brilliancies cannot be checked.")
        print("  Blunders below are still correct.")

    with Analyst(thresholds, stockfish) as analyst:
        detector = Detector(thresholds, analyst=analyst,
                            alert_both_sides=args.both_sides)
        findings = []
        for game in watched:
            findings += detector.scan_game(game)

    findings.sort(key=lambda f: (f.kind != BRILLIANCY, -f.drop))
    blunders = [f for f in findings if f.kind == BLUNDER]
    brilliancies = [f for f in findings if f.kind == BRILLIANCY]

    print("\nSensitivity '%s': %d alerts - %d blunders, %d brilliancies. (%.0fs)"
          % (args.sensitivity, len(findings), len(blunders), len(brilliancies),
             time.time() - started))
    if analyst_count := getattr(analyst, "positions_analysed", 0):
        print("Engine looked at %d positions." % analyst_count)

    shown = findings[:args.limit] if args.limit else findings
    for index, finding in enumerate(shown, start=1):
        show(finding, index)

    print()
    print("=" * 78)
    print("%d alerts shown. Nothing was posted to Slack." % len(shown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
