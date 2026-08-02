# 对应 C++ 工程 src/game.h / src/game.cpp
# 游戏主类：读取 .jgs 文件，构建四位玩家并逐事件推演棋局

from __future__ import annotations

import os

from .block import (
    EVENT_BLOCK_SIZE,
    TOTAL_INFO_BLOCK_SIZE,
    JGSEventBlock,
    JGSTotalInfoBlock,
)
from .jgs_types import (
    EventType,
    GameException,
    InfoResultType,
    MoveResultType,
    PlayerColor,
    Position,
    item_type2string,
)
from .player import Player


class Game:
    def __init__(self, jgs_file_name: str | os.PathLike):
        self._jgs_file_name = str(jgs_file_name)
        self._event_list: list[JGSEventBlock] = []
        self._color_player_map: dict[PlayerColor, Player] = {}
        self._total_steps = 0
        self._self_color = PlayerColor.UNKNOWN_COLOR
        self._init()

    def _init(self) -> None:
        try:
            with open(self._jgs_file_name, "rb") as f:
                data = f.read()
        except OSError:
            raise GameException("open file error")

        if len(data) < TOTAL_INFO_BLOCK_SIZE:
            raise GameException("read file error 1")

        total_info = JGSTotalInfoBlock(data[:TOTAL_INFO_BLOCK_SIZE])

        if total_info.header_block.discription_raw[:10] != b"QQGame JQS":
            raise GameException("file discription not matched")

        self._self_color = total_info.header_block.self_color
        self._total_steps = total_info.header_block.all_steps

        for i in range(JGSTotalInfoBlock.player_number):
            player = Player(total_info.player_info_block[i])
            self._color_player_map[player.color()] = player

        # read events
        offset = TOTAL_INFO_BLOCK_SIZE
        for i in range(self._total_steps):
            ev_data = data[offset:offset + EVENT_BLOCK_SIZE]
            if len(ev_data) != EVENT_BLOCK_SIZE:
                raise GameException("read events failed on: " + str(i))
            self._event_list.append(JGSEventBlock(ev_data))
            offset += EVENT_BLOCK_SIZE

    # at present, do not support go back steps
    # so process all events
    def process_all_events(self) -> None:
        for ev in self._event_list:
            self._process_event(ev)

    # get player
    def get_player(self, color: PlayerColor) -> Player:
        return self._color_player_map[color]

    def _process_event(self, ev: JGSEventBlock) -> None:
        event_type = ev.event_type()

        if event_type == EventType.InfoEventType:
            # info event
            if ev.info_result() == InfoResultType.InfoPlayerDead:
                self._color_player_map[ev.info_dead_player_color()].clear()
            elif ev.info_result() == InfoResultType.InfoGameOver:
                # game over
                return
            return  # next event

        if event_type != EventType.MoveEventType:
            return

        cur_move_color = self._which_player(ev.move_start_pos())
        cur_player = self._color_player_map[cur_move_color]
        cur_item = cur_player.get_item(ev.move_start_pos())

        move_result = ev.move_result()

        if move_result == MoveResultType.ResultMove:
            cur_player.move_item(ev.move_start_pos(), ev.move_end_pos())

        elif move_result == MoveResultType.ResultRebound:
            # 获取击杀方, 击杀方子
            target_color = self._which_player(ev.move_end_pos())
            if target_color == PlayerColor.UNKNOWN_COLOR:
                raise GameException("Unknown Target Pos: ResultRebound")

            target_player = self._color_player_map[target_color]
            target_item = target_player.get_item(ev.move_end_pos())

            # 告知
            target_player.notify_kill_event(target_item, cur_item)
            cur_player.notify_dead_event(target_item, cur_item)

            # 更新棋盘
            cur_player.remove_item(ev.move_start_pos())

        elif move_result == MoveResultType.ResultSwap:
            # 获取目标方, 目标方子
            target_color = self._which_player(ev.move_end_pos())
            if target_color == PlayerColor.UNKNOWN_COLOR:
                raise GameException("Unknown Target Pos: ResultRebound")

            target_player = self._color_player_map[target_color]
            target_item = target_player.get_item(ev.move_end_pos())

            # 告知 (打对这一操作，对于双方都是既击杀又死亡，可以这么来理解吧......)
            target_player.notify_kill_event(target_item, cur_item)
            cur_player.notify_dead_event(target_item, cur_item)  # 对于被击杀, 是对方击杀己方

            target_player.notify_dead_event(cur_item, target_item)
            cur_player.notify_kill_event(cur_item, target_item)  # 对于击杀, 是己方击杀对方

            # 更新棋盘
            cur_player.remove_item(ev.move_start_pos())
            target_player.remove_item(ev.move_end_pos())

        elif move_result == MoveResultType.ResultEat:
            # 获取目标方, 目标方子
            target_color = self._which_player(ev.move_end_pos())
            if target_color == PlayerColor.UNKNOWN_COLOR:
                raise GameException("Unknown Target Pos: ResultRebound")

            target_player = self._color_player_map[target_color]
            target_item = target_player.get_item(ev.move_end_pos())

            # 告知
            target_player.notify_dead_event(cur_item, target_item)
            cur_player.notify_kill_event(cur_item, target_item)

            # 更新棋盘
            target_player.remove_item(ev.move_end_pos())
            cur_player.move_item(ev.move_start_pos(), ev.move_end_pos())

        else:
            raise GameException("meet an unknown event")

    # ignore color: color will not search in this function
    def _which_player(
        self, pos: Position, ignore_color: PlayerColor = PlayerColor.UNKNOWN_COLOR
    ) -> PlayerColor:
        for color, player in self._color_player_map.items():
            if color == ignore_color:
                continue
            if player.has_item(pos):
                return color

        return PlayerColor.UNKNOWN_COLOR

    def __str__(self) -> str:
        # 与 C++ operator<< 输出保持一致
        lines = []
        for row in range(Position.max_axis + 1):
            line = ""
            for col in range(Position.max_axis + 1, -1, -1):
                color = self._which_player(Position(row, col))
                if color == PlayerColor.UNKNOWN_COLOR:
                    line += "    "
                else:
                    item = self._color_player_map[color].get_item(Position(row, col))
                    line += f"{int(color)}{item_type2string(item.get_type())} "
            lines.append(line)
        return "\n".join(lines) + "\n"
