"""QQ 四国军棋 17×17 棋盘几何与批量统计辅助。"""

from __future__ import annotations

from .jgs_types import ItemType
from .qq_replay import BoardPiece, FLAG_H_DIR, Move, PIECE_NAMES, resolve_move

CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}
FLAG_POSITIONS = {(5, 1), (5, 3)}

BOARD_LAYOUT = [
    "......PHPHP.......",
    "......PPPPP.......",
    "......PCPCP.......",
    "......PPCPP.......",
    "......PCPCP.......",
    "......PPPPP.......",
    "PPPPPPHRHRHPPPPPP",
    "HPCPCPRRRRRPCPCPH",
    "PPPCPPHRHRHPPCPPP",
    "HPCPCPRRRRRPCPCPH",
    "PPPPPPHRHRHPPPPPP",
    "......PPPPP.......",
    "......PCPCP.......",
    "......PPCPP.......",
    "......PCPCP.......",
    "......PPPPP.......",
    "......PHPHP.......",
]

RAIL_LINES = [
    ("h", 1, 6, 10), ("h", 5, 6, 10), ("h", 6, 1, 15),
    ("h", 8, 5, 11), ("h", 10, 1, 15), ("h", 11, 6, 10),
    ("h", 15, 6, 10),
    ("v", 1, 6, 10), ("v", 5, 6, 10), ("v", 6, 1, 15),
    ("v", 8, 5, 11), ("v", 10, 1, 15), ("v", 11, 6, 10),
    ("v", 15, 6, 10),
]

SEAT_COLORS = ("黄", "紫", "绿", "蓝")
CANONICAL_ORIENT = {"黄": "up", "紫": "left", "绿": "down", "蓝": "right"}
CANONICAL_SEAT = {"黄": "上家", "紫": "左家", "绿": "下家", "蓝": "右家"}
DIR_INDEX = {"up": 0, "right": 1, "down": 2, "left": 3}

REGION_FILL = {
    "up": "#FFF6D8",
    "down": "#E6F4E3",
    "left": "#F1E6F5",
    "right": "#E3F0FA",
}
COLOR_FILL = {
    "黄": "#F4C542",
    "紫": "#9C6BC0",
    "绿": "#5FA85F",
    "蓝": "#4F86C6",
}
COLOR_LINE = {
    "黄": "#A67C00",
    "紫": "#5E3A82",
    "绿": "#2F6B2F",
    "蓝": "#2B5585",
}
COLOR_TEXT = {
    "黄": "#8A6500",
    "紫": "#5E3A82",
    "绿": "#2F6B2F",
    "蓝": "#2B5585",
}
COLOR_TEXT_ON = {
    "黄": "#3A2A00",
    "紫": "#FFFFFF",
    "绿": "#FFFFFF",
    "蓝": "#FFFFFF",
}

PIECE_ORDER = (
    ItemType.Silin,
    ItemType.Junzhang,
    ItemType.Shizhang,
    ItemType.Lvzhang,
    ItemType.Tuanzhang,
    ItemType.Yinzhang,
    ItemType.Lianzhang,
    ItemType.Paizhang,
    ItemType.Gongbing,
    ItemType.Zhadan,
    ItemType.Dilei,
    ItemType.Junqi,
)


def cell_kind(row: int, col: int) -> str:
    return BOARD_LAYOUT[row][col]


def can_hold_piece(row: int, col: int) -> bool:
    """可承载棋子的格子：普通点、行营、九宫/大本营；排除空角与纯铁路通过点。"""
    return cell_kind(row, col) in ("P", "H", "C")


def region_of(row: int, col: int) -> str | None:
    if 0 <= row <= 5 and 6 <= col <= 10:
        return "up"
    if 11 <= row <= 16 and 6 <= col <= 10:
        return "down"
    if 6 <= row <= 10 and 0 <= col <= 5:
        return "left"
    if 6 <= row <= 10 and 11 <= col <= 16:
        return "right"
    return None


def self_coord(direction: str, row: int, col: int) -> tuple[int, int] | None:
    if direction == "up":
        i, j = 5 - row, 10 - col
    elif direction == "down":
        i, j = row - 11, col - 6
    elif direction == "left":
        i, j = 5 - col, row - 6
    else:
        i, j = col - 11, 10 - row
    if 0 <= i < 6 and 0 <= j < 5:
        return i, j
    return None


def layout_is_valid(matrix: list[int]) -> bool:
    """校验 6×5 自视角布阵: 25 子、行营空、非行营有子、恰好一旗且旗在 H。"""
    pieces = 0
    flag_cell = None
    for index, value in enumerate(matrix):
        row, col = divmod(index, 5)
        if (row, col) in CAMP_POSITIONS:
            if value != 0:
                return False
            continue
        if value == 0 or value not in PIECE_NAMES:
            return False
        if value == int(ItemType.Zhadan) and row == 0:
            return False
        if value == int(ItemType.Dilei) and row not in (4, 5):
            return False
        pieces += 1
        if value == int(ItemType.Junqi):
            if flag_cell is not None:
                return False
            flag_cell = (row, col)
    return pieces == 25 and flag_cell in FLAG_POSITIONS


def legal_cells(color: str, piece: int) -> set[tuple[int, int]]:
    """某颜色某棋子的条件可选位置。

    军旗/地雷不可移动：军旗仍限己方大本营，地雷仍限己方后两排；
    其余棋子可出现在全盘任意可承载棋子的格子（含行营、九宫、大本营）。
    """
    direction = CANONICAL_ORIENT[color]
    if piece == int(ItemType.Junqi):
        return {
            (row, col) for row, col in FLAG_H_DIR[direction]
            if can_hold_piece(row, col)
        }
    if piece == int(ItemType.Dilei):
        cells: set[tuple[int, int]] = set()
        for row in range(17):
            for col in range(17):
                if not can_hold_piece(row, col):
                    continue
                if region_of(row, col) != direction:
                    continue
                coord = self_coord(direction, row, col)
                if coord is None:
                    continue
                if coord[0] in (4, 5):
                    cells.add((row, col))
        return cells
    return {
        (row, col)
        for row in range(17)
        for col in range(17)
        if can_hold_piece(row, col)
    }


def rotate_cw(row: int, col: int, times: int) -> tuple[int, int]:
    for _ in range(times % 4):
        row, col = col, 16 - row
    return row, col


def rotation_times(orient: dict[str, str]) -> int | None:
    """返回把该方位旋转到“黄上/紫左/绿下/蓝右”的顺时针次数。"""
    if not all(color in orient for color in SEAT_COLORS):
        return None
    yellow_dir = DIR_INDEX.get(orient["黄"])
    if yellow_dir is None:
        return None
    times = (-yellow_dir) % 4
    for color in SEAT_COLORS:
        current = DIR_INDEX.get(orient.get(color, ""))
        if current is None:
            return None
        if (current + times) % 4 != DIR_INDEX[CANONICAL_ORIENT[color]]:
            return None
    return times


def normalize_board(board: dict[tuple[int, int], BoardPiece],
                    orient: dict[str, str]) -> dict[tuple[int, int], BoardPiece] | None:
    """把复盘棋盘旋转到“黄上/紫左/绿下/蓝右”的统一坐标系。

    颜色方位无法通过整体旋转对齐时返回 None（该文件不参与批量统计）。
    """
    times = rotation_times(orient)
    if times is None:
        return None
    normalized: dict[tuple[int, int], BoardPiece] = {}
    for (row, col), piece in board.items():
        nr, nc = rotate_cw(row, col, times)
        normalized[(nr, nc)] = piece
    return normalized


def clear_color(board: dict[tuple[int, int], BoardPiece], color: str) -> None:
    for key in [k for k, piece in board.items() if piece.color == color]:
        del board[key]


def iter_states(replay, board: dict[tuple[int, int], BoardPiece],
                times: int):
    """按指令顺序推演，依次产出 (步号, 棋盘)。

    首个产出是初始布阵（步号 0）；之后每处理一步棋/事件产出一次。
    产出的棋盘对象会被后续步骤原地修改，消费方应立即读取，不要保存引用。
    推演包含战败清子、连续两次未行棋判负、无子可动判负与扛旗清子。
    """
    turn_order = list(getattr(replay, "turn_order", ()) or ())
    turn_idx = len(turn_order) - 1
    skip_count = {color: 0 for color in SEAT_COLORS}
    alive = {color: True for color in SEAT_COLORS}

    def movable_left(color: str) -> bool:
        return any(
            piece.color == color
            and piece.piece not in (int(ItemType.Dilei), int(ItemType.Junqi))
            for piece in board.values()
        )

    def defeat(color: str | None) -> None:
        if color and alive.get(color):
            alive[color] = False
            clear_color(board, color)

    yield 0, board
    for kind, item in replay.seq:
        if kind == "event":
            is_defeat = item.code in (3, 4) or (
                item.code == 2 and item.b >= 5)
            if is_defeat:
                defeat(item.color)
            if item.code == 2 and item.color in turn_order:
                turn_idx = turn_order.index(item.color)
                skip_count[item.color] = 0
        else:
            if alive.get(item.color) and not movable_left(item.color):
                defeat(item.color)
            if alive.get(item.color) and item.color in turn_order:
                target_idx = turn_order.index(item.color)
                count = len(turn_order)
                for offset in range(1, count + 1):
                    index = (turn_idx + offset) % count
                    if index == target_idx:
                        break
                    color = turn_order[index]
                    if not alive.get(color):
                        continue
                    skip_count[color] = skip_count.get(color, 0) + 1
                    if skip_count[color] >= 2:
                        defeat(color)
                skip_count[item.color] = 0
                turn_idx = target_idx

            start = rotate_cw(item.start[0], item.start[1], times)
            end = rotate_cw(item.end[0], item.end[1], times)
            move = Move(
                no=item.no,
                color=item.color,
                start=start,
                end=end,
                result=item.result,
                tag=item.tag,
            )
            resolved = resolve_move(move, board)
            if resolved.type == "flag" and resolved.defender:
                alive[resolved.defender] = False
        yield item.no, board
