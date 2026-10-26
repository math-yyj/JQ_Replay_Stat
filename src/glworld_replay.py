"""联众四国军棋 .JQH.TXT 文本复盘解析。

联众客户端导出的文本复盘包含玩家、17×17 初始棋盘和行棋记录。
本模块按网页版 qq/fp 分析器的规则完成：
- GB18030 文本解码、玩家/棋盘/行棋解析；
- 按座位象限分配棋子归属；
- 逐步推演、战斗结算、跳过判负与扛旗清子；
- 子力得失、净值、评级与胜负判定。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

COLOR_ORDER = ("blue", "red", "green", "gray")
COLOR_CN = {"blue": "蓝", "red": "红", "green": "绿", "gray": "灰"}
SEAT_ORDER = ("上", "下", "左", "右")
SEAT_TO_QUAD = {"上": "top", "下": "bottom", "左": "left", "右": "right"}
TURN_ORDER = ["red", "green", "blue", "gray"]

ROW_LETTERS = "ABCDEFGHKMNPQRSTW"
ROW_MAP = {letter: index for index, letter in enumerate(ROW_LETTERS)}

PIECE_TYPES = {
    "司": ("司令", 9, 100),
    "军": ("军长", 8, 80),
    "师": ("师长", 7, 60),
    "旅": ("旅长", 6, 45),
    "团": ("团长", 5, 35),
    "营": ("营长", 4, 25),
    "连": ("连长", 3, 18),
    "排": ("排长", 2, 12),
    "兵": ("工兵", 1, 8),
    "弹": ("炸弹", 0, 35),
    "雷": ("地雷", -1, 15),
    "旗": ("军旗", -2, 0),
}
PIECE_CHARS = set(PIECE_TYPES)
PIECE_VALUE = {char: info[2] for char, info in PIECE_TYPES.items()}
PIECE_RANK = {char: info[1] for char, info in PIECE_TYPES.items()}
PIECE_NAME = {char: info[0] for char, info in PIECE_TYPES.items()}

# 联众文本棋盘的中央三行只在这些列有大本营/兵站标记
CENTER_HQ_ROWS = (6, 8, 10)
CENTER_HQ_COLS = (6, 8, 10)


@dataclass
class GLPlayer:
    color: str
    name: str
    seat: str
    status: str = ""
    win: bool = False
    is_break: bool = False
    is_first_move: bool = False
    raw: str = ""


@dataclass
class GLPiece:
    char: str
    player: str | None = None


@dataclass
class GLCell:
    type: str
    piece: GLPiece | None = None


@dataclass
class GLMove:
    num: int
    kind: str = "move"
    start: tuple[int, int] | None = None
    end: tuple[int, int] | None = None
    time1: str = ""
    time2: str = ""
    turn_player: str | None = None
    player: str | None = None
    combat: dict | None = None
    result: str = ""

    @property
    def display_type(self) -> str:
        return {"pass": "Pass", "giveup": "GiveUp"}.get(self.kind, "行棋")


@dataclass
class GLStats:
    color: str
    moves: int = 0
    attacks: int = 0
    captures: int = 0
    losses: int = 0
    mutual_destruct: int = 0
    bomb_kills: int = 0
    bomb_losses: int = 0
    mine_kills: int = 0
    mine_losses: int = 0
    value_gained: int = 0
    value_lost: int = 0
    net_value: int = 0
    rating: str = ""


@dataclass
class GLResult:
    winners: list[str] = field(default_factory=list)
    losers: list[str] = field(default_factory=list)
    breakers: list[str] = field(default_factory=list)
    is_draw: bool = False
    desc: str = ""


@dataclass
class GLAnalysis:
    stats: dict[str, GLStats]
    value_curve: list[tuple[int, dict[str, int]]]
    result: GLResult
    winner: list[str]
    key_events: list[dict] = field(default_factory=list)
    player_move_counts: dict[str, int] = field(default_factory=dict)
    player_time: dict[str, int] = field(default_factory=dict)


@dataclass
class GLReplay:
    path: str
    text: str
    players: dict[str, GLPlayer]
    seat_color: dict[str, str]
    first_move_color: str | None
    game_info: dict[str, str]
    board: list[list[GLCell | None]]
    moves: list[GLMove]
    _analysis: GLAnalysis | None = field(default=None, repr=False)

    @property
    def player_list(self) -> list[GLPlayer]:
        return [self.players[c] for c in COLOR_ORDER if c in self.players]

    @property
    def piece_count(self) -> int:
        return sum(
            1 for row in self.board for cell in row
            if cell is not None and cell.piece is not None
        )

    def analyze(self) -> GLAnalysis:
        if self._analysis is None:
            self._analysis = _simulate_and_analyze(self)
        return self._analysis

    def to_dict(self) -> dict:
        analysis = self.analyze()
        return {
            "file": self.path,
            "game_info": self.game_info,
            "seat_color": self.seat_color,
            "first_move_color": self.first_move_color,
            "players": {
                color: {
                    "name": p.name,
                    "seat": p.seat,
                    "status": p.status,
                    "win": p.win,
                    "break": p.is_break,
                    "first_move": p.is_first_move,
                }
                for color, p in self.players.items()
            },
            "board": [
                [
                    None if cell is None else {
                        "type": cell.type,
                        "piece": None if cell.piece is None else {
                            "char": cell.piece.char,
                            "player": cell.piece.player,
                        },
                    }
                    for cell in row
                ]
                for row in self.board
            ],
            "moves": [
                {
                    "num": move.num,
                    "type": move.kind,
                    "from": list(move.start) if move.start else None,
                    "to": list(move.end) if move.end else None,
                    "time1": move.time1,
                    "time2": move.time2,
                    "turn_player": move.turn_player,
                    "player": move.player,
                    "combat": move.combat,
                    "result": move.result,
                }
                for move in self.moves
            ],
            "stats": {
                color: {
                    "moves": st.moves,
                    "attacks": st.attacks,
                    "captures": st.captures,
                    "losses": st.losses,
                    "mutual_destruct": st.mutual_destruct,
                    "bomb_kills": st.bomb_kills,
                    "bomb_losses": st.bomb_losses,
                    "mine_kills": st.mine_kills,
                    "mine_losses": st.mine_losses,
                    "value_gained": st.value_gained,
                    "value_lost": st.value_lost,
                    "net_value": st.net_value,
                    "rating": st.rating,
                }
                for color, st in analysis.stats.items()
            },
            "winner": analysis.winner,
            "result": {
                "winners": analysis.result.winners,
                "losers": analysis.result.losers,
                "breakers": analysis.result.breakers,
                "is_draw": analysis.result.is_draw,
                "desc": analysis.result.desc,
            },
            "value_curve": [
                {"step": step, "values": values}
                for step, values in analysis.value_curve
            ],
            "key_events": analysis.key_events,
        }


def parse_glworld_text(path: str | os.PathLike) -> GLReplay:
    raw = open(path, "rb").read()
    return parse_glworld_bytes(raw, str(path))


def parse_glworld_bytes(raw: bytes, path: str = "<bytes>") -> GLReplay:
    if raw[:4] == b"GL\xea\x03":
        raise ValueError(
            "这是联众二进制 .jqh 复盘；网页版与本工具目前仅支持伴随导出的 "
            ".JQH.TXT 文本复盘。")
    text = _decode_text(raw)
    return parse_glworld_string(text, path)


def _decode_text(raw: bytes) -> str:
    for encoding in ("gb18030", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def parse_glworld_string(text: str, path: str = "<text>") -> GLReplay:
    lines = text.splitlines()
    players, seat_color = _parse_players(lines)
    game_info = _parse_game_info(lines)
    moves = _parse_moves(lines)
    if not moves:
        raise ValueError("未解析到行棋记录，请确认文件格式")
    first_move_color = _first_move_color(moves, seat_color, players)
    board = _parse_board(lines)
    color_quadrant = _build_color_quadrant_map(moves, first_move_color, seat_color)
    _assign_piece_owners(board, color_quadrant)
    if not players:
        # 极少数文本没有玩家行：按象限推断颜色名
        for color in COLOR_ORDER:
            seat = next((s for s, c in seat_color.items() if c == color), "")
            players[color] = GLPlayer(
                color=color, name=COLOR_CN[color] + "方", seat=seat)
    return GLReplay(
        path=path,
        text=text,
        players=players,
        seat_color=seat_color,
        first_move_color=first_move_color,
        game_info=game_info,
        board=board,
        moves=moves,
    )


def _fw(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _parse_coordinate(value: str) -> tuple[int, int] | None:
    value = _fw(value)
    if not value:
        return None
    row = ROW_MAP.get(value[0].upper())
    if row is None:
        return None
    match = re.search(r"(\d+)", value[1:])
    if not match:
        return None
    col = int(match.group(1)) - 1
    if not 0 <= col < 17:
        return None
    return row, col


def _parse_players(lines: list[str]):
    players: dict[str, GLPlayer] = {}
    seat_color: dict[str, str] = {}
    for raw in lines:
        match = re.match(
            r"^\s*([上下左右])([蓝红绿灰])(\*)?\s*:\s*(.*?)\s*$", raw.rstrip())
        if not match:
            continue
        seat, color_cn, star, body = match.groups()
        color = {"蓝": "blue", "红": "red", "绿": "green", "灰": "gray"}[color_cn]
        win = bool(re.search(r"\bwin\b", body))
        is_break = bool(re.search(r"\bbreak\b", body))
        is_first = bool(star) or bool(re.search(r"\(first move\)", body))
        name = re.sub(r"\bwin\b|\bbreak\b|\(first move\)", "", body)
        name = re.sub(r"\s+", "", name.replace(":", "")) or "(空)"
        status = "win" if win else ("break" if is_break else ("first" if is_first else ""))
        players[color] = GLPlayer(
            color=color, name=name, seat=seat, status=status,
            win=win, is_break=is_break, is_first_move=is_first,
            raw=raw.rstrip())
        seat_color[seat] = color
    return players, seat_color


def _parse_game_info(lines: list[str]) -> dict[str, str]:
    info: dict[str, str] = {}
    for raw in lines:
        match = re.match(r"^\s*(人数|保留|时间|简介)\s*:\s*(.*?)\s*$", raw.rstrip())
        if match:
            info[match.group(1)] = match.group(2)
    return info


def _parse_moves(lines: list[str]) -> list[GLMove]:
    moves: list[GLMove] = []
    for raw in lines:
        line = _fw(raw.rstrip())
        match = re.match(
            r"^\s*(\d+)\s*[.．、]\s*([A-Wa-w]\d+)-([A-Wa-w]\d+)\s*"
            r"\(([\d:]+)-([\d:]+)\)", line)
        if match:
            start = _parse_coordinate(match.group(2))
            end = _parse_coordinate(match.group(3))
            if start and end:
                moves.append(GLMove(
                    num=int(match.group(1)), kind="move",
                    start=start, end=end,
                    time1=match.group(4), time2=match.group(5)))
            continue
        match = re.match(
            r"^\s*(\d+)\s*[.．、]\s*(Pass|GiveUp)\s*"
            r"\(([\d:]+)-([\d:]+)\)", line, re.I)
        if match:
            moves.append(GLMove(
                num=int(match.group(1)),
                kind=match.group(2).lower(),
                time1=match.group(3), time2=match.group(4)))
    return moves


def _first_move_color(moves: list[GLMove], seat_color: dict[str, str],
                      players: dict[str, GLPlayer]) -> str | None:
    first = next((m for m in moves if m.start is not None), None)
    if first is not None:
        quadrant = _quadrant(*first.start)
        seat = next((s for s, q in SEAT_TO_QUAD.items() if q == quadrant), None)
        if seat and seat_color.get(seat):
            return seat_color[seat]
    for color, player in players.items():
        if player.is_first_move:
            return color
    return next(iter(seat_color.values()), None)


def _quadrant(row: int, col: int) -> str | None:
    if row <= 5 and 6 <= col <= 10:
        return "top"
    if row >= 11 and 6 <= col <= 10:
        return "bottom"
    if 6 <= row <= 10 and col <= 5:
        return "left"
    if 6 <= row <= 10 and col >= 11:
        return "right"
    return None


def _build_color_quadrant_map(moves: list[GLMove], first_color: str | None,
                              seat_color: dict[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for seat, color in seat_color.items():
        quadrant = SEAT_TO_QUAD.get(seat)
        if quadrant:
            mapping[color] = quadrant
    if len(mapping) == 4:
        return mapping

    turn = _turn_order(first_color)
    giveup_index = next((i for i, m in enumerate(moves) if m.kind == "giveup"), len(moves))
    turn_index = 0
    for move in moves[:giveup_index]:
        if move.kind != "move" or not move.start:
            continue
        actor = turn[turn_index % len(turn)] if turn else None
        turn_index += 1
        if actor in mapping:
            continue
        quadrant = _quadrant(*move.start)
        if quadrant:
            mapping[actor] = quadrant
        if len(mapping) >= 4:
            break
    remaining_quadrants = [
        q for q in ("top", "bottom", "left", "right") if q not in mapping.values()]
    for color in COLOR_ORDER:
        if color not in mapping and remaining_quadrants:
            mapping[color] = remaining_quadrants.pop(0)
    return mapping


def _parse_board(lines: list[str]) -> list[list[GLCell | None]]:
    board: list[list[GLCell | None]] = [[None] * 17 for _ in range(17)]
    for raw in lines:
        stripped = raw.lstrip("\u3000 ")
        if not stripped:
            continue
        first = _fw(stripped[0])
        if first not in ROW_MAP:
            continue
        row = ROW_MAP[first]
        cells: list[tuple[str, str]] = []
        for char in stripped[1:]:
            char = _fw(char)
            if char in PIECE_CHARS:
                cells.append(("piece", char))
            elif char == "○":
                cells.append(("camp", char))
            elif char in ("□", "◇"):
                cells.append(("hq", char))
        playable = [c for c in cells if c[0] in ("piece", "camp")]
        if 6 <= row <= 10:
            if row in CENTER_HQ_ROWS:
                for col in CENTER_HQ_COLS:
                    board[row][col] = GLCell("hq")
            for index, (kind, char) in enumerate(playable[:6]):
                board[row][index] = _make_cell(kind, char)
            for index, (kind, char) in enumerate(playable[-6:]):
                board[row][11 + index] = _make_cell(kind, char)
        else:
            if (row == 13 and len(playable) == 5
                    and playable[2][0] == "piece" and playable[3][0] == "camp"):
                playable[2], playable[3] = playable[3], playable[2]
            if row == 14 and len(playable) == 5:
                if playable[1][0] == "piece" and playable[2][0] == "camp":
                    playable[1], playable[2] = playable[2], playable[1]
                if playable[3][0] == "piece" and playable[4][0] == "camp":
                    playable[3], playable[4] = playable[4], playable[3]
            for index, (kind, char) in enumerate(playable[:5]):
                board[row][6 + index] = _make_cell(kind, char)
    return board


def _make_cell(kind: str, char: str) -> GLCell:
    if kind == "piece":
        return GLCell("piece", GLPiece(char))
    return GLCell(kind)


def _assign_piece_owners(board, color_quadrant: dict[str, str]) -> None:
    quadrant_color = {q: c for c, q in color_quadrant.items()}
    for row in range(17):
        for col in range(17):
            cell = board[row][col]
            if cell is not None and cell.piece is not None:
                cell.piece.player = quadrant_color.get(_quadrant(row, col))


def _turn_order(first_color: str | None) -> list[str]:
    if first_color not in TURN_ORDER:
        return list(TURN_ORDER)
    index = TURN_ORDER.index(first_color)
    return TURN_ORDER[index:] + TURN_ORDER[:index]


def _resolve_combat(attack: str, defend: str) -> str:
    if defend == "旗":
        return "flag_captured"
    if defend == "雷":
        if attack == "兵":
            return "attacker_wins"
        if attack == "弹":
            return "both_die"
        return "defender_wins"
    if attack == "弹" or defend == "弹":
        return "both_die"
    a_rank = PIECE_RANK.get(attack, -99)
    d_rank = PIECE_RANK.get(defend, -99)
    if a_rank > d_rank:
        return "attacker_wins"
    if a_rank < d_rank:
        return "defender_wins"
    return "both_die"


def _simulate_and_analyze(replay: GLReplay) -> GLAnalysis:
    board = [
        [None if cell is None else GLCell(
            cell.type, None if cell.piece is None else GLPiece(
                cell.piece.char, cell.piece.player))
         for cell in row]
        for row in replay.board
    ]
    initial_values = {color: 0 for color in COLOR_ORDER}
    for row in board:
        for cell in row:
            if cell is not None and cell.piece is not None and cell.piece.player:
                initial_values[cell.piece.player] += PIECE_VALUE.get(
                    cell.piece.char, 0)

    stats = {color: GLStats(color) for color in COLOR_ORDER}
    move_counts = {color: 0 for color in COLOR_ORDER}
    player_time = {color: 0 for color in COLOR_ORDER}
    value_curve: list[tuple[int, dict[str, int]]] = [
        (0, dict(initial_values))]
    values = dict(initial_values)
    key_events: list[dict] = []

    turn = _turn_order(replay.first_move_color)
    turn_index = 0
    active_players: list[str] = []
    last_player: str | None = None
    skip_counts: dict[str, int] = {}
    out: set[str] = set()

    def current_turn() -> str | None:
        return turn[turn_index % len(turn)] if turn else None

    def advance_turn() -> None:
        nonlocal turn_index
        if turn:
            turn_index = (turn_index + 1) % len(turn)

    def clear_player(color: str | None) -> None:
        if not color:
            return
        for row in board:
            for cell in row:
                if cell is not None and cell.piece is not None and cell.piece.player == color:
                    cell.piece = None

    def remove_from_turn(color: str) -> None:
        nonlocal turn_index
        if color not in turn:
            return
        index = turn.index(color)
        turn.pop(index)
        if turn_index > index:
            turn_index -= 1
        if turn and turn_index >= len(turn):
            turn_index = 0

    def check_skips(player: str) -> None:
        nonlocal last_player
        if last_player is None or player not in active_players:
            return
        start = active_players.index(last_player)
        for offset in range(1, len(active_players) + 1):
            candidate = active_players[(start + offset) % len(active_players)]
            if candidate == player:
                break
            if candidate in out:
                continue
            skip_counts[candidate] = skip_counts.get(candidate, 0) + 1
            if skip_counts[candidate] >= 2:
                out.add(candidate)
                clear_player(candidate)
                remove_from_turn(candidate)

    for move in replay.moves:
        move.turn_player = current_turn()
        if move.kind in ("pass", "giveup"):
            player = current_turn()
            move.player = player
            move.result = move.kind
            if player:
                if move.kind == "pass":
                    if player not in active_players:
                        active_players.append(player)
                    skip_counts[player] = 0
                    check_skips(player)
                    last_player = player
                else:
                    check_skips(player)
                    out.add(player)
                    clear_player(player)
                    if player in active_players:
                        index = active_players.index(player)
                        active_players.pop(index)
                        last_player = (
                            active_players[min(index, len(active_players) - 1)]
                            if active_players else None)
                    skip_counts.pop(player, None)
                    remove_from_turn(player)
            advance_turn()
            value_curve.append((move.num, dict(values)))
            continue

        if not move.start or not move.end:
            continue
        src = board[move.start[0]][move.start[1]]
        dst = board[move.end[0]][move.end[1]]
        if src is None or src.piece is None:
            move.player = None
            move.combat = None
            move.result = "invalid"
            value_curve.append((move.num, dict(values)))
            continue

        attacker = src.piece
        player = attacker.player
        move.player = player
        if player:
            if player not in active_players:
                active_players.append(player)
            skip_counts[player] = 0
            check_skips(player)
            last_player = player

        if dst is None or dst.piece is None:
            target_type = dst.type if dst is not None else "piece"
            board[move.end[0]][move.end[1]] = GLCell(target_type, attacker)
            board[move.start[0]][move.start[1]] = GLCell(src.type)
            move.combat = None
            move.result = "move"
        else:
            defender = dst.piece
            result = _resolve_combat(attacker.char, defender.char)
            move.combat = {
                "attacker": {"char": attacker.char, "player": attacker.player},
                "defender": {"char": defender.char, "player": defender.player},
                "result": result,
            }
            move.result = result
            if result == "attacker_wins":
                board[move.end[0]][move.end[1]] = GLCell(dst.type, attacker)
                board[move.start[0]][move.start[1]] = GLCell(src.type)
            elif result == "defender_wins":
                board[move.start[0]][move.start[1]] = GLCell(src.type)
            elif result == "both_die":
                board[move.start[0]][move.start[1]] = GLCell(src.type)
                board[move.end[0]][move.end[1]] = GLCell(dst.type)
            else:
                board[move.end[0]][move.end[1]] = GLCell(dst.type, attacker)
                board[move.start[0]][move.start[1]] = GLCell(src.type)
                clear_player(defender.player)
                out.add(defender.player)
                remove_from_turn(defender.player)

            _apply_combat_stats(stats, values, move, attacker, defender, result)
            event = _classify_event(move, attacker, defender, result)
            if event:
                key_events.append(event)

        if player and player in turn:
            turn_index = (turn.index(player) + 1) % len(turn)
        value_curve.append((move.num, dict(values)))

    if last_player and active_players:
        index = active_players.index(last_player)
        next_player = active_players[(index + 1) % len(active_players)]
        if next_player not in out:
            skip_counts[next_player] = skip_counts.get(next_player, 0) + 1
            if skip_counts[next_player] >= 2:
                out.add(next_player)
                clear_player(next_player)

    for player in replay.players.values():
        if player.is_break:
            clear_player(player.color)

    for st in stats.values():
        st.net_value = st.value_gained - st.value_lost

    result = _determine_result(replay, stats)
    for color, st in stats.items():
        st.rating = _rate_player(st, result, color)
    for move in replay.moves:
        if move.player and move.kind == "move":
            stats[move.player].moves += 1
            move_counts[move.player] += 1
            seconds = _parse_time(move.time1)
            if seconds is not None:
                player_time[move.player] += seconds

    if result.is_draw:
        winner = []
    elif result.winners:
        winner = result.winners
    else:
        winner = [color for color, p in replay.players.items() if not p.is_break]
    return GLAnalysis(
        stats=stats,
        value_curve=value_curve,
        result=result,
        winner=winner,
        key_events=sorted(
            key_events,
            key=lambda item: (-item.get("severity", 0), item.get("step", 0))),
        player_move_counts=move_counts,
        player_time=player_time,
    )


def _apply_combat_stats(stats, values, move, attacker, defender, result):
    a_color = attacker.player
    d_color = defender.player
    if not a_color or not d_color or a_color not in stats or d_color not in stats:
        return
    a_value = PIECE_VALUE.get(attacker.char, 0)
    d_value = PIECE_VALUE.get(defender.char, 0)
    a_stats = stats[a_color]
    d_stats = stats[d_color]
    a_stats.attacks += 1

    if result in ("attacker_wins", "flag_captured"):
        gained = 200 if defender.char == "旗" else d_value
        a_stats.captures += 1
        a_stats.value_gained += gained
        d_stats.losses += 1
        d_stats.value_lost += gained
        values[d_color] = 0 if defender.char == "旗" else values[d_color] - d_value
        if defender.char == "雷" and attacker.char == "兵":
            a_stats.mine_kills += 1
    elif result == "defender_wins":
        d_stats.captures += 1
        d_stats.value_gained += a_value
        a_stats.losses += 1
        a_stats.value_lost += a_value
        values[a_color] -= a_value
        if defender.char == "雷":
            a_stats.mine_losses += 1
    elif result == "both_die":
        a_stats.mutual_destruct += 1
        d_stats.mutual_destruct += 1
        a_stats.value_gained += d_value
        d_stats.value_gained += a_value
        a_stats.value_lost += a_value
        d_stats.value_lost += d_value
        values[a_color] -= a_value
        values[d_color] -= d_value
        if attacker.char == "弹" or defender.char == "弹":
            if attacker.char == "弹":
                a_stats.bomb_kills += 1
                d_stats.bomb_losses += 1
            else:
                d_stats.bomb_kills += 1
                a_stats.bomb_losses += 1
        if defender.char == "雷":
            a_stats.mine_losses += 1


def _classify_event(move: GLMove, attacker: GLPiece, defender: GLPiece,
                    result: str) -> dict | None:
    if result == "flag_captured":
        return {"step": move.num, "category": "扛旗", "severity": 3,
                "text": f"{attacker.player}方{attacker.char}扛走{defender.player}方军旗"}
    if result in ("attacker_wins", "defender_wins", "both_die"):
        if "弹" in (attacker.char, defender.char):
            category = "炸杀" if attacker.char == "弹" else "中炸"
            return {"step": move.num, "category": category, "severity": 3,
                    "text": f"{attacker.char} vs {defender.char}"}
        if defender.char == "雷":
            return {"step": move.num, "category": "触雷", "severity": 2,
                    "text": f"{attacker.char}触雷阵亡"}
        if attacker.char == "兵" and result == "attacker_wins" and defender.char == "雷":
            return {"step": move.num, "category": "挖雷", "severity": 2,
                    "text": "工兵挖雷"}
        if defender.char in ("司令", "军长"):
            return {"step": move.num, "category": "斩将", "severity": 2,
                    "text": f"{attacker.char}斩{defender.char}"}
    return None


def _determine_result(replay: GLReplay, stats: dict[str, GLStats]) -> GLResult:
    breakers = [c for c, p in replay.players.items() if p.is_break]
    desc = replay.game_info.get("简介", "")
    if re.search(r"\bdraw\b", desc, re.I):
        return GLResult(breakers=breakers, is_draw=True, desc=desc)

    win_colors = [c for c, p in replay.players.items() if p.win]
    if len(win_colors) == 2 and _same_team(win_colors[0], win_colors[1], replay):
        winners = [c for c in COLOR_ORDER if c in win_colors]
        losers = [c for c in COLOR_ORDER
                  if c in replay.players and c not in winners]
    else:
        red_blue = stats["red"].net_value + stats["blue"].net_value
        green_gray = stats["green"].net_value + stats["gray"].net_value
        if red_blue >= green_gray:
            winners, losers = ["red", "blue"], ["green", "gray"]
        else:
            winners, losers = ["green", "gray"], ["red", "blue"]
    losers = [c for c in losers if c in replay.players]
    losers += [c for c in breakers if c not in losers]
    return GLResult(winners=winners, losers=losers, breakers=breakers, desc=desc)


def _same_team(a: str, b: str, replay: GLReplay) -> bool:
    pa = replay.players.get(a)
    pb = replay.players.get(b)
    if not pa or not pb:
        return False
    opposite = {"上": "下", "下": "上", "左": "右", "右": "左"}
    return opposite.get(pa.seat) == pb.seat


def _rate_player(stats: GLStats, result: GLResult, color: str) -> str:
    if color in result.breakers:
        return "D"
    net = stats.net_value
    if color in result.winners:
        if net > 80:
            return "S"
        if net > 0:
            return "A"
        return "B"
    if net > 50:
        return "A"
    if net > 0:
        return "B"
    if net > -50:
        return "C"
    return "D"


def _parse_time(value: str) -> int | None:
    match = re.match(r"^(\d+):(\d+)$", value or "")
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def summary_text(replay: GLReplay) -> str:
    analysis = replay.analyze()
    lines = [f"文件: {replay.path}", f"棋子数: {replay.piece_count}",
             f"行棋: {len(replay.moves)}"]
    for color in COLOR_ORDER:
        player = replay.players.get(color)
        stats = analysis.stats.get(color)
        if not player or not stats:
            continue
        lines.append(
            f"{COLOR_CN[color]}({player.seat}家) {player.name}: "
            f"步数={stats.moves} 吃子={stats.captures} 被吃={stats.losses} "
            f"同尽={stats.mutual_destruct} 净值={stats.net_value} "
            f"评级={stats.rating}")
    lines.append("胜方: " + "、".join(
        analysis.stats[c].color for c in analysis.winner))
    return "\n".join(lines)
