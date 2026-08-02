# 对应 C++ 工程 src/item.h / src/item.cpp
# 棋子对象

from __future__ import annotations

from .jgs_types import ItemColor, ItemType, Position, item_type2string, itemcolor_type2string


class Item:
    def __init__(self, type_: ItemType, color: ItemColor, pos: Position):
        self._type = type_
        self._color = color
        self._pos = pos

    def get_type(self) -> ItemType:
        return self._type

    def set_type(self, type_: ItemType) -> None:
        self._type = type_

    def get_color(self) -> ItemColor:
        return self._color

    def set_color(self, color: ItemColor) -> None:
        self._color = color

    def get_pos(self) -> Position:
        return self._pos

    def set_pos(self, pos: Position) -> None:
        self._pos = pos

    # the same as set_pos
    def move_to(self, to_pos: Position) -> None:
        self.set_pos(to_pos)

    def __str__(self) -> str:
        return f"{item_type2string(self._type)} {itemcolor_type2string(self._color)} {self._pos}"
