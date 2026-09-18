"""Turn a round's PGN into games and moves we can reason about.

The broadcast PGN is generous: besides the moves it carries names, titles,
ratings, teams, FIDE ids, the game URL, and - this is the useful part -
Lichess's own Stockfish evaluation after most moves, written as [%eval 0.42]
or [%eval #-3].

Those evaluations let us screen every move of every watched game for free, and
wake our own engine only for the few moves that look interesting. When an
evaluation is missing (it can lag by a move or two on a live feed) we fall back
to our engine for that one position.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import chess
import chess.pgn

GAME_ID_RE = re.compile(r"/([A-Za-z0-9]{8})/?$")


@dataclass
class Player:
    name: str = ""
    title: str = ""
    rating: int = 0
    fide_id: int = 0
    team: str = ""

    @property
    def display(self) -> str:
        return ("%s %s" % (self.title, self.name)).strip()

    @property
    def short(self) -> str:
        """Lichess writes surname first; put it back the normal way round."""
        if "," in self.name:
            last, first = self.name.split(",", 1)
            return "%s %s" % (first.strip(), last.strip())
        return self.name


@dataclass
class MoveRecord:
    ply: int                 # 1 for White's first move
    move_number: int         # 1 for both 1.e4 and 1...e5
    side: bool               # chess.WHITE / chess.BLACK - who played it
    san: str
    move: chess.Move
    board_before: chess.Board
    board_after: chess.Board
    eval_before: object = None   # PovScore from the feed, or None
    eval_after: object = None
    clock_after: int = 0         # seconds left, 0 if not given
    nag_text: str = ""           # Lichess's own words, e.g. "Blunder. Qe8 was best."

    @property
    def numbered_san(self) -> str:
        if self.side == chess.WHITE:
            return "%d.%s" % (self.move_number, self.san)
        return "%d...%s" % (self.move_number, self.san)


@dataclass
class Game:
    game_id: str
    url: str
    white: Player
    black: Player
    result: str
    board: int               # board within the match, 1-4
    global_board: int
    round_name: str
    tour_name: str
    eco: str = ""
    opening: str = ""
    moves: list = field(default_factory=list)
    watch_reasons: dict = field(default_factory=dict)  # "white"/"black" -> why

    @property
    def is_live(self) -> bool:
        return self.result == "*"

    @property
    def headline(self) -> str:
        return "%s vs %s" % (self.white.display, self.black.display)

    def player(self, color: bool) -> Player:
        return self.white if color == chess.WHITE else self.black

    def opponent(self, color: bool) -> Player:
        return self.black if color == chess.WHITE else self.white


def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _player_from_headers(headers, prefix: str) -> Player:
    return Player(
        name=headers.get(prefix, "") or "",
        title=headers.get(prefix + "Title", "") or "",
        rating=_int(headers.get(prefix + "Elo")),
        fide_id=_int(headers.get(prefix + "FideId")),
        team=headers.get(prefix + "Team", "") or "",
    )


def _game_id_from_url(url: str) -> str:
    match = GAME_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _boards_from_round_tag(round_tag: str):
    """Read the Round header, which looks like "2.169".

    The number after the dot is the board's position across the whole event,
    not within the match. Every match is four boards listed in order, so the
    board a player sat on is that number folded back into 1-4. Board 1 is the
    team's top player, which is the bit worth putting in an alert.
    """
    global_board = 0
    if "." in (round_tag or ""):
        global_board = _int(round_tag.split(".", 1)[1])
    board = ((global_board - 1) % 4) + 1 if global_board else 0
    return global_board, board


def parse_round_pgn(pgn_text: str, round_name: str, tour_name: str):
    """Every game in one round's PGN, moves included."""
    games = []
    if not pgn_text.strip():
        return games

    stream = io.StringIO(pgn_text)
    while True:
        try:
            parsed = chess.pgn.read_game(stream)
        except Exception as exc:
            print("  ! skipping unreadable game: %s" % exc)
            continue
        if parsed is None:
            break

        headers = parsed.headers
        url = headers.get("GameURL", "") or headers.get("BroadcastURL", "")
        global_board, board = _boards_from_round_tag(headers.get("Round", ""))

        game = Game(
            game_id=_game_id_from_url(headers.get("GameURL", "")) or (
                "%s-%d" % (round_name.replace(" ", ""), global_board)),
            url=url,
            white=_player_from_headers(headers, "White"),
            black=_player_from_headers(headers, "Black"),
            result=headers.get("Result", "*"),
            board=board,
            global_board=global_board,
            round_name=round_name,
            tour_name=tour_name,
            eco=headers.get("ECO", "") or "",
            opening=headers.get("Opening", "") or "",
        )

        node = parsed
        ply = 0
        previous_eval = None
        while node.variations:
            node = node.variations[0]
            ply += 1
            board_before = node.parent.board()
            board_after = node.board()
            try:
                san = board_before.san(node.move)
            except Exception:
                san = node.move.uci()

            try:
                this_eval = node.eval()
            except Exception:
                this_eval = None

            try:
                clock = int(node.clock() or 0)
            except Exception:
                clock = 0

            game.moves.append(MoveRecord(
                ply=ply,
                move_number=(ply + 1) // 2,
                side=board_before.turn,
                san=san,
                move=node.move,
                board_before=board_before,
                board_after=board_after,
                eval_before=previous_eval,
                eval_after=this_eval,
                clock_after=clock,
                nag_text=(node.comment or "").strip(),
            ))
            previous_eval = this_eval

        games.append(game)
    return games


def select_watched(games, watchlist):
    """Keep only the games we care about, recording why for each side."""
    kept = []
    for game in games:
        # The Women's event is rated lower, so it gets its own rating floor.
        # The group name is the only place the feed says which event this is.
        women_event = "women" in (game.tour_name or "").lower()
        reasons = {}
        for key, colour in (("white", chess.WHITE), ("black", chess.BLACK)):
            player = game.player(colour)
            why = watchlist.match({
                "name": player.name,
                "rating": player.rating,
                "fide_id": player.fide_id,
                "team": player.team,
            }, women_event=women_event)
            if why:
                reasons[key] = why
        if reasons:
            game.watch_reasons = reasons
            kept.append(game)
    return kept
