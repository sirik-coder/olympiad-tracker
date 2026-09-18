"""Settings, thresholds and the watch list.

Everything a human is likely to want to change lives either here or in
watchlist.yml. Secrets are read from environment variables only.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# The Lichess "group" that holds all nine Olympiad broadcasts. This is the short
# code at the end of the tournament index URL. A group id is NOT a tournament
# id: /api/broadcast/<group id> returns 404. We reach the group by asking any
# member tournament for its group.tours list (see lichess.py).
GROUP_ID = "32sSzO6S"

# Any one tournament belonging to the group above. Used only as the door in.
SEED_TOUR_ID = "n1pPI5Q0"  # Open | Matches 1-12

LICHESS_API = "https://lichess.org/api"
USER_AGENT = "chessmood-olympiad-tracker (contact: sirik@chessmood.com)"


# ---------------------------------------------------------------------------
# Detection thresholds
#
# Evaluations are converted to a 0-100 "winning chances" scale before any
# comparison (see evalscale.py). That matters: +5 to +12 pawns is a huge
# centipawn swing that changes nothing about the game, while 0.0 to -2.0
# decides it. Working in winning chances makes one set of numbers behave
# sensibly in both cases, and handles forced mates without special cases.
# ---------------------------------------------------------------------------

SENSITIVITY_PRESETS = {
    # name:      (min_drop, was_at_least, now_at_most)
    "loud":     (20.0, 40.0, 35.0),
    "balanced": (25.0, 45.0, 30.0),
    "strict":   (35.0, 50.0, 22.0),
}


@dataclass
class Thresholds:
    """Rules for calling a move a blunder or a brilliancy."""

    # --- blunder ---
    blunder_min_drop: float = 25.0        # winning chances must fall this far
    blunder_was_at_least: float = 45.0    # from at least an equal position
    blunder_now_at_most: float = 30.0     # to a clearly lost one

    # --- brilliancy ---
    brilliancy_min_sacrifice: int = 90          # centipawns given up (SEE)
    brilliancy_min_winning_chances: float = 55.0
    brilliancy_max_drop: float = 8.0
    brilliancy_min_only_move_gap: float = 20.0  # second best must be worse

    # --- engine ---
    engine_depth: int = 18
    engine_movetime_ms: int = 1500
    engine_threads: int = 2
    engine_hash_mb: int = 256
    max_engine_positions_per_poll: int = 40

    @classmethod
    def from_env(cls) -> "Thresholds":
        t = cls()
        preset = os.getenv("SENSITIVITY", "balanced").strip().lower()
        if preset in SENSITIVITY_PRESETS:
            t.blunder_min_drop, t.blunder_was_at_least, t.blunder_now_at_most = (
                SENSITIVITY_PRESETS[preset]
            )
        overrides = [
            ("BLUNDER_MIN_DROP", "blunder_min_drop", float),
            ("BLUNDER_WAS_AT_LEAST", "blunder_was_at_least", float),
            ("BLUNDER_NOW_AT_MOST", "blunder_now_at_most", float),
            ("BRILLIANCY_MIN_SACRIFICE", "brilliancy_min_sacrifice", int),
            ("BRILLIANCY_MIN_ONLY_MOVE_GAP", "brilliancy_min_only_move_gap", float),
            ("ENGINE_DEPTH", "engine_depth", int),
            ("ENGINE_MOVETIME_MS", "engine_movetime_ms", int),
        ]
        for env_name, attr, cast in overrides:
            raw = os.getenv(env_name)
            if raw:
                setattr(t, attr, cast(raw))
        return t


# ---------------------------------------------------------------------------
# Watch list
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Make two spellings of the same name comparable.

    Lichess writes "Sargissian, Gabriel"; a person might type "Gabriel
    Sargsyan". Stripping accents, punctuation and word order lines up the easy
    cases. FIDE ids remain the reliable key.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c.lower() if c.isalnum() else " " for c in text)
    return " ".join(sorted(text.split()))


@dataclass
class WatchList:
    min_rating: int = 2600
    # The Women's event is rated lower across the board: the strongest woman in
    # this Olympiad is 2536. One shared cutoff would silence half the event.
    min_rating_women: int = 2400
    player_fide_ids: set = field(default_factory=set)
    player_names: set = field(default_factory=set)
    teams: set = field(default_factory=set)
    muted_names: set = field(default_factory=set)
    notes: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path=None) -> "WatchList":
        path = Path(path) if path else REPO_ROOT / "watchlist.yml"
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        wl = cls(
            min_rating=int(data.get("min_rating") or 2600),
            min_rating_women=int(data.get("min_rating_women") or 2400),
        )
        for entry in data.get("always_watch_players") or []:
            if entry.get("fide_id"):
                wl.player_fide_ids.add(int(entry["fide_id"]))
            if entry.get("name"):
                key = normalize_name(entry["name"])
                wl.player_names.add(key)
                if entry.get("note"):
                    wl.notes[key] = entry["note"]
        wl.teams = {t.strip().lower() for t in (data.get("always_watch_teams") or [])}
        wl.muted_names = {normalize_name(n) for n in (data.get("mute_players") or [])}

        if os.getenv("MIN_RATING"):
            wl.min_rating = int(os.environ["MIN_RATING"])
        if os.getenv("MIN_RATING_WOMEN"):
            wl.min_rating_women = int(os.environ["MIN_RATING_WOMEN"])
        return wl

    def match(self, player: dict, women_event: bool = False):
        """Return why we watch this player, or None if we do not."""
        key = normalize_name(player.get("name", ""))
        if key and key in self.muted_names:
            return None
        fide_id = player.get("fide_id")
        if fide_id and int(fide_id) in self.player_fide_ids:
            return self.notes.get(key, "watch list")
        if key and key in self.player_names:
            return self.notes.get(key, "watch list")
        team = (player.get("team") or "").strip().lower()
        if team and team in self.teams:
            return "team " + str(player.get("team"))
        floor = self.min_rating_women if women_event else self.min_rating
        if (player.get("rating") or 0) >= floor:
            return "rated " + str(player["rating"])
        return None
