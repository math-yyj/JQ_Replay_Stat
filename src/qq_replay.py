"""QQ 四国军棋 .jgs 完整复盘解析。

移植自 junqiwang.cn QQ 复盘分析器的解析与结算逻辑：
- 0x20 起 4×88 字节玩家块，+0x1C 起 30 字节布阵；
- 0x19B/0x19C 起 10 字节指令流（0x5F 棋步 / 0xF5 事件）；
- 24 个“颜色→方位”双射按行棋自洽率推导；
- 17×17 棋盘铺盘、军旗大本营对齐、行棋结算与胜负判定。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Iterable

from .jgs_types import ItemType, item_type2string

MIN_FILE_SIZE = 0x19B
PLAYER_BLOCK_OFFSET = 0x20
PLAYER_BLOCK_SIZE = 88
INSTRUCTION_SIZE = 10
MOVE_INSTRUCTION = 0x5F
INFO_INSTRUCTION = 0xF5

BYTE_COLOR = ("黄", "蓝", "绿", "紫")
COLOR_ORDER = ("绿", "黄", "蓝", "紫")
DIRS = ("up", "down", "left", "right")
DIR_CN = {"up": "上", "down": "下", "left": "左", "right": "右"}
DEFAULT_ORIENT = {"绿": "up", "黄": "down", "蓝": "left", "紫": "right"}
QQ2DIR = {0: "down", 1: "left", 2: "up", 3: "right"}

FLAG_H_DIR = {
    "up": ((0, 7), (0, 9)),
    "down": ((16, 7), (16, 9)),
    "left": ((7, 0), (9, 0)),
    "right": ((7, 16), (9, 16)),
}

PIECE_NAMES = {
    int(ItemType.Junqi): "军旗",
    int(ItemType.Dilei): "地雷",
    int(ItemType.Zhadan): "炸弹",
    int(ItemType.Silin): "司令",
    int(ItemType.Junzhang): "军长",
    int(ItemType.Shizhang): "师长",
    int(ItemType.Lvzhang): "旅长",
    int(ItemType.Tuanzhang): "团长",
    int(ItemType.Yinzhang): "营长",
    int(ItemType.Lianzhang): "连长",
    int(ItemType.Paizhang): "排长",
    int(ItemType.Gongbing): "工兵",
}
PIECE_SHORT = {
    code: item_type2string(code) for code in PIECE_NAMES
}
NAME_PIECE = {name: code for code, name in PIECE_NAMES.items()}

# 页面使用的复盘分析分值，与旧 C++ 引擎的 item_score 不是同一套。
VALUE = {
    "司令": 100, "军长": 80, "师长": 60, "旅长": 45, "团长": 35,
    "营长": 25, "连长": 18, "排长": 12, "工兵": 8, "炸弹": 35,
    "地雷": 15, "军旗": 0,
}

GROUND = "ground"
CAMP = "camp"
FLAG = "flag"
MOVABLE_EXCLUDE = {int(ItemType.Dilei), int(ItemType.Junqi)}

RESULT_TAG = {2: "被吃", 3: "同尽"}
RESULT_TEXT = {0: "移动", 1: "吃子", 2: "被吃", 3: "同尽"}
EVENT_TEXT = {2: "超时", 3: "自杀战败", 4: "退出游戏"}
END_RESULT = {2: "和局", 4: "一方战败", 6: "有玩家逃跑"}

COMBAT_TYPES = {"eat", "killed", "both", "dig", "mine", "flag"}


@dataclass
class BoardPiece:
    color: str
    piece: int


@dataclass
class PlayerBlock:
    byte_color: int
    color: str
    name: str
    qq_number: int
    matrix: list[int]
    orient: str = ""
    seat: str = ""


@dataclass
class Move:
    no: int
    color: str
    start: tuple[int, int]
    end: tuple[int, int]
    result: int
    tag: str = ""

    @property
    def text(self) -> str:
        suffix = f" {self.tag}" if self.tag else ""
        return (f"{self.color} {self.start[0]},{self.start[1]}"
                f"-{self.end[0]},{self.end[1]}{suffix}")


@dataclass
class GameEvent:
    no: int
    code: int
    a: int
    b: int
    c: int
    color: str | None = None
    text: str = ""


@dataclass
class ResolvedMove:
    type: str
    desc: str
    delta: dict[str, int]
    event_cat: str | None = None
    event_text: str | None = None
    severity: int = 0
    gain: int = 0
    winner: str | None = None
    from_piece: int | None = None
    def_piece: int | None = None
    defender: str | None = None
    error: bool = False


@dataclass
class PlayerStats:
    color: str
    steps: int = 0
    eat: int = 0
    killed: int = 0
    both: int = 0
    reversal: int = 0
    bomb_kill: int = 0
    bomb_hit: int = 0
    dig: int = 0
    flag: int = 0
    mine: int = 0
    mistakes: int = 0
    mistake_loss: int = 0
    score: int = 0
    loss: int = 0
    net: int = 0
    exchanges: int = 0
    initiative: int = 0
    captured: list[dict] = field(default_factory=list)
    lost: list[dict] = field(default_factory=list)
    lost_pieces: dict[str, int] = field(default_factory=dict)

    def lose_piece(self, piece: int, step: int, killer: str, kind: str) -> None:
        name = PIECE_NAMES.get(piece, "?")
        self.lost.append({"step": step, "piece": name, "color": killer, "kind": kind})
        self.lost_pieces[name] = self.lost_pieces.get(name, 0) + 1


@dataclass
class AnalyzedStep:
    no: int
    kind: str
    move: Move | None = None
    event: GameEvent | None = None
    resolved: ResolvedMove | None = None
    net: dict[str, int] = field(default_factory=dict)
    event_text: str = ""


@dataclass
class ReplayAnalysis:
    stats: dict[str, PlayerStats]
    steps: list[AnalyzedStep]
    curve: list[tuple[int, dict[str, int]]]
    alive: dict[str, bool]
    defeated_at: dict[str, int]
    winner: list[str] | None
    result_text: str
    errors: list[tuple[int, str]]
    end_event: GameEvent | None
    self_check_rate: float = 0.0

    @property
    def error_count(self) -> int:
        return len(self.errors)


@dataclass
class QQReplay:
    path: str
    file_size: int
    header_steps: int
    my_color_byte: int
    my_color: str | None
    blocks: list[PlayerBlock]
    players: dict[str, str]
    orient: dict[str, str]
    orient_src: dict[str, str]
    dir_to_color: dict[str, str]
    turn_order: list[str]
    board: dict[tuple[int, int], BoardPiece]
    moves: list[Move]
    events: list[GameEvent]
    seq: list[tuple[str, Move | GameEvent]]
    self_check_rate: float
    end_event: GameEvent | None = None
    _analysis: ReplayAnalysis | None = field(default=None, repr=False)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def move_count(self) -> int:
        return len(self.moves)

    @property
    def seat_map(self) -> dict[str, str]:
        return {color: f"{DIR_CN[d]}家" for color, d in self.orient.items()}

    @property
    def player_names(self) -> dict[str, str]:
        return {b.color: b.name for b in self.blocks}

    def analyze(self) -> ReplayAnalysis:
        if self._analysis is None:
            self._analysis = _analyze(self)
        return self._analysis

    def to_text(self) -> str:
        return _to_text(self, self.analyze())

    def to_dict(self, include_steps: bool = True) -> dict:
        analysis = self.analyze()
        players = {}
        for block in self.blocks:
            players[block.color] = {
                "name": block.name,
                "qq": block.qq_number,
                "direction": block.orient,
                "seat": block.seat,
                "layout": [
                    block.matrix[r * 5:(r + 1) * 5] for r in range(6)
                ],
            }
        moves = []
        for step in analysis.steps:
            if step.kind == "event":
                ev = step.event
                moves.append({
                    "no": step.no,
                    "type": "event",
                    "color": ev.color,
                    "event": step.event_text,
                    "net": step.net,
                })
                continue
            mv = step.move
            res = step.resolved
            moves.append({
                "no": step.no,
                "type": "move",
                "color": mv.color,
                "from": list(mv.start),
                "to": list(mv.end),
                "result": mv.result,
                "tag": mv.tag,
                "desc": res.desc,
                "resolve": res.type,
                "delta": res.delta,
                "event": res.event_text,
                "error": res.error,
                "net": step.net,
            })
        stats = {}
        for color, st in analysis.stats.items():
            stats[color] = {
                "color": color,
                "name": self.players.get(color, ""),
                "seat": self.seat_map.get(color, ""),
                "steps": st.steps,
                "eat": st.eat,
                "killed": st.killed,
                "both": st.both,
                "reversal": st.reversal,
                "bomb_kill": st.bomb_kill,
                "bomb_hit": st.bomb_hit,
                "dig": st.dig,
                "flag": st.flag,
                "mine": st.mine,
                "mistakes": st.mistakes,
                "mistake_loss": st.mistake_loss,
                "score": st.score,
                "loss": st.loss,
                "net": st.net,
                "exchanges": st.exchanges,
                "initiative": st.initiative,
                "captured": st.captured,
                "lost": st.lost,
                "lost_pieces": st.lost_pieces,
            }
        board = [
            {"row": r, "col": c, "color": p.color, "piece": PIECE_NAMES.get(p.piece, "?")}
            for (r, c), p in sorted(self.board.items())
        ]
        data = {
            "file": self.path,
            "size": self.file_size,
            "header_steps": self.header_steps,
            "my_color": self.my_color,
            "my_color_byte": self.my_color_byte,
            "orient": self.orient,
            "orient_src": self.orient_src,
            "seat_map": self.seat_map,
            "turn_order": self.turn_order,
            "self_check_rate": self.self_check_rate,
            "players": players,
            "board": board,
            "stats": stats,
            "result": analysis.result_text,
            "winner": analysis.winner,
            "alive": analysis.alive,
            "defeated_at": analysis.defeated_at,
            "errors": [{"step": n, "desc": d} for n, d in analysis.errors],
            "curve": [{"step": n, "net": net} for n, net in analysis.curve],
            "moves": moves if include_steps else [],
        }
        return data


def _decode_name(raw: bytes) -> str:
    raw = raw.split(b"\x00", 1)[0]
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def map_layout(direction: str, row: int, col: int) -> tuple[int, int]:
    """自视角布阵坐标 (row 0 前线, col 0 左手) -> 17×17 棋盘坐标。"""
    if direction == "up":
        return 5 - row, 10 - col
    if direction == "down":
        return 11 + row, 6 + col
    if direction == "left":
        return 6 + col, 5 - row
    return 10 - col, 11 + row


def _orientations() -> Iterable[dict[str, str]]:
    for perm in itertools.permutations(DIRS):
        yield dict(zip(COLOR_ORDER, perm))


def _layout_by_orient(blocks: list[PlayerBlock], orient: dict[str, str]) -> dict[tuple[int, int], str]:
    """按方位假设铺盘，只记录格子归属颜色；用于方位自洽率打分。"""
    board: dict[tuple[int, int], str] = {}
    for block in blocks:
        direction = orient[block.color]
        laid: dict[tuple[int, int], int] = {}
        for index, value in enumerate(block.matrix):
            if value not in PIECE_NAMES:
                continue
            key = map_layout(direction, index // 5, index % 5)
            if key in laid:
                continue
            laid[key] = value
        flag_key = next((k for k, v in laid.items() if v == int(ItemType.Junqi)), None)
        if flag_key is not None:
            homes = FLAG_H_DIR[direction]
            if flag_key not in homes:
                for home in homes:
                    if home not in laid:
                        laid[home] = laid.pop(flag_key)
                        break
        for key in laid:
            board[key] = block.color
    return board


def _orientation_score(blocks: list[PlayerBlock],
                       seq: list[tuple[str, Move | GameEvent]],
                       orient: dict[str, str]) -> float:
    board = _layout_by_orient(blocks, orient)
    total = 0
    ok = 0
    for kind, item in seq:
        if kind == "event":
            if item.code in (3, 4) and item.color:
                for key in [k for k, c in board.items() if c == item.color]:
                    del board[key]
            continue
        total += 1
        start = item.start
        if board.get(start) == item.color:
            ok += 1
        if item.result in (0, 1):
            if start in board:
                board[item.end] = board.pop(start)
        elif item.result == 2:
            board.pop(start, None)
        elif item.result == 3:
            board.pop(start, None)
            board.pop(item.end, None)
    return ok / total if total else 0.0


def _pick_orientation(blocks: list[PlayerBlock],
                      seq: list[tuple[str, Move | GameEvent]]) -> tuple[dict[str, str], float]:
    best: dict[str, str] | None = None
    best_score = -1.0
    for orient in _orientations():
        prior = sum(
            1 for block in blocks
            if orient[block.color] == QQ2DIR.get(block.byte_color)
        )
        score = _orientation_score(blocks, seq, orient) + prior * 1e-6
        if score > best_score + 1e-12:
            best_score = score
            best = orient
    assert best is not None
    return best, _orientation_score(blocks, seq, best)


def _place_layouts(blocks: list[PlayerBlock],
                   orient: dict[str, str]) -> dict[tuple[int, int], BoardPiece]:
    board: dict[tuple[int, int], BoardPiece] = {}
    for block in blocks:
        direction = orient[block.color]
        laid: dict[tuple[int, int], int] = {}
        for index, value in enumerate(block.matrix):
            if value not in PIECE_NAMES:
                continue
            key = map_layout(direction, index // 5, index % 5)
            if key in laid:
                continue
            if key in board and board[key].color != block.color:
                continue
            laid[key] = value
        flag_key = next((k for k, v in laid.items() if v == int(ItemType.Junqi)), None)
        if flag_key is not None:
            homes = FLAG_H_DIR[direction]
            if flag_key not in homes:
                for home in homes:
                    if home not in laid:
                        laid[home] = laid.pop(flag_key)
                        break
        for key, piece in laid.items():
            board[key] = BoardPiece(block.color, piece)
    return board


def _extract_instructions(data: bytes, start: int) -> tuple[list[Move], list[GameEvent],
                                                            list[tuple[str, Move | GameEvent]]]:
    moves: list[Move] = []
    events: list[GameEvent] = []
    seq: list[tuple[str, Move | GameEvent]] = []

    ring = ["蓝", "绿", "紫", "黄"]
    bit_of_color = {"蓝": 0, "绿": 1, "紫": 2, "黄": 3}
    color_of_bit: dict[int, str | None] = {0: "蓝", 1: "绿", 2: "紫", 3: "黄"}
    active = {"黄": True, "蓝": True, "绿": True, "紫": True}

    def apply_exit(color: str | None) -> None:
        if not color or not active.get(color):
            return
        active[color] = False
        bit = bit_of_color[color]
        index = ring.index(color)
        for offset in range(1, len(ring) + 1):
            candidate = ring[(index + offset) % len(ring)]
            if active[candidate]:
                candidate_bit = bit_of_color[candidate]
                color_of_bit[bit] = candidate
                bit_of_color[candidate] = bit
                if candidate_bit != bit:
                    color_of_bit[candidate_bit] = None
                break

    pos = start
    while pos + INSTRUCTION_SIZE <= len(data):
        kind = data[pos]
        if kind == MOVE_INSTRUCTION:
            b1 = data[pos + 1]
            color = color_of_bit.get((b1 >> 3) & 3)
            if color:
                y1, x1, y2, x2 = data[pos + 2], data[pos + 3], data[pos + 4], data[pos + 5]
                if all(v <= 16 for v in (y1, x1, y2, x2)):
                    move = Move(
                        no=0,
                        color=color,
                        start=(16 - y1, x1),
                        end=(16 - y2, x2),
                        result=b1 & 3,
                        tag=RESULT_TAG.get(b1 & 3, ""),
                    )
                    moves.append(move)
                    seq.append(("move", move))
        elif kind == INFO_INSTRUCTION:
            event = GameEvent(
                no=0,
                code=data[pos + 1],
                a=data[pos + 2],
                b=data[pos + 3],
                c=data[pos + 4],
                color=BYTE_COLOR[data[pos + 2]] if data[pos + 2] < 4 else None,
            )
            events.append(event)
            # 结束事件只用于结果展示，不进入行棋步序列（与页面 jgsToText 一致）
            if event.code != 5:
                seq.append(("event", event))
                if event.code in (3, 4):
                    apply_exit(event.color)
        pos += INSTRUCTION_SIZE
    return moves, events, seq


def parse_jgs(path: str | os.PathLike) -> QQReplay:
    with open(path, "rb") as f:
        data = f.read()
    return parse_jgs_bytes(data, str(path))


def parse_jgs_bytes(data: bytes, path: str = "<bytes>") -> QQReplay:
    if len(data) < MIN_FILE_SIZE:
        raise ValueError(f"文件不完整（{len(data)} 字节，至少需要 {MIN_FILE_SIZE} 字节）")
    if data[:10] != b"QQGame JQS":
        raise ValueError("文件头不是 QQGame JQS，请确认是 QQ 四国军棋复盘(.jgs)文件")

    header_steps = int.from_bytes(data[0x1A:0x1C], "little")
    my_color_byte = data[0x0F]
    my_color = BYTE_COLOR[my_color_byte] if my_color_byte < 4 else None

    blocks: list[PlayerBlock] = []
    for index in range(4):
        offset = PLAYER_BLOCK_OFFSET + index * PLAYER_BLOCK_SIZE
        if offset + 0x3A > len(data):
            break
        byte_color = data[offset]
        if byte_color not in QQ2DIR:
            continue
        matrix = list(data[offset + 0x1C:offset + 0x1C + 30])
        qq_number = int.from_bytes(data[offset + 0x3C:offset + 0x40], "little")
        blocks.append(PlayerBlock(
            byte_color=byte_color,
            color=BYTE_COLOR[byte_color],
            name=_decode_name(data[offset + 0x08:offset + 0x1C]) or f"玩家{index + 1}",
            qq_number=qq_number,
            matrix=matrix,
        ))
    if not blocks:
        raise ValueError("未找到有效的玩家块")

    first = _extract_instructions(data, 0x19B)
    second = _extract_instructions(data, 0x19C)
    moves, events, seq = second if len(second[0]) > len(first[0]) else first

    for number, (kind, item) in enumerate(seq, 1):
        item.no = number

    orient, self_check_rate = _pick_orientation(blocks, seq)
    for block in blocks:
        block.orient = orient[block.color]
        block.seat = f"{DIR_CN[block.orient]}家"

    turn_order = [
        next((c for c, d in orient.items() if d == direction), None)
        for direction in ("up", "left", "down", "right")
    ]
    turn_order = [c for c in turn_order if c]
    if not turn_order:
        turn_order = list(COLOR_ORDER)

    orient_src = {}
    moved_colors = {m.color for m in moves}
    for block in blocks:
        orient_src[block.color] = "geo" if block.color in moved_colors else "byte"

    board = _place_layouts(blocks, orient)
    end_event = next((ev for ev in reversed(events) if ev.code == 5), None)
    if end_event:
        end_event.text = END_RESULT.get(end_event.b, "对局结束")
    for event in events:
        if event.code == 2:
            event.text = "超时判负" if event.b >= 5 else "超时"
        elif event.code == 3:
            event.text = "自杀战败"
        elif event.code == 4:
            event.text = "退出游戏"

    return QQReplay(
        path=path,
        file_size=len(data),
        header_steps=header_steps,
        my_color_byte=my_color_byte,
        my_color=my_color,
        blocks=blocks,
        players={b.color: b.name for b in blocks},
        orient=orient,
        orient_src=orient_src,
        dir_to_color={d: c for c, d in orient.items()},
        turn_order=turn_order,
        board=board,
        moves=moves,
        events=events,
        seq=seq,
        self_check_rate=self_check_rate,
        end_event=end_event,
    )


def _auto_result(attack: int, defender: int | None) -> str:
    if defender is None:
        return "move"
    if defender == int(ItemType.Junqi):
        return "flag"
    if defender == int(ItemType.Dilei):
        if attack == int(ItemType.Gongbing):
            return "dig"
        if attack == int(ItemType.Zhadan):
            return "both"
        return "mine"
    if attack == int(ItemType.Zhadan) or defender == int(ItemType.Zhadan):
        return "both"
    a = VALUE[PIECE_NAMES[attack]]
    d = VALUE[PIECE_NAMES[defender]]
    if a > d:
        return "eat"
    if a == d:
        return "both"
    return "killed"


def _tag_type(tag: str) -> str | None:
    if not tag:
        return None
    if tag.startswith("吃"):
        return "eat"
    return {
        "被吃": "killed",
        "同尽": "both",
        "中炸": "both",
        "挖雷": "dig",
        "扛旗": "flag",
        "反弹": "bounce",
    }.get(tag)


def _remove_all(board: dict[tuple[int, int], BoardPiece], color: str) -> None:
    for key in [k for k, p in board.items() if p.color == color]:
        del board[key]


def _movable_left(board: dict[tuple[int, int], BoardPiece], color: str) -> bool:
    return any(p.color == color and p.piece not in MOVABLE_EXCLUDE for p in board.values())


def resolve_move(move: Move, board: dict[tuple[int, int], BoardPiece]) -> ResolvedMove:
    delta = {color: 0 for color in COLOR_ORDER}
    attacker = board.get(move.start)
    if attacker is None:
        return ResolvedMove(
            type="error", desc="错误：起点无子", delta=delta, error=True,
        )
    target = board.get(move.end)
    if target is not None and target.color == attacker.color:
        return ResolvedMove(
            type="error", desc="错误：目标为友军棋子", delta=delta,
            from_piece=attacker.piece, error=True,
        )

    result_type = _tag_type(move.tag)
    if result_type is None:
        result_type = _auto_result(attacker.piece, target.piece if target else None)
    if result_type == "move" and target is not None and target.color != attacker.color:
        result_type = _auto_result(attacker.piece, target.piece)

    attack = attacker.piece
    defence = target.piece if target else None
    a_name = PIECE_NAMES.get(attack, "?")
    d_name = PIECE_NAMES.get(defence, "?") if defence is not None else ""
    a_value = VALUE.get(a_name, 0)
    d_value = VALUE.get(d_name, 0)
    defender_color = target.color if target else None
    resolved = ResolvedMove(
        type=result_type,
        desc="移动",
        delta=delta,
        from_piece=attack,
        def_piece=defence,
        defender=defender_color,
    )

    if result_type == "bounce":
        resolved.desc = "非法行走（反弹，未生效）"
        resolved.error = True
        return resolved

    if result_type == "move":
        del board[move.start]
        board[move.end] = BoardPiece(attacker.color, attack)
        resolved.desc = "移动"
        return resolved

    if result_type == "eat":
        delta[attacker.color] += d_value
        if defender_color:
            delta[defender_color] -= d_value
        resolved.gain = d_value
        resolved.winner = attacker.color
        del board[move.start]
        board.pop(move.end, None)
        board[move.end] = BoardPiece(attacker.color, attack)
        resolved.desc = f"吃{d_name}"
        if d_name in ("司令", "军长"):
            resolved.event_cat = "斩将"
            resolved.severity = 3 if d_name == "司令" else 2
            resolved.event_text = f"{attacker.color}方{a_name}斩杀{defender_color}方{d_name}"
        else:
            resolved.event_cat = "吃子"
            resolved.severity = 1
            resolved.event_text = f"{attacker.color}方{a_name}吃{defender_color}方{d_name}"
        if a_value < d_value:
            resolved.event_cat = "失着"
            resolved.severity = 2
            resolved.event_text = f"{defender_color}方{d_name}被{a_name}反吃，属防守失着"
        return resolved

    if result_type == "killed":
        delta[attacker.color] -= a_value
        if defender_color:
            delta[defender_color] += a_value
        resolved.gain = -a_value
        resolved.winner = defender_color
        del board[move.start]
        board.pop(move.end, None)
        if target is not None:
            board[move.end] = target
        resolved.desc = f"{a_name}被{d_name or '敌子'}吃"
        resolved.event_cat = "失着"
        resolved.severity = 2
        resolved.event_text = f"{attacker.color}方{a_name}主动撞{defender_color}方{d_name}被反吃"
        return resolved

    if result_type == "both":
        delta[attacker.color] += d_value - a_value
        if defender_color:
            delta[defender_color] += a_value - d_value
        resolved.gain = d_value - a_value
        del board[move.start]
        board.pop(move.end, None)
        resolved.desc = f"同尽（{a_name}vs{d_name}）"
        if a_name == "炸弹":
            resolved.event_cat = "炸杀"
            resolved.severity = 3
            resolved.event_text = f"{attacker.color}方炸弹炸掉{defender_color}方{d_name}"
        elif d_name == "炸弹":
            resolved.event_cat = "中炸"
            resolved.severity = 3
            resolved.event_text = f"{attacker.color}方{a_name}撞上{defender_color}方炸弹同尽"
        elif a_value == d_value:
            resolved.event_cat = "同尽"
            resolved.severity = 3 if "司令" in (a_name, d_name) else 2
            resolved.event_text = f"{attacker.color}方{a_name}与{defender_color}方{d_name}同归于尽"
        if d_name != "炸弹" and d_value > a_value:
            resolved.event_cat = "失着"
            resolved.severity = 2
            resolved.event_text = f"{defender_color}方{d_name}被{a_name}换掉，属失着"
        return resolved

    if result_type == "dig":
        delta[attacker.color] += VALUE["地雷"]
        if defender_color:
            delta[defender_color] -= VALUE["地雷"]
        resolved.gain = VALUE["地雷"]
        resolved.winner = attacker.color
        del board[move.start]
        del board[move.end]
        board[move.end] = BoardPiece(attacker.color, attack)
        resolved.desc = f"挖雷（{a_name}挖掉地雷）"
        resolved.event_cat = "挖雷"
        resolved.severity = 2
        resolved.event_text = f"{attacker.color}方{a_name}挖掉{defender_color}方地雷"
        return resolved

    if result_type == "mine":
        delta[attacker.color] -= a_value
        if defender_color:
            delta[defender_color] += a_value
        resolved.gain = -a_value
        resolved.winner = defender_color
        del board[move.start]
        resolved.desc = f"触雷（{a_name}碰雷阵亡）"
        resolved.event_cat = "触雷"
        resolved.severity = 2
        resolved.event_text = f"{attacker.color}方{a_name}触{defender_color}方地雷阵亡"
        return resolved

    if result_type == "flag":
        del board[move.start]
        board[move.end] = BoardPiece(attacker.color, attack)
        resolved.desc = f"扛旗！夺得{defender_color}方军旗"
        resolved.event_cat = "扛旗"
        resolved.severity = 3
        resolved.event_text = f"{attacker.color}方{a_name}杀入大本营扛走{defender_color}方军旗"
        if defender_color:
            _remove_all(board, defender_color)
        return resolved

    return resolved


def _check_defeat_before_move(board: dict[tuple[int, int], BoardPiece], color: str,
                              alive: dict[str, bool], defeated_at: dict[str, int],
                              step_no: int) -> None:
    if alive.get(color) and not _movable_left(board, color):
        alive[color] = False
        defeated_at[color] = step_no
        _remove_all(board, color)


def _track_turn_skip(move: Move, step_no: int, turn_order: list[str],
                     alive: dict[str, bool], defeated_at: dict[str, int],
                     skip_count: dict[str, int], turn_idx: int,
                     board: dict[tuple[int, int], BoardPiece]) -> int:
    if not alive.get(move.color) or move.color not in turn_order:
        return turn_idx
    target_index = turn_order.index(move.color)
    count = len(turn_order)
    for offset in range(1, count + 1):
        index = (turn_idx + offset) % count
        if index == target_index:
            break
        color = turn_order[index]
        if not alive.get(color):
            continue
        skip_count[color] = skip_count.get(color, 0) + 1
        if skip_count[color] >= 2:
            alive[color] = False
            defeated_at[color] = step_no
            _remove_all(board, color)
    skip_count[move.color] = 0
    return target_index


def _team_split(orient: dict[str, str]) -> tuple[list[str], list[str]] | None:
    up_down = [c for c, d in orient.items() if d in ("up", "down")]
    left_right = [c for c, d in orient.items() if d in ("left", "right")]
    if len(up_down) != 2 or len(left_right) != 2:
        return None
    return up_down, left_right


def _decide_winner(replay: QQReplay, alive: dict[str, bool],
                   defeated_at: dict[str, int]) -> tuple[list[str] | None, str]:
    split = _team_split(replay.orient)
    if split:
        team_a, team_b = split
        out_a = [c for c in team_a if not alive.get(c, True)]
        out_b = [c for c in team_b if not alive.get(c, True)]
        if len(out_b) == 2 and len(out_a) < 2:
            return team_a, "、".join(replay.players.get(c, c) for c in team_a) + " 胜"
        if len(out_a) == 2 and len(out_b) < 2:
            return team_b, "、".join(replay.players.get(c, c) for c in team_b) + " 胜"
        if len(out_a) == 2 and len(out_b) == 2:
            last_a = max(defeated_at.get(c, 0) for c in team_a)
            last_b = max(defeated_at.get(c, 0) for c in team_b)
            if last_a != last_b:
                winners = team_a if last_a > last_b else team_b
                return winners, "、".join(replay.players.get(c, c) for c in winners) + " 胜"
    if replay.end_event and replay.end_event.b == 2:
        return None, "和局"
    if replay.end_event and replay.end_event.b == 6:
        return None, "有玩家逃跑"
    return None, "未分胜负"


def _analyze(replay: QQReplay) -> ReplayAnalysis:
    board = dict(replay.board)
    stats = {color: PlayerStats(color) for color in COLOR_ORDER}
    alive = {color: True for color in COLOR_ORDER}
    defeated_at: dict[str, int] = {}
    net = {color: 0 for color in COLOR_ORDER}
    skip_count = {color: 0 for color in COLOR_ORDER}
    turn_idx = len(replay.turn_order) - 1
    steps: list[AnalyzedStep] = []
    curve: list[tuple[int, dict[str, int]]] = []
    errors: list[tuple[int, str]] = []

    def snapshot() -> dict[str, int]:
        return dict(net)

    for kind, item in replay.seq:
        if kind == "event":
            event = item
            color = event.color
            is_timeout = event.code == 2
            is_defeat = (event.code == 2 and event.b >= 5) or event.code in (3, 4)
            if is_defeat and color and alive.get(color):
                alive[color] = False
                defeated_at[color] = event.no
                _remove_all(board, color)
            if is_timeout and color in replay.turn_order:
                turn_idx = replay.turn_order.index(color)
                skip_count[color] = 0
            text = event.text or EVENT_TEXT.get(event.code, "事件")
            if is_timeout and not is_defeat:
                text = f"超时（累计第 {event.b} 次，尚未判负）"
            elif event.code == 3 and steps:
                previous = steps[-1]
                if (previous.resolved is not None
                        and previous.resolved.type == "flag"
                        and previous.resolved.defender == color):
                    text = "军旗被扛，战败"
            steps.append(AnalyzedStep(
                no=event.no, kind="event", event=event,
                net=snapshot(), event_text=text,
            ))
            curve.append((event.no, snapshot()))
            continue

        move = item
        if alive.get(move.color):
            _check_defeat_before_move(board, move.color, alive, defeated_at, move.no)
        turn_idx = _track_turn_skip(
            move, move.no, replay.turn_order, alive, defeated_at,
            skip_count, turn_idx, board,
        )
        resolved = resolve_move(move, board)
        color = move.color
        st = stats[color]
        st.steps += 1
        for c in COLOR_ORDER:
            net[c] += resolved.delta.get(c, 0)

        result_type = resolved.type
        attack = resolved.from_piece
        defence = resolved.def_piece
        defender = resolved.defender
        defender_stats = stats.get(defender) if defender else None
        attack_name = PIECE_NAMES.get(attack, "?") if attack is not None else "?"
        defence_name = PIECE_NAMES.get(defence, "?") if defence is not None else ""
        attack_value = VALUE.get(attack_name, 0)
        defence_value = VALUE.get(defence_name, 0)

        if resolved.error:
            errors.append((move.no, resolved.desc))

        if result_type == "eat":
            st.eat += 1
            st.score += defence_value
            st.captured.append({"step": move.no, "piece": defence_name,
                                "color": defender, "kind": "吃"})
            if defender_stats:
                defender_stats.killed += 1
                defender_stats.loss += defence_value
                defender_stats.lose_piece(defence, move.no, color, "被吃")
                if attack_value < defence_value:
                    defender_stats.mistakes += 1
                    defender_stats.mistake_loss += defence_value - attack_value
        elif result_type == "killed":
            st.killed += 1
            st.reversal += 1
            st.loss += attack_value
            st.mistakes += 1
            st.mistake_loss += attack_value
            st.lose_piece(attack, move.no, defender or "", "被吃")
            if defender_stats:
                defender_stats.eat += 1
                defender_stats.score += attack_value
                defender_stats.captured.append({
                    "step": move.no, "piece": attack_name,
                    "color": color, "kind": "吃",
                })
        elif result_type == "both":
            st.both += 1
            st.score += defence_value
            st.loss += attack_value
            st.lose_piece(attack, move.no, defender or "", "同尽")
            st.captured.append({"step": move.no, "piece": defence_name,
                                "color": defender, "kind": "对掉"})
            if defender_stats:
                defender_stats.both += 1
                defender_stats.score += attack_value
                defender_stats.loss += defence_value
                defender_stats.lose_piece(defence, move.no, color, "同尽")
                defender_stats.captured.append({
                    "step": move.no, "piece": attack_name,
                    "color": color, "kind": "对掉",
                })
                if attack_name == "炸弹":
                    defender_stats.bomb_hit += 1
                if defence_name == "炸弹":
                    defender_stats.bomb_kill += 1
                if defence_name != "炸弹" and defence_value > attack_value:
                    defender_stats.mistakes += 1
                    defender_stats.mistake_loss += defence_value - attack_value
            if attack_name == "炸弹":
                st.bomb_kill += 1
            if defence_name == "炸弹":
                st.bomb_hit += 1
        elif result_type == "dig":
            st.dig += 1
            st.score += VALUE["地雷"]
            st.captured.append({"step": move.no, "piece": "地雷",
                                "color": defender, "kind": "挖"})
            if defender_stats:
                defender_stats.loss += VALUE["地雷"]
                defender_stats.lose_piece(int(ItemType.Dilei), move.no, color, "被挖")
        elif result_type == "mine":
            st.mine += 1
            st.loss += attack_value
            st.mistakes += 1
            st.mistake_loss += attack_value
            st.lose_piece(attack, move.no, defender or "", "触雷")
            if defender_stats:
                defender_stats.eat += 1
                defender_stats.score += attack_value
                defender_stats.captured.append({
                    "step": move.no, "piece": attack_name,
                    "color": color, "kind": "吃",
                })
        elif result_type == "flag":
            st.flag += 1
            st.captured.append({"step": move.no, "piece": "军旗",
                                "color": defender, "kind": "扛"})
            if defender and alive.get(defender):
                alive[defender] = False
                defeated_at[defender] = move.no

        if result_type in COMBAT_TYPES:
            st.exchanges += 1
            st.initiative += 1
            if defender_stats and defender != color:
                defender_stats.exchanges += 1

        steps.append(AnalyzedStep(
            no=move.no, kind="move", move=move, resolved=resolved,
            net=snapshot(), event_text=resolved.event_text or "",
        ))
        curve.append((move.no, snapshot()))

    for st in stats.values():
        st.net = st.score - st.loss

    winner, result_text = _decide_winner(replay, alive, defeated_at)
    return ReplayAnalysis(
        stats=stats,
        steps=steps,
        curve=curve,
        alive=alive,
        defeated_at=defeated_at,
        winner=winner,
        result_text=result_text,
        errors=errors,
        end_event=replay.end_event,
        self_check_rate=replay.self_check_rate,
    )


def _to_text(replay: QQReplay, analysis: ReplayAnalysis) -> str:
    lines = [
        "QQ四国军棋复盘 v2.0",
        "[对局信息]",
        "来源=QQ四国军棋复盘文件(.jgs)转换",
        f"文件={os.path.basename(replay.path)}",
        f"人数={len(replay.blocks)}",
    ]
    if replay.my_color:
        lines.append(f"己方={replay.my_color}")
    direction_text = " ".join(
        f"{DIR_CN[d]}:{replay.dir_to_color[d]}"
        for d in ("up", "right", "down", "left")
        if d in replay.dir_to_color
    )
    if direction_text:
        lines.append(f"方位={direction_text}")
    lines.append(f"结果={analysis.result_text}")
    lines.append("[玩家]")
    for color in COLOR_ORDER:
        if color in replay.players:
            lines.append(f"{color}={replay.players[color]}")
    lines.append("[布局]")
    by_color: dict[str, list[tuple[int, int, int]]] = {c: [] for c in COLOR_ORDER}
    for (row, col), piece in replay.board.items():
        by_color.setdefault(piece.color, []).append((row, col, piece.piece))
    for color in COLOR_ORDER:
        items = sorted(by_color.get(color, []), key=lambda x: (x[0], x[1]))
        if items:
            body = " ".join(f"{r},{c}:{PIECE_NAMES.get(p, '?')}" for r, c, p in items)
            lines.append(f"{color} {body}")
    lines.append("[行棋]")
    for step in analysis.steps:
        if step.kind == "event":
            lines.append(f"{step.no}. {step.event.color or ''} {step.event_text}")
            continue
        move = step.move
        tag = ""
        if move.result in RESULT_TAG:
            tag = RESULT_TAG[move.result]
        if step.resolved and step.resolved.type == "flag":
            tag = "扛旗"
        suffix = f" {tag}" if tag else ""
        lines.append(
            f"{step.no}. {move.color} {move.start[0]},{move.start[1]}"
            f"-{move.end[0]},{move.end[1]}{suffix}"
        )
    return "\n".join(lines) + "\n"


def summary_text(replay: QQReplay) -> str:
    analysis = replay.analyze()
    seats = "  ".join(
        f"{b.color}={b.seat}" for b in replay.blocks
    )
    directions = "  ".join(
        f"{b.color}={DIR_CN.get(b.orient, b.orient)}" for b in replay.blocks
    )
    return "\n".join([
        f"文件: {replay.path}",
        f"大小: {replay.file_size} 字节",
        f"头部步数: {replay.header_steps}  实际记录: {len(replay.seq)}"
        f"（棋步 {replay.move_count} / 事件 {replay.event_count}）",
        f"己方: {replay.my_color or '未知'}"
        + (f"（{replay.players.get(replay.my_color, '')}）" if replay.my_color else ""),
        f"方位: {directions}",
        f"座位: {seats}",
        f"行棋顺序: {' → '.join(replay.turn_order)}",
        f"方位自洽率: {replay.self_check_rate * 100:.1f}%",
        f"结果: {analysis.result_text}",
        f"数据自洽: {analysis.error_count} 步异常",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析 QQ 四国军棋 .jgs 完整对局")
    parser.add_argument("file", help=".jgs 复盘文件")
    parser.add_argument("--json", metavar="FILE", help="导出完整解析 JSON")
    parser.add_argument("--text", metavar="FILE", help="导出复盘 v2.0 文本")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        replay = parse_jgs(args.file)
    except (OSError, ValueError) as exc:
        print(f"解析失败: {exc}", file=sys.stderr)
        return 1

    print(summary_text(replay))
    analysis = replay.analyze()
    print("\n子力统计:")
    for color in COLOR_ORDER:
        st = analysis.stats.get(color)
        if not st:
            continue
        print(
            f"  {color}({replay.players.get(color, '')}): "
            f"步数={st.steps} 得分={st.score} 损失={st.loss} 净值={st.net} "
            f"吃子={st.eat} 被吃={st.killed} 同尽={st.both} 扛旗={st.flag}"
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(replay.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已导出: {args.json}")
    if args.text:
        with open(args.text, "w", encoding="utf-8") as f:
            f.write(replay.to_text())
        print(f"文本已导出: {args.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
