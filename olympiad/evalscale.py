"""Turn engine evaluations into "winning chances" from 0 to 100.

Why not just use centipawns? Because a centipawn is not worth the same amount
everywhere on the scale. Going from +5.0 to +12.0 is a 700 centipawn swing and
means nothing: the game was already over. Going from 0.0 to -2.0 is a 200
centipawn swing and decides the game. If we set a blunder threshold in raw
centipawns we would either drown in alerts from won positions or miss the real
ones.

Winning chances fix that. They are a squashed version of the evaluation, so
they move fast near equality and barely move at all once someone is winning.
A forced mate is simply 100 or 0, with no special case needed.

The curve is the one Lichess uses for its own accuracy numbers.
"""

from __future__ import annotations

import math

MATE_CHANCES = 100.0
# Steepness of the curve. Lichess's constant.
_K = 0.00368208


def cp_to_winning_chances(centipawns: float) -> float:
    """Centipawns (from one player's point of view) -> 0..100 for that player."""
    capped = max(-1500.0, min(1500.0, float(centipawns)))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-_K * capped)) - 1.0)


def score_to_winning_chances(score) -> float:
    """A python-chess Score (already from one player's point of view) -> 0..100.

    `score` is what you get from PovScore.pov(color) or .white()/.black().
    """
    mate = score.mate()
    if mate is not None:
        # Mate in N for us is 100, mate in N against us is 0. The number of
        # moves does not change who is winning, so we do not scale by it.
        return MATE_CHANCES if mate > 0 else 0.0
    cp = score.score()
    if cp is None:
        return 50.0
    return cp_to_winning_chances(cp)


def format_score(score) -> str:
    """Human-readable evaluation, always from White's point of view upstream.

    Examples: "+1.35", "-0.42", "M4", "-M2".
    """
    mate = score.mate()
    if mate is not None:
        return ("M%d" % mate) if mate > 0 else ("-M%d" % abs(mate))
    cp = score.score()
    if cp is None:
        return "?"
    return "%+.2f" % (cp / 100.0)


def describe_position(chances: float) -> str:
    """Plain words for how good a position is, for the player to move-ish.

    Used in the Slack text so a reader does not have to decode numbers.
    """
    if chances >= 90:
        return "completely winning"
    if chances >= 75:
        return "clearly winning"
    if chances >= 60:
        return "better"
    if chances > 40:
        return "roughly equal"
    if chances > 25:
        return "worse"
    if chances > 10:
        return "clearly losing"
    return "completely lost"
