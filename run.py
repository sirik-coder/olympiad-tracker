#!/usr/bin/env python3
"""Watch the live Olympiad games and post alerts to Slack.

One run does several polls rather than one, and here is why. GitHub's scheduler
will not run a job more often than every five minutes, and under load it often
starts a scheduled job ten or twenty minutes late. If each run looked once and
exited, "real time" would quietly become "sometime within half an hour". So the
workflow starts a run every fifteen minutes, and each run looks every two and a
half minutes until its slot is nearly up. Late starts then cost a little
overlap instead of a gap.

Usage:
    python run.py                     # poll for the default window
    python run.py --once              # a single poll, then exit
    python run.py --minutes 12        # poll for twelve minutes
    python run.py --dry-run           # print alerts instead of sending them
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from olympiad.config import Thresholds, WatchList
from olympiad.detect import Detector
from olympiad.engine import Analyst, find_stockfish
from olympiad.lichess import Lichess
from olympiad.pgnfeed import parse_round_pgn, select_watched
from olympiad.slack import Slack
from olympiad.state import State

# Measured on the first live round: one sweep of all nine groups takes about
# 80 seconds, so this is the gap on top of that, not the cycle time. 90 gives a
# cycle of roughly three and a half minutes, inside the "every few minutes"
# this is meant to deliver, without hammering Lichess.
POLL_SECONDS = 90
DEFAULT_MINUTES = 20

# On first sight of a game, how far back to read. Zero means the whole game.
#
# This was 40 half-moves, to stop a cold start eating the engine budget. On
# Round 4 that turned out to be the wrong trade. GitHub started the morning run
# three hours and forty-five minutes late, by which point the top boards were
# fifty moves deep, and the limit meant the first three hours of every game
# were not analysed - not found clean, never looked at. Every alert that round
# came from the one section whose decisive moments happened to fall inside the
# last forty half-moves.
#
# Reading the whole game costs one slow poll at the start of a late run and
# nothing thereafter, because the state file remembers where it got to.
MAX_BACKFILL_PLIES = 0

# How many polls in a row may find no live round before the run gives up. A
# run that starts after the round has finished should not sit there for hours.
EMPTY_POLLS_BEFORE_GIVING_UP = 3


def log(message: str):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def _utc_time_today(hhmm: str):
    """Turn "18:30" into a unix timestamp for that time today, UTC.

    Returns None if it cannot be read, so a typo in the workflow degrades to
    "run for the full length" rather than crashing the round.
    """
    try:
        hours, minutes = (int(part) for part in hhmm.strip().split(":"))
    except (ValueError, TypeError):
        return None
    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return target.timestamp()


def poll_once(lichess, watchlist, detector, slack, state) -> int:
    """One sweep of every live group. Returns how many alerts were sent."""
    rounds = lichess.current_rounds()
    if not rounds:
        log("no round is live right now")
        return None   # distinct from 0 alerts: there was nothing to look at

    alerts = 0
    for rnd in rounds:
        pgn = lichess.round_pgn(rnd.id)
        if not pgn.strip():
            continue

        games = parse_round_pgn(pgn, rnd.name, rnd.tour_name)
        watched = select_watched(games, watchlist)
        if not watched:
            continue

        new_findings = []
        for game in watched:
            seen_to = state.last_ply(rnd.id, game.game_id)
            highest = game.moves[-1].ply if game.moves else 0
            if highest <= seen_to:
                continue
            found, processed_to = detector.scan_game(game, from_ply=seen_to)
            for finding in found:
                if not state.already_alerted(finding.key):
                    new_findings.append(finding)
            # Record how far we actually got, not how far the game has gone.
            # If the engine budget ran out mid-game, the rest is read next poll
            # instead of being skipped for good.
            state.set_last_ply(rnd.id, game.game_id, processed_to)

        log("%-22s %-9s %3d games, %2d watched, %d new"
            % (rnd.tour_name, rnd.name, len(games), len(watched), len(new_findings)))

        # Oldest moment first, so the channel reads in the order things happened.
        for finding in sorted(new_findings, key=lambda f: f.move.ply):
            posted = slack.post(finding)
            # Only write it off as delivered if it really was. A dry run must
            # not use up an alert: on the first live round the webhook was not
            # set yet, and seven real findings were quietly marked as sent and
            # could never be posted again.
            if posted and not slack.dry_run:
                state.mark_alerted(finding.key)
            if posted:
                alerts += 1

    state.save()
    return alerts


def replay_round(round_name: str, lichess, watchlist, detector, slack) -> int:
    """Post a finished round's findings to Slack, to show what alerts look like.

    Deliberately does not touch the state file. A replay is a demonstration,
    not a record: it must not mark anything as already alerted, and it must not
    move the marker for a round that is still being played.
    """
    rounds = lichess.round_by_name(round_name)
    if not rounds:
        log("no round called %r was found" % round_name)
        return 1

    findings = []
    for rnd in rounds:
        games = parse_round_pgn(lichess.round_pgn(rnd.id), rnd.name, rnd.tour_name)
        watched = select_watched(games, watchlist)
        for game in watched:
            found, _ = detector.scan_game(game)
            findings += found
        log("%-24s %3d games, %2d watched" % (rnd.tour_name, len(games), len(watched)))

    findings.sort(key=lambda f: (f.game.tour_name, f.game.board, f.move.ply))
    log("replaying %d findings from %s" % (len(findings), round_name))

    slack.post_text(
        ":rewind: *Replay of %s* - this round is already finished. "
        "These are the alerts the tracker would have posted while it was being "
        "played, shown so you can see the format before a live round."
        % round_name)
    for finding in findings:
        slack.post(finding)
    slack.post_text(":white_check_mark: End of %s replay. Live alerts follow "
                    "automatically during the next round." % round_name)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", default="",
                        help='post a finished round to Slack, e.g. "Round 3"')
    parser.add_argument("--once", action="store_true", help="one poll, then exit")
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                        help="longest this run may poll for (default %d)"
                             % DEFAULT_MINUTES)
    parser.add_argument("--until", default="",
                        help="stop at this UTC time, e.g. 18:30. A run that "
                             "starts late must still stop when the round ends, "
                             "not run on for its full length into the night.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending them")
    parser.add_argument("--both-sides", action="store_true",
                        help="also alert when a watched player's opponent blunders")
    args = parser.parse_args()

    thresholds = Thresholds.from_env()
    watchlist = WatchList.load()
    state = State()
    slack = Slack(dry_run=args.dry_run or None)
    lichess = Lichess()

    stockfish = find_stockfish()
    log("watching players rated %d+, plus %d named, plus %d teams"
        % (watchlist.min_rating, len(watchlist.player_fide_ids), len(watchlist.teams)))
    log("stockfish: %s" % (stockfish or "NOT FOUND - brilliancy detection is off"))
    log("slack: %s" % ("dry run" if slack.dry_run else "live webhook"))
    if slack.dry_run and not args.dry_run:
        log("!" * 66)
        log("! SLACK_WEBHOOK_URL is not set, so nothing will be posted.")
        log("! Findings below are printed to this log only. To fix: repository")
        log("! Settings > Secrets and variables > Actions > New repository secret,")
        log("! named SLACK_WEBHOOK_URL.")
        log("!" * 66)
    log("state: %s" % state.summary())

    if args.replay:
        with Analyst(thresholds, stockfish) as analyst:
            # No backfill limit: a replay reads the whole game, not just the
            # part that arrived since the last poll.
            detector = Detector(thresholds, analyst=analyst,
                                alert_both_sides=args.both_sides)
            return replay_round(args.replay, lichess, watchlist, detector, slack)

    deadline = time.time() + args.minutes * 60
    if args.until:
        until_ts = _utc_time_today(args.until)
        if until_ts is None:
            log("could not read --until %r, ignoring it" % args.until)
        else:
            deadline = min(deadline, until_ts)
            if deadline <= time.time():
                # Queued behind an earlier run and only now got a machine, by
                # which point the round is over. Stop rather than watch nothing.
                log("it is past %s UTC, so this round is done. Nothing to do."
                    % args.until)
                return 0
            log("polling until %s UTC (%.0f minutes)"
                % (args.until, (deadline - time.time()) / 60.0))

    total = 0
    empty_polls = 0

    with Analyst(thresholds, stockfish) as analyst:
        detector = Detector(thresholds, analyst=analyst,
                            alert_both_sides=args.both_sides,
                            max_backfill_plies=MAX_BACKFILL_PLIES)
        while True:
            analyst.reset_budget()
            try:
                sent = poll_once(lichess, watchlist, detector, slack, state)
                if sent is None:
                    empty_polls += 1
                else:
                    empty_polls = 0
                    total += sent
            except Exception as exc:
                # One bad poll must not end the run: the next one is a couple
                # of minutes away and the tournament is still going.
                log("poll failed: %s: %s" % (type(exc).__name__, exc))

            if empty_polls >= EMPTY_POLLS_BEFORE_GIVING_UP:
                log("no round has been live for %d polls; stopping early"
                    % empty_polls)
                break
            if args.once or time.time() + POLL_SECONDS > deadline:
                break
            time.sleep(POLL_SECONDS)

    state.save()
    log("done: %d alerts sent this run" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
