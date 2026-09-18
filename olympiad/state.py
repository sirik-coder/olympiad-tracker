"""Remembering what we have already looked at.

Every run is a fresh machine that has never seen this tournament before, so
without a memory the job would re-read every game from move one and re-post
every alert it has ever posted. The memory is one small JSON file.

Two things are stored, because they answer two different questions:

  progress   how far into each game we have read. Stops us re-analysing
             seventy moves to find the two that are new.

  alerted    which findings have already gone to Slack. Belt and braces: if a
             run dies halfway, or the feed revises a move, progress may go
             backwards, and this stops a duplicate alert going out.

Old rounds are dropped after the event so the file cannot grow without limit.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_PATH = Path(os.getenv("STATE_FILE", "state/seen.json"))
MAX_ALERT_KEYS = 5000


class State:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DEFAULT_PATH)
        self.progress = {}   # round_id -> {game_id: last ply read}
        self.alerted = []    # finding keys, oldest first
        self._alerted_set = set()
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print("  ! state file unreadable, starting fresh: %s" % exc)
            return
        self.progress = data.get("progress") or {}
        self.alerted = data.get("alerted") or []
        self._alerted_set = set(self.alerted)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": int(time.time()),
            "progress": self.progress,
            "alerted": self.alerted[-MAX_ALERT_KEYS:],
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        temp.replace(self.path)

    # -- progress -----------------------------------------------------------

    def last_ply(self, round_id: str, game_id: str) -> int:
        return int(self.progress.get(round_id, {}).get(game_id, 0))

    def set_last_ply(self, round_id: str, game_id: str, ply: int):
        self.progress.setdefault(round_id, {})[game_id] = int(ply)

    # -- alerts -------------------------------------------------------------

    def already_alerted(self, key: str) -> bool:
        return key in self._alerted_set

    def mark_alerted(self, key: str):
        if key in self._alerted_set:
            return
        self._alerted_set.add(key)
        self.alerted.append(key)

    def summary(self) -> str:
        games = sum(len(v) for v in self.progress.values())
        return "%d rounds, %d games tracked, %d alerts sent" % (
            len(self.progress), games, len(self.alerted))
