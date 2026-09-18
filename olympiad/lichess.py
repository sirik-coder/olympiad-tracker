"""Talking to the Lichess Broadcast API.

Three things worth knowing, because they are not obvious from the docs:

1. The Olympiad is not one broadcast. It is nine, held together by a "group".
   The short code in the tournament index URL (32sSzO6S) is the *group* id, and
   asking /api/broadcast/<group id> returns 404. The way in is to ask any
   member tournament for its own record: the reply carries group.tours, the
   full list of nine. We do that rather than hard-coding the nine ids, so the
   list stays right if Lichess adds a group mid-event.

2. The nine groups are split by match number ("Open | Matches 38-62"), and
   match numbers follow the standings, which change every round. A team is not
   in a fixed group. Thailand sat in Matches 38-62 in round 2 and will move.
   So we never look up a team's group: we read all nine every time.

3. Each group keeps its own set of round ids. Round 3 of the Open top boards is
   a different id from round 3 of the Women's top boards. There are 99 round
   ids in total, and we resolve them at run time.

One more thing that saves a lot of requests: the PGN feed already carries
player names, ratings, teams, FIDE ids and the game URL in its headers, so we
never need the separate games endpoint. Nine PGN requests per poll is the whole
network cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .config import LICHESS_API, SEED_TOUR_ID, USER_AGENT

TIMEOUT = 45


@dataclass
class Round:
    id: str
    name: str
    starts_at_ms: int
    finished: bool
    tour_id: str
    tour_name: str          # e.g. "Open | Matches 38-62"
    url: str

    @property
    def starts_at(self) -> float:
        return (self.starts_at_ms or 0) / 1000.0


class Lichess:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _get(self, url: str, as_json: bool = True, attempts: int = 3):
        last_error = None
        for attempt in range(attempts):
            try:
                response = self.session.get(url, timeout=TIMEOUT)
                if response.status_code == 429:
                    # Lichess asks us to slow down. It means it.
                    wait = 60
                    print("  ! rate limited, waiting %ds" % wait)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json() if as_json else response.text
            except Exception as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        print("  ! giving up on %s: %s" % (url, last_error))
        return None

    # -- discovery ----------------------------------------------------------

    def group_tours(self, seed_tour_id: str = SEED_TOUR_ID):
        """The nine Olympiad tournaments, as (id, name) pairs."""
        data = self._get("%s/broadcast/%s" % (LICHESS_API, seed_tour_id))
        if not data:
            return []
        group = data.get("group") or {}
        tours = group.get("tours") or []
        if tours:
            return [(t["id"], t["name"]) for t in tours]
        # No group for some reason: fall back to the seed alone.
        return [(seed_tour_id, data["tour"]["name"])]

    def rounds(self, tour_id: str, tour_name: str):
        data = self._get("%s/broadcast/%s" % (LICHESS_API, tour_id))
        if not data:
            return []
        out = []
        for r in data.get("rounds", []):
            out.append(Round(
                id=r["id"],
                name=r.get("name", "?"),
                starts_at_ms=r.get("startsAt") or 0,
                finished=bool(r.get("finished")),
                tour_id=tour_id,
                # Names from group.tours are already short ("Women | Matches
                # 1-25"). Keep them whole: the "Women" half is how we tell the
                # two events apart, and they have different rating floors.
                tour_name=tour_name,
                url=r.get("url", ""),
            ))
        return out

    def current_rounds(self, now: float | None = None, lookahead_min: int = 45,
                       lookbehind_h: int = 10):
        """The round each group is playing right now, one per group.

        A classical Olympiad round lasts six hours or more, so "current" means
        "started within the last ten hours and is not finished". We also accept
        a round starting in the next 45 minutes, so the first poll of the day
        already has the right round loaded when the clocks start.
        """
        now = now or time.time()
        live = []
        for tour_id, tour_name in self.group_tours():
            best = None
            for rnd in self.rounds(tour_id, tour_name):
                if not rnd.starts_at_ms:
                    continue
                age_h = (now - rnd.starts_at) / 3600.0
                if -lookahead_min / 60.0 <= age_h <= lookbehind_h:
                    if best is None or rnd.starts_at > best.starts_at:
                        best = rnd
            if best is not None:
                live.append(best)
        return live

    def round_by_name(self, round_name: str):
        """Every group's round with this name, e.g. "Round 2". For backtests."""
        out = []
        for tour_id, tour_name in self.group_tours():
            for rnd in self.rounds(tour_id, tour_name):
                if rnd.name.strip().lower() == round_name.strip().lower():
                    out.append(rnd)
        return out

    # -- the live feed ------------------------------------------------------

    def round_pgn(self, round_id: str) -> str:
        """Every game of one round, as PGN, as it stands right now.

        This is a snapshot, not the streaming endpoint. A cron job wakes up,
        looks, and goes away again, so a stream it cannot hold open is no use
        to it: the snapshot already contains the moves the stream would have
        pushed while we were asleep.
        """
        text = self._get("%s/broadcast/round/%s.pgn" % (LICHESS_API, round_id),
                         as_json=False)
        return text or ""
