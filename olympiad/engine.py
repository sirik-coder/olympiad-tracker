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


def best_material_grab(board: chess.Board) -> int:
    """Most material the side to move can win by force, anywhere on the board.

    This is the general form of the sacrifice test. Looking only at the square
    a piece moves to misses most real sacrifices: a queen offered on one square
    and taken on another, a rook left hanging while the attack goes elsewhere,
    an exchange sacrifice that is only accepted two moves later. Asking instead
    "after this move, what can the opponent simply win?" catches all of them.
    """
    best = 0
    for move in board.legal_moves:
        if not board.is_capture(move):
            continue
        gain = _captured_value(board, move) + _promotion_bonus(move)
        board.push(move)
        gain -= _best_capture_gain(board, move.to_square)
        board.pop()
        if gain > best:
            best = gain
    return best


def material_swing(board: chess.Board, move: chess.Move, anywhere: bool = True) -> int:
    """Centipawns the mover nets from `move`. Negative means material given up.

    Both halves matter, and getting one of them wrong is easy.

    What the move *wins* has to be counted, or every ordinary capture looks
    like a sacrifice: Qxd8 answered by Rxd8 hands the opponent a queen, but it
    also took one, so the net is nothing. Measuring only what the opponent can
    grab flagged 73 routine recaptures in two rounds as brilliancies.

    What the move *loses* has to be counted across the whole board, not just
    the square landed on, or most real sacrifices are invisible: a queen
    offered on one square and taken on another, a rook left hanging while the
    attack goes elsewhere. That mistake found 5 candidates in two rounds where
    there should have been dozens.
    """
    gain = _captured_value(board, move) + _promotion_bonus(move)
    after = board.copy(stack=False)
    after.push(move)
    if anywhere:
        gain -= best_material_grab(after)
    else:
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
        self.positions_screened = 0   # shallow, every new move
        self.positions_analysed = 0   # full depth, only flagged moves
        self.positions_confirmed = 0  # deepest, only moves about to be alerted

    def reset_budget(self):
        self.positions_screened = 0
        self.positions_analysed = 0
        self.positions_confirmed = 0

    def _spend(self, tier: str) -> bool:
        """Take one unit from this tier's budget. False if it is used up."""
        t = self.thresholds
        if tier == "screen":
            if self.positions_screened >= t.max_screen_positions_per_poll:
                return False
            self.positions_screened += 1
        elif tier == "confirm":
            if self.positions_confirmed >= t.max_confirm_positions_per_poll:
                return False
            self.positions_confirmed += 1
        else:
            if self.positions_analysed >= t.max_engine_positions_per_poll:
                return False
            self.positions_analysed += 1
        return True

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

    def _limit(self, tier: str = "deep") -> chess.engine.Limit:
        t = self.thresholds
        if tier == "screen":
            return chess.engine.Limit(depth=t.engine_screen_depth,
                                      time=t.engine_screen_ms / 1000.0)
        if tier == "confirm":
            return chess.engine.Limit(depth=t.engine_confirm_depth,
                                      time=t.engine_confirm_ms / 1000.0)
        return chess.engine.Limit(depth=t.engine_depth,
                                  time=t.engine_movetime_ms / 1000.0)

    def top_moves(self, board: chess.Board, count: int = 2, tier: str = "deep"):
        """Best `count` moves, best first. None if the engine is unavailable.

        `tier` is one of "screen", "deep" or "confirm", cheapest first.
        """
        if self._engine is None:
            return None
        if not self._spend(tier):
            return None
        try:
            infos = self._engine.analyse(board, self._limit(tier), multipv=count)
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
        """Evaluation of `board` from the side to move. None if unavailable.

        Deliberately shallow. This runs only where the broadcast's own
        evaluation is missing, and there may be hundreds of those in one poll
        if the live feed is running without them.
        """
        lines = self.top_moves(board, count=1, tier="screen")
        return lines[0].score if lines else None
