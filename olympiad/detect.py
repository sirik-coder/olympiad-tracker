"""Deciding which moves are worth waking someone up for.

A correction worth recording, because it drove the design here. The broadcast
PGN carries Lichess's own evaluation after every move - but only once a game is
over. While a game is still being played its moves arrive with clock times and
nothing else. Checked against two other live broadcasts: every in-progress game
had zero evaluations, every finished one had them on almost every move.

So for live games the engine is not an optional extra used to confirm a
brilliancy. It is where every number comes from. Finished games (and therefore
backtests) still get the feed's evaluations for free.

That makes the cost of the job the thing to design around, and the work is done
in three passes:

  screen   Every new move, at shallow depth. Enough to tell "still equal" from
           "now lost", cheap enough to run on a couple of hundred positions
           inside one poll. Positions are cached, because the position after
           one move is the position before the next.

  verify   Only for moves the screen flagged, at full depth. Shallow searches
           are noisy, and a false blunder alert is worse than a missed one:
           it goes out to a Slack channel that people are meant to trust.

  confirm  Only for brilliancy candidates, at full depth with the top two
           moves. A brilliancy is not a sacrifice that worked, it is a
           sacrifice that was the only thing that worked, and knowing that
           needs the runner-up move as well as the best one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import chess

from .engine import material_swing
from .evalscale import describe_position, format_score, score_to_winning_chances

# Lichess puts its machine-readable bits in [%eval ...] / [%clk ...] and its
# prose in the rest of the comment. We only want the prose.
BRACKET_RE = re.compile(r"\[%[^\]]*\]")
BEST_MOVE_RE = re.compile(r"([A-Za-z][A-Za-z0-9+#=\-]{1,7})\s+was best", re.I)

BLUNDER = "blunder"
BRILLIANCY = "brilliancy"

# On the first sight of a game, do not re-analyse it from move one. In normal
# running the first poll of the day happens before the clocks start, so this
# only bites after a missed run, where analysing forty half-moves of catch-up
# per game would eat the whole budget.
DEFAULT_MAX_BACKFILL_PLIES = 40


@dataclass
class Finding:
    """One moment worth alerting on."""

    kind: str                 # BLUNDER or BRILLIANCY
    subkind: str              # threw_game / threw_win / only_move_sac
    game: object              # pgnfeed.Game
    move: object              # pgnfeed.MoveRecord
    mover: bool               # chess.WHITE / chess.BLACK
    chances_before: float
    chances_after: float
    eval_before_text: str     # always from White's point of view
    eval_after_text: str
    better_move: str = ""     # what should have been played
    sacrifice_cp: int = 0     # material given up, for a brilliancy
    only_move_gap: float = 0.0
    engine_confirmed: bool = False
    detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable id, so the same move is never alerted twice."""
        return "%s:%d:%s" % (self.game.game_id, self.move.ply, self.kind)

    @property
    def drop(self) -> float:
        return self.chances_before - self.chances_after

    @property
    def player(self):
        return self.game.player(self.mover)

    @property
    def opponent(self):
        return self.game.opponent(self.mover)


def clean_comment(comment: str) -> str:
    """Lichess's own words about a move, with the bracket codes removed."""
    return " ".join(BRACKET_RE.sub("", comment or "").split()).strip()


def _better_move_from_comment(comment: str) -> str:
    match = BEST_MOVE_RE.search(clean_comment(comment))
    return match.group(1) if match else ""


def _plain_blunder_detail(finding: Finding) -> str:
    was = describe_position(finding.chances_before)
    name = finding.player.short
    other = finding.opponent.short
    if finding.subkind == "threw_win":
        return ("%s was %s, then played %s. The position is now %s for %s."
                % (name, was, finding.move.san,
                   describe_position(finding.chances_after), name))
    return ("%s was %s. After %s, %s is %s."
            % (name, was, finding.move.san, other,
               describe_position(100.0 - finding.chances_after)))


def _plain_brilliancy_detail(finding: Finding) -> str:
    return ("%s gave up about %.1f pawns of material with %s, and the engine "
            "says it is the only move that works - everything else is %.0f "
            "points worse."
            % (finding.player.short, finding.sacrifice_cp / 100.0,
               finding.move.san, finding.only_move_gap))


class Detector:
    def __init__(self, thresholds, analyst=None, alert_both_sides: bool = False,
                 max_backfill_plies: int = 0):
        self.thresholds = thresholds
        self.analyst = analyst
        # By default we only alert on moves played *by* someone on the watch
        # list. Turning this on also alerts when their opponent blunders, which
        # roughly doubles the volume and brings in weaker players' mistakes.
        self.alert_both_sides = alert_both_sides
        self.max_backfill_plies = max_backfill_plies
        # (fen, deep) -> Score from the point of view of the side to move.
        self._cache = {}

    def reset_cache(self):
        self._cache = {}

    # -- evaluations --------------------------------------------------------

    def _engine_score(self, board: chess.Board, deep: bool):
        """Engine score for `board`, from the side to move. None if we cannot.

        Cached, because the position after one move is the position before the
        next one, so a run of moves costs one search per move rather than two.
        A deep result also answers a later shallow request: it is strictly
        better information.
        """
        if not (self.analyst and self.analyst.available):
            return None
        fen = board.board_fen() + " " + ("w" if board.turn else "b") + \
            " " + board.castling_xfen() + " " + str(board.ep_square)

        if (fen, True) in self._cache:
            return self._cache[(fen, True)]
        if not deep and (fen, False) in self._cache:
            return self._cache[(fen, False)]

        lines = self.analyst.top_moves(board, count=1, deep=deep)
        if not lines:
            return None
        score = lines[0].score
        self._cache[(fen, deep)] = score
        return score

    def _resolve(self, move, deep: bool):
        """Winning chances before and after `move`, from the mover's side.

        Returns (before, after, before_text, after_text) or None. The feed's
        own numbers win when they exist, which on a live game they do not.
        """
        mover = move.side
        before = after = None
        before_text = after_text = "?"

        if move.eval_before is not None:
            before = score_to_winning_chances(move.eval_before.pov(mover))
            before_text = format_score(move.eval_before.white())
        elif move.ply == 1:
            before, before_text = 50.0, "+0.20"

        if move.eval_after is not None:
            after = score_to_winning_chances(move.eval_after.pov(mover))
            after_text = format_score(move.eval_after.white())

        if before is None:
            score = self._engine_score(move.board_before, deep)
            if score is None:
                return None
            before = score_to_winning_chances(score)
            before_text = format_score(score if mover == chess.WHITE else -score)

        if after is None:
            # This score is from the side to move *after* the move, which is
            # the opponent. Flip it to get the mover's view.
            score = self._engine_score(move.board_after, deep)
            if score is None:
                return None
            after = 100.0 - score_to_winning_chances(score)
            after_text = format_score(-score if mover == chess.WHITE else score)

        return before, after, before_text, after_text

    # -- rules --------------------------------------------------------------

    def _blunder_subkind(self, before: float, after: float):
        t = self.thresholds
        if before - after < t.blunder_min_drop:
            return None
        # Was at least equal, is now losing. The classic one.
        if before >= t.blunder_was_at_least and after <= t.blunder_now_at_most:
            return "threw_game"
        # Was winning, is not any more. Not losing, but the win is gone, and
        # that is just as much of a story.
        if before >= 80.0 and after <= 55.0:
            return "threw_win"
        return None

    def _confirm_brilliancy(self, move):
        """Ask Stockfish whether this was the only move that worked.

        Without an engine we return (False, 0) and stay quiet: better silence
        than calling something brilliant on a guess.
        """
        if not (self.analyst and self.analyst.available):
            return False, 0.0
        lines = self.analyst.top_moves(move.board_before, count=2, deep=True)
        if not lines or lines[0].move != move.move:
            return False, 0.0      # the engine would have played something else
        if len(lines) < 2:
            return True, 100.0     # no second move at all: forced
        gap = (score_to_winning_chances(lines[0].score)
               - score_to_winning_chances(lines[1].score))
        return gap >= self.thresholds.brilliancy_min_only_move_gap, gap

    # -- the pass over one game --------------------------------------------

    def _start_ply(self, game, from_ply: int) -> int:
        """Where to begin reading, respecting the catch-up limit."""
        if from_ply or not self.max_backfill_plies or not game.moves:
            return from_ply
        highest = game.moves[-1].ply
        if highest <= self.max_backfill_plies:
            return 0
        return highest - self.max_backfill_plies

    def scan_game(self, game, from_ply: int = 0):
        """Findings in `game` after `from_ply`.

        Returns (findings, processed_to_ply). The second value matters: if the
        engine budget runs out mid-game we must not record the unread moves as
        read, or they would be skipped forever and the blunder in them would
        never be seen.
        """
        t = self.thresholds
        findings = []
        start = self._start_ply(game, from_ply)
        processed_to = start

        for move in game.moves:
            if move.ply <= start:
                continue

            side_key = "white" if move.side == chess.WHITE else "black"
            watched = side_key in game.watch_reasons
            if not self.alert_both_sides and not watched:
                # Nothing to decide about this move, but we have still read it.
                processed_to = move.ply
                continue

            screened = self._resolve(move, deep=False)
            if screened is None:
                # Out of engine budget, or no engine at all. Stop here rather
                # than marking the rest of the game as seen.
                break
            before, after, before_text, after_text = screened

            subkind = self._blunder_subkind(before, after)
            looks_sacrificial = (
                after >= t.brilliancy_min_winning_chances
                and before - after <= t.brilliancy_max_drop
                and before < t.brilliancy_max_before
                and material_swing(move.board_before, move.move)
                <= -t.brilliancy_min_sacrifice
            )

            if subkind or looks_sacrificial:
                # Shallow searches are noisy. Before anything reaches Slack,
                # look again properly.
                checked = self._resolve(move, deep=True)
                if checked is not None:
                    before, after, before_text, after_text = checked
                    subkind = self._blunder_subkind(before, after)
                    looks_sacrificial = (
                        after >= t.brilliancy_min_winning_chances
                        and before - after <= t.brilliancy_max_drop
                        and before < t.brilliancy_max_before
                        and material_swing(move.board_before, move.move)
                        <= -t.brilliancy_min_sacrifice
                    )

            processed_to = move.ply

            if subkind:
                finding = Finding(
                    kind=BLUNDER, subkind=subkind, game=game, move=move,
                    mover=move.side, chances_before=before, chances_after=after,
                    eval_before_text=before_text, eval_after_text=after_text,
                    better_move=_better_move_from_comment(move.nag_text),
                    engine_confirmed=True,
                )
                if not finding.better_move and self.analyst and self.analyst.available:
                    lines = self.analyst.top_moves(move.board_before, count=1,
                                                   deep=True)
                    if lines:
                        finding.better_move = lines[0].san
                finding.detail = _plain_blunder_detail(finding)
                findings.append(finding)
                continue

            # A blunder and a brilliancy are mutually exclusive.
            if not looks_sacrificial:
                continue

            confirmed, gap = self._confirm_brilliancy(move)
            if not confirmed:
                continue

            finding = Finding(
                kind=BRILLIANCY, subkind="only_move_sac", game=game, move=move,
                mover=move.side, chances_before=before, chances_after=after,
                eval_before_text=before_text, eval_after_text=after_text,
                sacrifice_cp=-material_swing(move.board_before, move.move),
                only_move_gap=gap, engine_confirmed=True,
            )
            finding.detail = _plain_brilliancy_detail(finding)
            findings.append(finding)

        return findings, processed_to
