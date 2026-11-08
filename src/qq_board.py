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


def _enterable(board: dict[tuple[int, int], BoardPiece], color: str,
               cell: tuple[int, int]) -> bool:
    """该格可否作为落点: 可持子, 且非"被占行营", 且非己子。"""
    if not (0 <= cell[0] < 17 and 0 <= cell[1] < 17) or not can_hold_piece(*cell):
        return False
    occ = board.get(cell)
    if occ is None:
        return True
    if occ.color == color:
        return False
    return cell_kind(*cell) != "C"      # 行营内棋子受保护, 不可被攻击


def _line_cells(line, cell):
    kind, fixed, a, b = line
    if kind == "h":
        if cell[0] != fixed or not a <= cell[1] <= b:
            return None
        return [(fixed, c) for c in range(a, b + 1)]
    if cell[1] != fixed or not a <= cell[0] <= b:
        return None
    return [(r, fixed) for r in range(a, b + 1)]


def _rail_scan(cells, start, board, color) -> bool:
    """沿线滑行: 空可持子格即可停; 敌子(非行营)可吃即停; 被占格挡住。"""
    i = cells.index(start)
    for step in (1, -1):
        j = i + step
        while 0 <= j < len(cells):
            cell = cells[j]
            if not can_hold_piece(*cell):
                j += step
                continue
            occ = board.get(cell)
            if occ is None:
                return True
            if occ.color != color and cell_kind(*cell) != "C":
                return True
            break
    return False


def piece_has_legal_move(board: dict[tuple[int, int], BoardPiece], color: str,
                         pos: tuple[int, int]) -> bool:
    """该子是否存在至少一个合法落点(高估可动性, 供"无棋可走"判定用)。

    近路按八邻接高估(含斜连/行营对角); 铁路沿线滑行; 工兵按铁路网可转弯。
    """
    r, c = pos
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            if _enterable(board, color, (r + dr, c + dc)):
                return True

    piece = board[pos].piece
    lines = [ln for ln in RAIL_LINES if _line_cells(ln, pos) is not None]
    for ln in lines:
        if _rail_scan(_line_cells(ln, pos), pos, board, color):
            return True

    if piece == int(ItemType.Gongbing):
        # 铁路网 BFS(工兵可转弯): 经无子铁路格连通, 落点判定同滑行
        seen = {pos}
        frontier = [pos]
        while frontier:
            cur = frontier.pop()
            for ln in RAIL_LINES:
                cells = _line_cells(ln, cur)
                if cells is None:
                    continue
                i = cells.index(cur)
                for j in (i - 1, i + 1):
                    if not 0 <= j < len(cells):
                        continue
                    cell = cells[j]
                    if cell in seen:
                        continue
                    if not can_hold_piece(*cell):
                        seen.add(cell)
                        frontier.append(cell)
                        continue
                    occ = board.get(cell)
                    if occ is None or (occ.color != color and cell_kind(*cell) != "C"):
                        return True
                    seen.add(cell)      # 被占格止步
        return False
    return False


def can_act(board: dict[tuple[int, int], BoardPiece], color: str) -> bool:
    """"无棋可走"判定(三情形): 无子/仅剩地雷军旗/存活子均无合法落点。"""
    return any(
        p.color == color and p.piece not in (int(ItemType.Dilei), int(ItemType.Junqi))
        and piece_has_legal_move(board, color, pos)
        for pos, p in board.items()
    )


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
    死亡口径同 QQ 裁定(2026-11-06): 认负/超时5次/扛旗即刻阵亡,
    退出仅为标志位; 行棋后判无棋可走(他家先行棋顺序, 胜利优先, 再判行棋方)。
    """
    from .qq_replay import _post_move_judgment

    alive = {color: True for color in SEAT_COLORS}
    defeated_at: dict[str, int] = {}

    def defeat(color: str | None) -> None:
        if color and alive.get(color):
            alive[color] = False
            clear_color(board, color)

    yield 0, board
    for kind, item in replay.seq:
        if kind == "event":
            if item.code == 3 or (item.code == 2 and item.b >= 5):
                defeat(item.color)
        else:
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
            if resolved.flag_down and resolved.defender:
                defeat(resolved.defender)
            _post_move_judgment(board, item.color, replay, alive, defeated_at,
                                item.no)
        yield item.no, board
