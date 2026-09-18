#!/usr/bin/env python3
"""Look up a player in the live broadcast: FIDE id, rating, team, group.

Use this before adding anyone to watchlist.yml. Names are spelled differently
in different places - Lichess writes "Sargissian, Gabriel" for the player you
might write down as Gabriel Sargsyan - so copy the FIDE id it prints rather
than typing the name out.

Usage:
    python tools/find_player.py Sargsyan
    python tools/find_player.py "Laohawirapap"
    python tools/find_player.py --team Armenia
    python tools/find_player.py --team Thailand --round "Round 2"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from olympiad.config import normalize_name  # noqa: E402
from olympiad.lichess import Lichess  # noqa: E402
from olympiad.pgnfeed import parse_round_pgn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default="",
                        help="part of a player's name")
    parser.add_argument("--team", default="", help="a team name instead")
    parser.add_argument("--round", dest="round_name", default="",
                        help='which round to read (default: the latest finished)')
    args = parser.parse_args()

    if not args.query and not args.team:
        parser.error("give a name to search for, or --team")

    lichess = Lichess()

    round_name = args.round_name
    if not round_name:
        # Pick the most recent round that has actually been played, so there
        # are games to read.
        tours = lichess.group_tours()
        if not tours:
            print("Could not reach Lichess.")
            return 1
        rounds = lichess.rounds(tours[0][0], tours[0][1])
        played = [r for r in rounds if r.finished] or rounds[:1]
        round_name = played[-1].name
    print("Reading %s across all nine groups ...\n" % round_name)

    needle = normalize_name(args.query) if args.query else ""
    team_needle = args.team.strip().lower()
    hits = []

    for rnd in lichess.round_by_name(round_name):
        games = parse_round_pgn(lichess.round_pgn(rnd.id), rnd.name, rnd.tour_name)
        for game in games:
            for player, opponent in ((game.white, game.black),
                                     (game.black, game.white)):
                name_key = normalize_name(player.name)
                matched = False
                if needle:
                    matched = all(part in name_key.split()
                                  for part in needle.split())
                if team_needle and (player.team or "").lower() == team_needle:
                    matched = True
                if matched:
                    hits.append((player, opponent, game))

    if not hits:
        print("Nobody matched.")
        return 1

    hits.sort(key=lambda h: (h[0].team or "", h[2].board))
    print("%-34s %-6s %-9s %-22s %-6s %s"
          % ("PLAYER", "RATING", "FIDE ID", "GROUP", "BOARD", "TEAM"))
    print("-" * 100)
    for player, opponent, game in hits:
        print("%-34s %-6d %-9d %-22s %-6d %s"
              % (player.display[:34], player.rating, player.fide_id,
                 game.tour_name[:22], game.board, player.team))

    print()
    print("Paste into watchlist.yml under always_watch_players:")
    seen = set()
    for player, _, _ in hits:
        if player.fide_id in seen:
            continue
        seen.add(player.fide_id)
        print('  - name: "%s"' % player.name)
        print("    fide_id: %d" % player.fide_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
