# 对应 C++ 工程 src/block.h / src/block.cpp
# JGS 文件二进制块解析：文件头、玩家信息、初始布局、事件块

from __future__ import annotations

import struct

from .jgs_types import (
    EventType,
    InfoGameOverType,
    InfoResultType,
    ItemColor,
    ItemType,
    MoveResultType,
    Position,
    event_moveresult2string,
    event_type2string,
    item_type2string,
    itemcolor_type2string,
)

HEADER_BLOCK_SIZE = 0x20        # JGSHeaderBlock
PLAYER_INFO_BLOCK_SIZE = 88     # JGSPLayerInfoBlock
TOTAL_INFO_BLOCK_SIZE = 0x19C   # JGSTotalInfoBlock
EVENT_BLOCK_SIZE = 10           # JGSEventBlock


def _decode_cstr(raw: bytes) -> str:
    """C 风格字符串: 取 '\\0' 前内容，玩家名为 GBK 编码"""
    raw = raw.split(b"\x00", 1)[0]
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


class JGSHeaderBlock:
    """0x00 ~ 0x1F"""

    def __init__(self, data: bytes):
        assert len(data) == HEADER_BLOCK_SIZE
        self.raw = data
        # 0x00 ~ 0x0A: discription; 0x0F: self_color; 0x1A ~ 0x1B: all_steps (little endian)
        self.discription_raw: bytes = data[0x00:0x0B]
        self.discription: str = _decode_cstr(self.discription_raw)
        self.self_color = ItemColor(data[0x0F])
        (self.all_steps,) = struct.unpack_from("<H", data, 0x1A)

    def __str__(self) -> str:
        return (
            f"{self.discription}\n"
            f"self  color:{itemcolor_type2string(self.self_color)}\n"
            f"total steps:{self.all_steps}"
        )


class JGSLayoutBlock:
    """layout of player, 30 bytes, 5 * 6 (6 lines, 5 item each line)"""

    row_num = 6
    col_num = 5

    def __init__(self, data: bytes):
        assert len(data) == self.row_num * self.col_num
        self._items = [
            [ItemType(data[row * self.col_num + col]) for col in range(self.col_num)]
            for row in range(self.row_num)
        ]

    def item(self, row: int, col: int) -> ItemType:
        assert 0 <= row < self.row_num
        assert 0 <= col < self.col_num
        return self._items[row][col]

    def __str__(self) -> str:
        lines = []
        for row in range(self.row_num):
            lines.append(
                " ".join(item_type2string(self.item(row, col)) for col in range(self.col_num)) + " "
            )
        return "\n".join(lines) + "\n"


class JGSPlayerInfoBlock:
    """0x020 ~ 0x17F, 4 * 88 bytes, each 88 bytes"""

    def __init__(self, data: bytes):
        assert len(data) == PLAYER_INFO_BLOCK_SIZE
        self.raw = data
        # 0x00: player_color; 0x08 ~ 0x1B: player_name (20 bytes);
        # 0x1C ~ 0x39: layout 30 bytes; 0x3C ~ 0x3F: qq number (little endian)
        self.player_color = ItemColor(data[0x00])
        self.player_name_raw: bytes = data[0x08:0x1C]
        self.player_name: str = _decode_cstr(self.player_name_raw)
        self.player_layout = JGSLayoutBlock(data[0x1C:0x3A])
        (self.player_qqnumber,) = struct.unpack_from("<I", data, 0x3C)

    def __str__(self) -> str:
        return (
            f"color :{itemcolor_type2string(self.player_color)}\n"
            f"name  :{self.player_name}\n"
            f"qq_num:{self.player_qqnumber}\n"
            f"layout:\n{self.player_layout}"
        )


class JGSEventBlock:
    """10 bytes 事件块"""

    def __init__(self, data: bytes):
        assert len(data) == EVENT_BLOCK_SIZE
        self.byte = bytes(data)

    def event_type(self) -> EventType:
        try:
            return EventType(self.byte[0])
        except ValueError:
            return EventType.UnknownEventType

    def _assert_move(self) -> None:
        assert self.event_type() == EventType.MoveEventType

    def _assert_info(self) -> None:
        assert self.event_type() == EventType.InfoEventType

    # move event
    def move_has_40_dead(self) -> bool:
        self._assert_move()
        return bool(self.byte[1] & 0b01000000)

    def move_result(self) -> MoveResultType:
        self._assert_move()
        return MoveResultType(self.byte[1] & 0b11)

    def move_start_pos(self) -> Position:
        self._assert_move()
        return Position(self.byte[2], self.byte[3])

    def move_end_pos(self) -> Position:
        self._assert_move()
        return Position(self.byte[4], self.byte[5])

    def move_attacker_banner_pos(self) -> Position:
        # 进攻方军旗位置，都为0则表示不显示
        self._assert_move()
        return Position(self.byte[6], self.byte[7])

    def move_show_attacker_banner(self) -> bool:
        self._assert_move()
        return self.byte[6] != 0 or self.byte[7] != 0

    def move_defender_banner_pos(self) -> Position:
        # 被进攻方
        self._assert_move()
        return Position(self.byte[8], self.byte[9])

    def move_show_defender_banner(self) -> bool:
        self._assert_move()
        return self.byte[8] != 0 or self.byte[9] != 0

    # info event
    def info_result(self) -> InfoResultType:
        self._assert_info()
        return InfoResultType(self.byte[1])

    def info_dead_player_color(self) -> ItemColor:
        self._assert_info()
        return ItemColor(self.byte[2])

    def info_quit_player_color(self) -> ItemColor:
        self._assert_info()
        return ItemColor(self.byte[2])

    def info_timeout_player_color(self) -> ItemColor:
        self._assert_info()
        return ItemColor(self.byte[2])

    def info_game_over_type(self) -> InfoGameOverType:
        self._assert_info()
        return InfoGameOverType(self.byte[3])

    def __str__(self) -> str:
        s = f"JGS_EVENT: {event_type2string(self.event_type())}, "
        if self.event_type() == EventType.MoveEventType:
            s += ("有" if self.move_has_40_dead() else "无") + "40死亡, "
            s += f"结果:{event_moveresult2string(self.move_result())}, "
            s += f"起始位置:{self.move_start_pos()}, "
            s += f"到达位置:{self.move_end_pos()}, "
        return s


class JGSTotalInfoBlock:
    """jgs file info block, exclude event blocks (0x000 ~ 0x19B)"""

    player_number = 4

    def __init__(self, data: bytes):
        assert len(data) == TOTAL_INFO_BLOCK_SIZE
        self.header_block = JGSHeaderBlock(data[0x00:HEADER_BLOCK_SIZE])
        self.player_info_block = [
            JGSPlayerInfoBlock(
                data[
                    HEADER_BLOCK_SIZE + i * PLAYER_INFO_BLOCK_SIZE:
                    HEADER_BLOCK_SIZE + (i + 1) * PLAYER_INFO_BLOCK_SIZE
                ]
            )
            for i in range(self.player_number)
        ]

    def __str__(self) -> str:
        s = f">>JGS_TOTAL_INFO:\n\n[JGS_HEADER]\n{self.header_block}\n\n[JGS_PLAYERINFO]\n"
        for i, pib in enumerate(self.player_info_block):
            s += f">Player {i}:\n{pib}\n"
        return s
