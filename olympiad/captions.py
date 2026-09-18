"""Draft social captions in ChessMood's voice.

The brief for this voice: 80% premium, 20% fun. MasterClass quality with a
coach who cracks jokes. Never silly, never at the expense of quality.

Two rules that are easy to get wrong and matter a lot:

Never mock the player. These are the strongest players alive, and a brand built
on "GM-guided improvement" does not get to sneer at a grandmaster's worst
moment. Every blunder caption points the lesson back at the reader instead.
That is also the more useful caption: the reader is 35-55, rated somewhere
between 800 and 2400, and wants to know what it means for them.

Say the positioning without reading out the tagline. Words like system, path,
roadmap, personalized, your gaps, step-by-step and GM-guided belong in the
copy. Words like platform, library, collection, browse, explore and video
courses do not.

Captions are picked deterministically from the finding, so the same moment
always produces the same caption, and two alerts in a row do not sound alike.
"""

from __future__ import annotations

import hashlib

from .detect import BLUNDER, BRILLIANCY

# Words we must never ship. Checked by the tests, so a future edit that
# reintroduces one is caught before it reaches Slack.
BANNED_WORDS = [
    "platform", "library", "collection", "generic",
    "browse", "explore", "video course", "chess website", "chess app",
]

# British spellings that should not appear in American English copy.
BANNED_SPELLINGS = ["centre", "defence", "realise", "analyse", "favourite", "practise"]


def _pick(options, seed: str):
    """Choose one option, always the same one for the same seed."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


# Letters that are read starting with a vowel sound, so "an IM" but "a GM".
_VOWEL_SOUND_LETTERS = set("AEFHILMNORSX")


def _article(word: str) -> str:
    if not word:
        return "A"
    return "An" if word[0].upper() in _VOWEL_SOUND_LETTERS else "A"


def _rating_phrase(rating: int, title: str = "") -> str:
    """How to refer to a player in one short phrase.

    Rating first where it is impressive on its own, and the title otherwise.
    Never round a player up: a 2424-rated IM is not "grandmaster-level", and
    getting that wrong in public is exactly the kind of thing chess players
    notice.
    """
    if rating >= 2700:
        return "A 2700"
    if rating >= 2600:
        return "A 2600"
    if title:
        return "%s %s" % (_article(title), title)
    if rating:
        return "A %d-rated player" % rating
    return "A titled player"


# ---------------------------------------------------------------------------
# Blunders: someone equal or better is now losing
# ---------------------------------------------------------------------------

_THREW_GAME = [
    ("{rating_phrase} played {move} and the evaluation fell off a cliff.\n"
     "Chess doesn't care what's next to your name — it cares about your blind "
     "spots. The only question that matters is whether you know where yours are."),

    ("{name} was fine. One move later, {name_last} was lost.\n"
     "That's how it goes for all of us: the position doesn't collapse, a single "
     "habit does. A personalized plan finds the habit before the game does."),

    ("Move {move_no}. {move}. {eval_before} to {eval_after}.\n"
     "You've had this exact moment in your own games. The difference between "
     "repeating it and fixing it is having a roadmap instead of a hunch."),

    ("Even at the Olympiad, games turn on one move.\n"
     "{name} played {move} and the position flipped. Your gaps don't wait for "
     "the right opponent either — which is exactly why you diagnose them first."),

    ("{rating_phrase}. Board {board}. One move.\n"
     "Nobody outgrows this. They just get a system that catches it earlier."),
]

_THREW_WIN = [
    ("{name} was winning. Then {name_last} played {move}, and wasn't.\n"
     "Converting a won position is a skill of its own, and almost nobody trains "
     "it on purpose. That's a gap worth naming."),

    ("{eval_before} to {eval_after} in one move.\n"
     "Winning positions don't win themselves. If yours keep slipping, that's not "
     "bad luck — it's a specific hole, and it has a specific fix."),

    ("The hard part was already done. Then came {move}.\n"
     "Most players lose more rating to shaky conversion than to bad openings. "
     "Step by step is how you stop giving it back."),
]


# ---------------------------------------------------------------------------
# Brilliancies: a sacrifice the engine confirms was the only way
# ---------------------------------------------------------------------------

_BRILLIANCY = [
    ("{name} just gave up {material} and the engine says it's the only move "
     "that wins.\n"
     "Moves like this don't come from inspiration. They come from having seen "
     "the pattern before — which is a thing you can actually train."),

    ("{move}. A sacrifice of {material}, on board {board}, at the Olympiad.\n"
     "Every other move throws the position away. That's not calculation alone — "
     "that's knowing which positions are worth calculating."),

    ("Would you have played {move}?\n"
     "{name} gave up {material} and it's the only path to a win. The players who "
     "find these have a map of the position. You can build the same map."),

    ("The kind of move you replay three times.\n"
     "{name} sacrificed {material} with {move} and the engine backs every inch "
     "of it. Beautiful chess is learnable chess — that's the whole point."),
]

_MATERIAL_WORDS = [
    (850, "a queen"),
    (450, "a rook"),
    (280, "a piece"),
    (150, "more than a pawn"),
    (0, "a pawn"),
]


def _material_words(centipawns: int) -> str:
    for threshold, words in _MATERIAL_WORDS:
        if centipawns >= threshold:
            return words
    return "material"


def draft_caption(finding) -> str:
    """A short, ready-to-post caption for one finding."""
    player = finding.player
    fields = {
        "name": player.short,
        "name_last": player.short.split()[-1] if player.short else "he",
        "rating_phrase": _rating_phrase(player.rating, player.title),
        "rating": player.rating,
        "move": finding.move.san,
        "move_no": finding.move.move_number,
        "board": finding.game.board or 1,
        "eval_before": finding.eval_before_text,
        "eval_after": finding.eval_after_text,
        "opponent": finding.opponent.short,
        "team": player.team,
        "material": _material_words(finding.sacrifice_cp),
    }

    if finding.kind == BRILLIANCY:
        options = _BRILLIANCY
    elif finding.subkind == "threw_win":
        options = _THREW_WIN
    else:
        options = _THREW_GAME

    return _pick(options, finding.key).format(**fields)


def lint_caption(text: str):
    """Off-brand words found in a caption. Empty list means it is clean."""
    lowered = text.lower()
    problems = [w for w in BANNED_WORDS if w in lowered]
    problems += [w for w in BANNED_SPELLINGS if w in lowered]
    return problems
