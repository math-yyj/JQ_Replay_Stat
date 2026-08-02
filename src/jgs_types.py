# 对应 C++ 工程 src/type.h / src/type.cpp
# 基础类型定义：棋子类型、颜色、位置、事件类型及其字符串转换与分值表

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

ItemTypeStringShort = False


class ItemType(IntEnum):
    JQNone = 0x00
    Junqi = 0x02
    Dilei = 0x03
    Zhadan = 0x04
    Silin = 0x05
    Junzhang = 0x06
    Shizhang = 0x07
    Lvzhang = 0x08
    Tuanzhang = 0x09
    Yinzhang = 0x0A
    Lianzhang = 0x0B
    Paizhang = 0x0C
    Gongbing = 0x0D


class ItemColor(IntEnum):
    YELLOW = 0
    BLUE = 1
    GREEN = 2
    PURPLE = 3
    UNKNOWN_COLOR = 0xFF


# PlayerColor 与 ItemColor 等价（对应 C++ typedef）
PlayerColor = ItemColor

if ItemTypeStringShort:
    _ITEMTYPE_MAP = {
        ItemType.JQNone: "○○",
        ItemType.Junqi: "旗",
        ItemType.Dilei: "雷",
        ItemType.Zhadan: "炸",
        ItemType.Silin: "司",
        ItemType.Junzhang: "军",
        ItemType.Shizhang: "师",
        ItemType.Lvzhang: "旅",
        ItemType.Tuanzhang: "团",
        ItemType.Yinzhang: "营",
        ItemType.Lianzhang: "连",
        ItemType.Paizhang: "排",
        ItemType.Gongbing: "兵",
    }
else:
    _ITEMTYPE_MAP = {
        ItemType.JQNone: " ○○ ",
        ItemType.Junqi: "军旗",
        ItemType.Dilei: "地雷",
        ItemType.Zhadan: "炸弹",
        ItemType.Silin: "司令",
        ItemType.Junzhang: "军长",
        ItemType.Shizhang: "师长",
        ItemType.Lvzhang: "旅长",
        ItemType.Tuanzhang: "团长",
        ItemType.Yinzhang: "营长",
        ItemType.Lianzhang: "连长",
        ItemType.Paizhang: "排长",
        ItemType.Gongbing: "工兵",
    }

_ITEMCOLOR_MAP = {
    ItemColor.YELLOW: "黄色",
    ItemColor.BLUE: "蓝色",
    ItemColor.GREEN: "绿色",
    ItemColor.PURPLE: "紫色",
    ItemColor.UNKNOWN_COLOR: "无色",  # ^ ^
}

_ITEMSCORE_MAP = {
    ItemType.JQNone: 0,
    ItemType.Junqi: 0,
    ItemType.Dilei: 15,
    ItemType.Zhadan: 100,
    ItemType.Silin: 220,
    ItemType.Junzhang: 180,
    ItemType.Shizhang: 120,
    ItemType.Lvzhang: 80,
    ItemType.Tuanzhang: 40,
    ItemType.Yinzhang: 20,
    ItemType.Lianzhang: 10,
    ItemType.Paizhang: 5,
    ItemType.Gongbing: 30,
}


def item_type2string(item_type: ItemType | int) -> str:
    return _ITEMTYPE_MAP[ItemType(item_type)]


def itemcolor_type2string(item_color: ItemColor | int) -> str:
    return _ITEMCOLOR_MAP[ItemColor(item_color)]


def item_score(item_type: ItemType | int) -> int:
    return _ITEMSCORE_MAP[ItemType(item_type)]


def set_item_score(item_type: ItemType | int, score: int) -> None:
    # update settings
    key = ItemType(item_type)
    if key not in _ITEMSCORE_MAP:
        raise KeyError(key)
    _ITEMSCORE_MAP[key] = score


MAX_AXIS = 0x10


@dataclass(frozen=True)
class Position:
    """GlobalAxis Position，row/col 均为 uint8。

    坐标系定义（对应 C++ 注释）:
                 Green                   0x00
                                         0x01
                                           :
     Blue                    Purple        :
                                           :
                 Yellow                  0x10
     0x10 ...  ...           ... 0x01 0x00
    """

    row: int
    col: int

    max_axis = MAX_AXIS

    def is_none(self) -> bool:
        return self == NONE_POSITION

    # rotate `times` * 90 degrees, return rotated position value
    def rotate_counter_clockwise(self, times: int) -> "Position":
        times = times % 4
        assert times >= 0
        if times == 0:
            return self
        elif times == 1:
            return Position(self.col, (MAX_AXIS - self.row) & 0xFF)
        elif times == 2:
            return Position((MAX_AXIS - self.row) & 0xFF, (MAX_AXIS - self.col) & 0xFF)
        else:  # times == 3
            return Position((MAX_AXIS - self.col) & 0xFF, self.row)

    # convert item's pos in JGSLayoutBlock to default (YELLOW) GlobalAxis Position
    @staticmethod
    def from_init_layout_pos(row: int, col: int) -> "Position":
        return Position((row + 0x0B) & 0xFF, (0x0A - col) & 0xFF)

    def __str__(self) -> str:
        return f"(r:{self.row:2x}, c:{self.col:2x})"


NONE_POSITION = Position(0, 0)


class EventType(IntEnum):
    UnknownEventType = 0
    MoveEventType = 0x5F
    InfoEventType = 0xF5


class MoveResultType(IntEnum):
    ResultMove = 0     # 移动
    ResultEat = 1      # 吃子
    ResultRebound = 2  # 反弹
    ResultSwap = 3     # 交换，打对


class InfoResultType(IntEnum):
    InfoTimeout = 2
    InfoPlayerDead = 3
    InfoPlayerQuit = 4
    InfoGameOver = 5


class InfoGameOverType(IntEnum):
    GameOverTypeDraw = 0x02
    GameOverTypeCommon = 0x04
    GameOverTypePlayerForceQuit = 0x06
    GameOverTypePlayerRequestQuit = 0x07


def event_type2string(event_type: EventType | int) -> str:
    if event_type == EventType.MoveEventType:
        return "动作命令"
    elif event_type == EventType.InfoEventType:
        return "通知命令"
    else:
        return "未知命令"


_MOVERESULT_ARR = ["移动", "吃子", "反弹", "交换"]


def event_moveresult2string(move_result: MoveResultType | int) -> str:
    return _MOVERESULT_ARR[int(move_result)]


class GameException(Exception):
    def __init__(self, err_str: str):
        super().__init__(err_str)
        self.err_str = err_str
