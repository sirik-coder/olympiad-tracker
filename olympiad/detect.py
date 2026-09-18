"""Deciding which moves are worth waking someone up for.

The shape of the work, and why it is shaped this way:

A round has about 780 games. Even filtered down to the players we watch that is
still tens of games and thousands of moves, and running Stockfish over all of
them on a small cloud machine would take longer than the round. So the work is
done in two passes.

Pass one is free. Lichess already puts its own engine evaluation after almost
every move of the broadcast, so we read the evaluation before the move and
after it, convert both to winning chances, and look at the difference. That is
enough to find every blunder, and enough to throw away the 99% of moves that
cannot possibly be a brilliancy.

Pass two costs something, so it runs on a handful of moves. A brilliancy is not
merely a sacrifice that worked; it is a sacrifice that was the *only* way. To
know whether anything else also won, you need the best move and the second best
move in the same position, and the broadcast only gives us one number. That is
what our own Stockfish is for.

The same engine covers the gap when the feed's evaluation is missing, which
happens for a move or two at the live edge of a game.
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
        """Stable id so the same move is never alerted twice."""
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


def _chances(score_pov_white, mover: bool) -> float:
    """Winning chances for `mover`, given a PovScore from the feed."""
    return score_to_winning_chances(score_pov_white.pov(mover))


def _plain_blunder_detail(finding: Finding) -> str:
    was = describe_position(finding.chances_before)
    now = describe_position(100.0 - finding.chances_after)  # from opponent's side
    name = finding.player.short
    other = finding.opponent.short
    if finding.subkind == "threw_win":
        return ("%s was %s, then played %s. The position is now %s for %s."
                % (name, was, finding.move.san, describe_position(finding.chances_after),
                   name))
    return ("%s was %s. After %s, %s is %s."
            % (name, was, finding.move.san, other, now))


def _plain_brilliancy_detail(finding: Finding) -> str:
    material = finding.sacrifice_cp / 100.0
    return ("%s gave up about %.1f pawns of material with %s, and the engine says "
            "it is the only move that works - everything else is %.0f points worse."
            % (finding.player.short, material, finding.move.san, finding.only_move_gap))


class Detector:
    def __init__(self, thresholds, analyst=None, alert_both_sides: bool = False):
        self.thresholds = thresholds
        self.analyst = analyst
        # By default we only alert on moves played *by* someone on the watch
        # list. Turn this on to also alert when their opponent blunders, which
        # roughly doubles the volume and brings in weaker players' mistakes.
        self.alert_both_sides = alert_both_sides

    # -- evaluations --------------------------------------------------------

    def _resolve_evals(self, move):
        """Winning chances before and after this move, from the mover's side.

        Prefers the broadcast's own numbers. Falls back to our engine, which is
        what happens at the live edge of a game where the feed has not caught
        up yet. Returns None if we cannot get both.
        """
        mover = move.side
        before_pov = move.eval_before
        after_pov = move.eval_after

        before_text = after_text = "?"
        chances_before = chances_after = None

        if before_pov is not None:
            chances_before = _chances(before_pov, mover)
            before_text = format_score(before_pov.white())
        elif move.ply == 1:
            chances_before, before_text = 50.0, "+0.20"

        if after_pov is not None:
            chances_after = _chances(after_pov, mover)
            after_text = format_score(after_pov.white())

        if (chances_before is None or chances_after is None) and self.analyst \
                and self.analyst.available:
            if chances_before is None:
                score = self.analyst.evaluate(move.board_before)
                if score is not None:
                    chances_before = score_to_winning_chances(score)
                    white_pov = score if mover == chess.WHITE else -score
                    before_text = format_score(white_pov)
            if chances_after is None:
                score = self.analyst.evaluate(move.board_after)
                if score is not None:
                    # `score` is from the side to move after the move, i.e. the
                    # opponent. Flip it to get the mover's view.
                    chances_after = 100.0 - score_to_winning_chances(score)
                    white_pov = -score if mover == chess.WHITE else score
                    after_text = format_score(white_pov)

        if chances_before is None or chances_after is None:
            return None
        return chances_before, chances_after, before_text, after_text

    # -- rules --------------------------------------------------------------

    def _blunder_subkind(self, before: float, after: float):
        t = self.thresholds
        if before - after < t.blunder_min_drop:
            return None
        # Was at least equal, is now losing. The classic one.
        if before >= t.blunder_was_at_least and after <= t.blunder_now_at_most:
            return "threw_game"
        # Was winning, is no longer. Not losing, but the win is gone, and that
        # is just as much of a story.
        if before >= 80.0 and after <= 55.0:
            return "threw_win"
        return None

    def _looks_like_sacrifice(self, move):
        """Cheap test: did this move hand over material on its target square?"""
        return material_swing(move.board_before, move.move)

    def _confirm_brilliancy(self, move, chances_after: float):
        """Ask Stockfish whether this was the only move that worked.

        Returns (confirmed, gap_in_winning_chances). Without an engine we
        return (False, 0) and the move is not alerted: we would rather stay
        quiet than call something brilliant on a guess.
        """
        if not (self.analyst and self.analyst.available):
            return False, 0.0
        lines = self.analyst.top_moves(move.board_before, count=2)
        if not lines:
            return False, 0.0
        if lines[0].move != move.move:
            return False, 0.0          # the engine would have played something else
        if len(lines) < 2:
            return True, 100.0         # no second move at all: forced
        gap = (score_to_winning_chances(lines[0].score)
               - score_to_winning_chances(lines[1].score))
        return gap >= self.thresholds.brilliancy_min_only_move_gap, gap

    # -- the pass over one game --------------------------------------------

    def scan_game(self, game, from_ply: int = 0):
        """Findings in `game` for moves after `from_ply`."""
        t = self.thresholds
        findings = []

        for move in game.moves:
            if move.ply <= from_ply:
                continue
            # Only alert on the players we are actually watching. A 2700 losing
            # to a 2100 is a story; the 2100's own mistakes usually are not.
            side_key = "white" if move.side == chess.WHITE else "black"
            if not self.alert_both_sides and side_key not in game.watch_reasons:
                continue

            resolved = self._resolve_evals(move)
            if resolved is None:
                continue
            before, after, before_text, after_text = resolved

            subkind = self._blunder_subkind(before, after)
            if subkind:
                finding = Finding(
                    kind=BLUNDER, subkind=subkind, game=game, move=move,
                    mover=move.side, chances_before=before, chances_after=after,
                    eval_before_text=before_text, eval_after_text=after_text,
                    better_move=_better_move_from_comment(move.nag_text),
                )
                if not finding.better_move and self.analyst and self.analyst.available:
                    lines = self.analyst.top_moves(move.board_before, count=1)
                    if lines:
                        finding.better_move = lines[0].san
                finding.detail = _plain_blunder_detail(finding)
                findings.append(finding)
                continue

            # A brilliancy and a blunder are mutually exclusive, so only look
            # here if the move was not a blunder.
            if after < t.brilliancy_min_winning_chances:
                continue
            if before - after > t.brilliancy_max_drop:
                continue
            if before >= 92.0:
                continue  # already completely winning: a sacrifice is just tidying up
            swing = self._looks_like_sacrifice(move)
            if swing > -t.brilliancy_min_sacrifice:
                continue

            confirmed, gap = self._confirm_brilliancy(move, after)
            if not confirmed:
                continue

            finding = Finding(
                kind=BRILLIANCY, subkind="only_move_sac", game=game, move=move,
                mover=move.side, chances_before=before, chances_after=after,
                eval_before_text=before_text, eval_after_text=after_text,
                sacrifice_cp=-swing, only_move_gap=gap, engine_confirmed=True,
            )
            finding.detail = _plain_brilliancy_detail(finding)
            findings.append(finding)

        return findings
