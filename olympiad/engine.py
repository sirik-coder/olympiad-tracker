"""Stockfish wrapper, plus the material test used to spot a sacrifice.

Two jobs live here:

1. Ask Stockfish for the best moves in a position (MultiPV), which is how we
   confirm a brilliancy. A brilliancy is not just "a good sacrifice", it is a
   sacrifice that was the *only* way. To know that, we need the best move and
   the second best move, and Lichess's broadcast evaluations only give us one.

2. Work out how much material a move gives away, without an engine. This is a
   static exchange evaluation: play out every capture on the target square,
   best play from both sides, and see who ends up ahead. It is cheap, so we can
   run it on every move of every watched game and only wake the engine for the
   handful of moves that actually look like sacrifices.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

import chess
import chess.engine

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}

# Where Stockfish usually lives, in the order we try.
CANDIDATE_PATHS = [
    "/usr/games/stockfish",
    "/usr/bin/stockfish",
    "/usr/local/bin/stockfish",
    "/opt/homebrew/bin/stockfish",
]


def find_stockfish() -> str | None:
    """Locate the Stockfish program, or return None if it is not installed."""
    override = os.getenv("STOCKFISH_PATH")
    if override and os.path.exists(override):
        return override
    found = shutil.which("stockfish")
    if found:
        return found
    for path in CANDIDATE_PATHS:
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Static exchange evaluation
# ---------------------------------------------------------------------------

def _captured_value(board: chess.Board, move: chess.Move) -> int:
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    piece = board.piece_at(move.to_square)
    return PIECE_VALUES[piece.piece_type] if piece else 0


def _promotion_bonus(move: chess.Move) -> int:
    if not move.promotion:
        return 0
    return PIECE_VALUES[move.promotion] - PIECE_VALUES[chess.PAWN]


def _best_capture_gain(board: chess.Board, square: int, depth: int = 0) -> int:
    """Most material the side to move can win by capturing on `square`.

    Returns 0 if capturing there only loses material: nobody is forced to take.
    """
    if depth > 12:
        return 0
    best = 0
    for move in board.legal_moves:
        if move.to_square != square or not board.is_capture(move):
            continue
        gain = _captured_value(board, move) + _promotion_bonus(move)
        board.push(move)
        gain -= _best_capture_gain(board, square, depth + 1)
        board.pop()
        if gain > best:
            best = gain
    return best


def material_swing(board: chess.Board, move: chess.Move) -> int:
    """Centipawns the mover nets from `move`, assuming best play on that square.

    Negative means material was given up. A knight dropped on an empty defended
    square scores about -320; taking a free pawn scores +100.
    """
    gain = _captured_value(board, move) + _promotion_bonus(move)
    after = board.copy(stack=False)
    after.push(move)
    gain -= _best_capture_gain(after, move.to_square)
    return gain


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class Line:
    """One engine suggestion: a move and how good the engine thinks it is."""
    move: chess.Move
    san: str
    score: chess.engine.Score  # from the point of view of the side to move
    pv: list


class Analyst:
    """A running Stockfish process. Use as a context manager.

    If Stockfish is missing, `available` is False and every call returns None,
    so the rest of the program keeps working on Lichess's evaluations alone.
    """

    def __init__(self, thresholds, path: str | None = None):
        self.thresholds = thresholds
        self.path = path or find_stockfish()
        self._engine = None
        self.positions_analysed = 0

    @property
    def available(self) -> bool:
        return self._engine is not None

    def __enter__(self) -> "Analyst":
        if not self.path:
            return self
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path)
            self._engine.configure({
                "Threads": self.thresholds.engine_threads,
                "Hash": self.thresholds.engine_hash_mb,
            })
        except Exception as exc:  # a missing or broken binary must not be fatal
            print("  ! could not start Stockfish (%s): %s" % (self.path, exc))
            self._engine = None
        return self

    def __exit__(self, *exc_info):
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None

    def _limit(self) -> chess.engine.Limit:
        return chess.engine.Limit(
            depth=self.thresholds.engine_depth,
            time=self.thresholds.engine_movetime_ms / 1000.0,
        )

    def top_moves(self, board: chess.Board, count: int = 2):
        """Best `count` moves, best first. None if the engine is unavailable."""
        if self._engine is None:
            return None
        if self.positions_analysed >= self.thresholds.max_engine_positions_per_poll:
            return None
        self.positions_analysed += 1
        try:
            infos = self._engine.analyse(board, self._limit(), multipv=count)
        except Exception as exc:
            print("  ! engine error: %s" % exc)
            return None
        if isinstance(infos, dict):
            infos = [infos]

        lines = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            mover = board.turn
            lines.append(Line(
                move=pv[0],
                san=board.san(pv[0]),
                score=info["score"].pov(mover),
                pv=list(pv),
            ))
        return lines or None

    def evaluate(self, board: chess.Board):
        """Evaluation of `board` from the side to move. None if unavailable."""
        lines = self.top_moves(board, count=1)
        return lines[0].score if lines else None
