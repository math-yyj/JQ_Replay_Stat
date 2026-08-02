# 对应 C++ 工程 src/player.h / src/player.cpp
# 玩家：初始布局展开为全局坐标棋子表，维护棋盘状态与荣誉称号统计

from __future__ import annotations

from .block import JGSLayoutBlock, JGSPlayerInfoBlock
from .item import Item
from .jgs_types import (
    GameException,
    ItemType,
    PlayerColor,
    Position,
    item_score,
    item_type2string,
    itemcolor_type2string,
)


class Player:
    def __init__(self, player_info: JGSPlayerInfoBlock):
        self._name = player_info.player_name
        self._qq = player_info.player_qqnumber
        self._color = player_info.player_color
        self._color_str = itemcolor_type2string(self._color)
        self._init_layout = player_info.player_layout

        self._pos_item_map: dict[Position, Item] = {}

        # 记录player的"荣誉称号"数据
        # "（1）"神兵天降"：使用工兵消灭敌方单位价值最高。
        # 注1：工兵消灭的单位中，炸弹与军旗价值3，地雷价值2，工兵价值1。"
        self._gongbing_destroyed_values = 0  # 己方工兵消灭单位的价值总和
        # （2）"拆弹专家"：消灭炸弹数量最多
        self._destroyed_zhadan_count = 0  # 己方消灭的他方炸弹数目总和
        # "（3）"运筹帷幄"：除炸弹与军旗外，司令消灭敌方单位价值最高
        # 注2：司令消灭的单位中，司令与军长价值5，师长价值4，旅长价值3，团长价值2，其余子力价值1。"
        self._silin_destroyed_values = 0  # 己方40消灭的他方单位价值总和
        # （4）"七大天王"/"八大天王"：单次战斗中斩获司令、军长、师长共计七枚/八枚
        self._destroyed_403938_count = 0  # 己方消灭他方40,39,38的数目总和

        for init_row in range(JGSLayoutBlock.row_num):
            for init_col in range(JGSLayoutBlock.col_num):
                item_type = self._init_layout.item(init_row, init_col)
                if item_type == ItemType.JQNone:
                    continue
                pos = Position.from_init_layout_pos(init_row, init_col)
                if self._color == PlayerColor.PURPLE:
                    pos = pos.rotate_counter_clockwise(1)
                elif self._color == PlayerColor.GREEN:
                    pos = pos.rotate_counter_clockwise(2)
                elif self._color == PlayerColor.BLUE:
                    pos = pos.rotate_counter_clockwise(3)
                # YELLOW 不旋转
                self._pos_item_map[pos] = Item(item_type, self._color, pos)

    def color(self) -> PlayerColor:
        return self._color

    def remove_item(self, pos: Position) -> None:
        if self._pos_item_map.pop(pos, None) is None:
            raise GameException("Player::remove_item failed")

    def clear(self) -> None:
        self._pos_item_map.clear()

    def move_item(self, start_pos: Position, end_pos: Position) -> None:
        item = self._pos_item_map.pop(start_pos)
        item.move_to(end_pos)
        self._pos_item_map[end_pos] = item

    def get_item(self, pos: Position) -> Item:
        return self._pos_item_map[pos]

    def has_item(self, pos: Position) -> bool:
        return pos in self._pos_item_map

    def get_current_score(self) -> int:
        return sum(item_score(item.get_type()) for item in self._pos_item_map.values())

    def qq(self) -> int:
        return self._qq

    def name(self) -> str:
        return self._name

    # 用于统计荣誉称号数据
    # 告知player自己击杀他人的信息
    def notify_kill_event(self, killer: Item, dead: Item) -> None:
        killertype = killer.get_type()
        deadtype = dead.get_type()
        # 神兵天降 (只在己方击杀他方(notify_kill_event)中记录,
        # notify_dead_event中他方用炸弹,工兵击杀我方工兵不记录)
        if killertype == ItemType.Gongbing:
            if deadtype in (ItemType.Zhadan, ItemType.Junqi):
                self._gongbing_destroyed_values += 3
            elif deadtype == ItemType.Dilei:
                self._gongbing_destroyed_values += 2
            elif deadtype == ItemType.Gongbing:
                self._gongbing_destroyed_values += 1
            else:
                raise GameException(
                    "notify_kill_event, gongbing kill invalid: " + item_type2string(deadtype)
                )

        # 拆弹专家
        if deadtype == ItemType.Zhadan:
            self._destroyed_zhadan_count += 1

        # 运筹帷幄
        if killertype == ItemType.Silin:
            if deadtype in (ItemType.Silin, ItemType.Junzhang):
                self._silin_destroyed_values += 5
            elif deadtype == ItemType.Shizhang:
                self._silin_destroyed_values += 4
            elif deadtype == ItemType.Lvzhang:
                self._silin_destroyed_values += 3
            elif deadtype == ItemType.Tuanzhang:
                self._silin_destroyed_values += 2
            else:
                self._silin_destroyed_values += 1

        # 七、八大天王
        if deadtype in (ItemType.Silin, ItemType.Junzhang, ItemType.Shizhang):
            self._destroyed_403938_count += 1

    # 告知player自己被他人击杀而死亡的信息
    def notify_dead_event(self, killer: Item, dead: Item) -> None:
        pass

    def get_stat(self) -> list[int]:
        return [
            self._gongbing_destroyed_values,
            self._destroyed_zhadan_count,
            self._silin_destroyed_values,
            self._destroyed_403938_count,
        ]
