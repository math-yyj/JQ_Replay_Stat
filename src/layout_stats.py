# 批处理功能：递归遍历文件夹查找 .jgs 文件，统计布阵中每个位置上各子力的出现频率

from __future__ import annotations

import glob
import os

from .block import TOTAL_INFO_BLOCK_SIZE, JGSLayoutBlock, JGSTotalInfoBlock
from .jgs_types import GameException, ItemColor, ItemType, item_type2string


def find_jgs_files(folder: str | os.PathLike) -> list[str]:
    """用 glob.glob 递归遍历指定文件夹，查找所有 .jgs 文件"""
    pattern = os.path.join(str(folder), "**", "*.jgs")
    return sorted(glob.glob(pattern, recursive=True))


def load_total_info(jgs_file: str) -> JGSTotalInfoBlock:
    """读取一个 .jgs 文件的头部信息块（含主视角颜色与 4 位玩家信息）"""
    with open(jgs_file, "rb") as f:
        data = f.read(TOTAL_INFO_BLOCK_SIZE)

    if len(data) < TOTAL_INFO_BLOCK_SIZE:
        raise GameException("read file error 1")

    total_info = JGSTotalInfoBlock(data)
    if total_info.header_block.discription_raw[:10] != b"QQGame JQS":
        raise GameException("file discription not matched")

    return total_info


def load_layouts(jgs_file: str) -> list[JGSLayoutBlock]:
    """读取一个 .jgs 文件，返回其中 4 位玩家的初始布阵"""
    return [pib.player_layout for pib in load_total_info(jgs_file).player_info_block]


# 走棋顺序: 黄-紫-绿-蓝-黄……循环; 黄绿一方搭档, 蓝紫一方搭档
MOVE_ORDER = [ItemColor.YELLOW, ItemColor.PURPLE, ItemColor.GREEN, ItemColor.BLUE]


def next_mover(color: ItemColor) -> ItemColor:
    """下家: 走棋顺序中的下一家"""
    return MOVE_ORDER[(MOVE_ORDER.index(color) + 1) % 4]


def prev_mover(color: ItemColor) -> ItemColor:
    """上家: 走棋顺序中的上一家"""
    return MOVE_ORDER[(MOVE_ORDER.index(color) - 1) % 4]


def partner(color: ItemColor) -> ItemColor:
    """搭档: 黄↔绿, 蓝↔紫"""
    return MOVE_ORDER[(MOVE_ORDER.index(color) + 2) % 4]


# 筛选器: 名称 -> 函数(主视角颜色) -> 需统计的颜色集合
COLOR_SELECTORS: dict[str, callable] = {
    "全部四家": lambda self_color: set(MOVE_ORDER),
    "仅主视角": lambda self_color: {self_color},
    "仅主视角搭档": lambda self_color: {partner(self_color)},
    "仅上家": lambda self_color: {prev_mover(self_color)},
    "仅下家": lambda self_color: {next_mover(self_color)},
}

# 固定颜色筛选器: 与主视角无关, 直接按玩家颜色筛选
FIXED_COLOR_SELECTORS: dict[str, callable] = {
    "仅黄色": lambda self_color: {ItemColor.YELLOW},
    "仅蓝色": lambda self_color: {ItemColor.BLUE},
    "仅绿色": lambda self_color: {ItemColor.GREEN},
    "仅紫色": lambda self_color: {ItemColor.PURPLE},
    "黄绿双方": lambda self_color: {ItemColor.YELLOW, ItemColor.GREEN},
    "蓝紫双方": lambda self_color: {ItemColor.BLUE, ItemColor.PURPLE},
}


# 行营位置 (初始布局 6x5 坐标): 不可放子, 统计展示时留空
CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}


def _layout_is_empty(layout: JGSLayoutBlock) -> bool:
    """布阵全空(全为 JQNone)则该玩家未参与对局"""
    return all(
        layout.item(row, col) == ItemType.JQNone
        for row in range(JGSLayoutBlock.row_num)
        for col in range(JGSLayoutBlock.col_num)
    )


class LayoutStats:
    """布阵统计：每个位置(row, col)上各种子力的绝对频率与相对频率

    位置为 JGSLayoutBlock 的初始布局坐标，6 行 5 列，
    row 0 为最前排（靠近对方），row 5 为最后排（大本营所在排）。
    """

    def __init__(self):
        # _counts[row][col][ItemType] = 出现次数 (左右旗合并, 等于 left+right)
        self._counts: list[list[dict[ItemType, int]]] = [
            [{} for _ in range(JGSLayoutBlock.col_num)]
            for _ in range(JGSLayoutBlock.row_num)
        ]
        # 按军旗位置分组: 旗在左大本营(col=1) / 旗在右大本营(col=3)
        self._counts_flag_left = self._empty_counts()
        self._counts_flag_right = self._empty_counts()
        self._layout_count = 0  # 统计的布阵总数（受筛选影响, 每个文件 1~4 个）
        self._file_count = 0    # 成功解析的文件数
        self._failed_files: list[tuple[str, str]] = []  # (文件, 失败原因)
        self._skipped_1v1_files: list[str] = []  # 1v1(单挑)复盘, 不计入统计
        self._skipped_abnormal_files: list[tuple[str, str]] = []  # 联众异常复盘

    @staticmethod
    def _empty_counts():
        return [[{} for _ in range(JGSLayoutBlock.col_num)]
                for _ in range(JGSLayoutBlock.row_num)]

    @property
    def skipped_abnormal_files(self) -> list[tuple[str, str]]:
        return list(self._skipped_abnormal_files)

    def add_layout(self, layout: JGSLayoutBlock) -> None:
        flag_side = self._detect_flag_side_layout(layout)
        target = self._counts_flag_left if flag_side == "left" else self._counts_flag_right
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                item_type = layout.item(row, col)
                counter = self._counts[row][col]
                counter[item_type] = counter.get(item_type, 0) + 1
                tc = target[row][col]
                tc[item_type] = tc.get(item_type, 0) + 1
        self._layout_count += 1

    @staticmethod
    def _detect_flag_side_layout(layout: JGSLayoutBlock) -> str:
        """检测QQ布阵军旗位置: col=1 -> 'left', col=3 -> 'right'"""
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                if layout.item(row, col) == ItemType.Junqi:
                    return "left" if col <= 2 else "right"
        return "left"  # 默认归左

    @staticmethod
    def _detect_flag_side_matrix(board: list[list[str]]) -> str:
        """检测联众布阵军旗位置: col=1 -> 'left', col=3 -> 'right'"""
        for row in range(len(board)):
            for col in range(len(board[row])):
                if board[row][col] == "旗":
                    return "left" if col <= 2 else "right"
        return "left"

    def add_matrix(self, board: list[list[str]], char_to_itemtype: dict) -> None:
        """联众布阵专用: 接受 6x5 字符矩阵, 用映射表转为 ItemType 后统计"""
        flag_side = self._detect_flag_side_matrix(board)
        target = self._counts_flag_left if flag_side == "left" else self._counts_flag_right
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                ch = board[row][col] if row < len(board) and col < len(board[row]) else "空"
                item_type = char_to_itemtype.get(ch, ItemType.JQNone)
                counter = self._counts[row][col]
                counter[item_type] = counter.get(item_type, 0) + 1
                tc = target[row][col]
                tc[item_type] = tc.get(item_type, 0) + 1
        self._layout_count += 1

    def add_jqh_folder(self, folder: str | os.PathLike, colors: set[str],
                       char_to_itemtype: dict) -> list[str]:
        """递归统计文件夹下所有 .jqh 文件的联众布阵, 返回文件列表。

        先解析全部四色布阵, 验证通过后按 colors 筛选计入统计。"""
        from .lianzhong import decode_jqh_all_boards
        CAMP_POSITIONS_6x5 = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}
        FLAG_POSITIONS = {(5, 1), (5, 3)}
        ALL_COLORS = ["红", "绿", "蓝", "黑"]
        pattern = os.path.join(str(folder), "**", "*.jqh")
        files = sorted(glob.glob(pattern, recursive=True))
        for jqh_file in files:
            try:
                raw = open(jqh_file, "rb").read()
                boards, color_grids = decode_jqh_all_boards(raw)

                # ── 验证 ──
                skip_reasons = []
                for color in ALL_COLORS:
                    board = boards.get(color)
                    color_grid = color_grids.get(color)
                    if board is None:
                        continue
                    # 检查1: 每色25枚棋子
                    pieces = [(r, c, board[r][c]) for r in range(6) for c in range(5) if board[r][c] != "空"]
                    if len(pieces) != 25:
                        skip_reasons.append(f"{color}方棋子数={len(pieces)}(期望25)")
                        continue
                    # 检查2: A色棋子出现在B色阵地
                    for r in range(6):
                        for c in range(5):
                            if color_grid[r][c] not in ("空", color):
                                skip_reasons.append(f"{color}方阵地({r},{c})出现{color_grid[r][c]}色棋子{board[r][c]}")
                    # 检查3.1: 行营必须空
                    for r, c in CAMP_POSITIONS_6x5:
                        if board[r][c] != "空":
                            skip_reasons.append(f"{color}方行营({r},{c})非空:{board[r][c]}")
                    # 检查3.2: 炸弹不能在第0行
                    for r, c, ch in pieces:
                        if ch == "弹" and r == 0:
                            skip_reasons.append(f"{color}方炸弹在第0行({r},{c})")
                    # 检查3.3: 地雷只能在第4、5行
                    for r, c, ch in pieces:
                        if ch == "雷" and r not in (4, 5):
                            skip_reasons.append(f"{color}方地雷在第{r}行({r},{c})")
                    # 检查3.4: 军旗只能在(5,1)或(5,3)
                    for r, c, ch in pieces:
                        if ch == "旗" and (r, c) not in FLAG_POSITIONS:
                            skip_reasons.append(f"{color}方军旗在({r},{c})")

                if skip_reasons:
                    self._skipped_abnormal_files.append((jqh_file, "; ".join(skip_reasons)))
                    continue

                # ── 统计 ──
                has_valid = False
                for color, board in boards.items():
                    if all(ch == "空" for row in board for ch in row):
                        continue
                    has_valid = True
                    if color in colors:
                        self.add_matrix(board, char_to_itemtype)
                if has_valid:
                    self._file_count += 1
                else:
                    self._failed_files.append((jqh_file, "四色布阵全空"))
            except (OSError, AssertionError, ValueError, KeyError) as e:
                self._failed_files.append((jqh_file, str(e)))
        return files

    def add_file(self, jgs_file: str, color_selector=None, player_predicate=None) -> None:
        """统计一个文件

        color_selector(主视角颜色)->颜色集合, 仅统计在集合内的玩家布阵;
        player_predicate(玩家信息块, 主视角颜色)->bool, 按玩家属性(如QQ号)筛选;
        两者同时提供时取交集。
        若只有两家有布阵(1v1 单挑), 则整个文件不计入统计, 记录到 skipped_1v1_files"""
        total_info = load_total_info(jgs_file)

        active_count = sum(
            0 if _layout_is_empty(pib.player_layout) else 1
            for pib in total_info.player_info_block
        )
        if active_count == 2:
            self._skipped_1v1_files.append(jgs_file)
            return

        self_color = total_info.header_block.self_color
        colors = None if color_selector is None else color_selector(self_color)
        for pib in total_info.player_info_block:
            if colors is not None and pib.player_color not in colors:
                continue
            if player_predicate is not None and not player_predicate(pib, self_color):
                continue
            self.add_layout(pib.player_layout)
        self._file_count += 1

    def add_folder(self, folder: str | os.PathLike, color_selector=None,
                   player_predicate=None) -> list[str]:
        """递归统计文件夹下所有 .jgs 文件，返回文件列表；解析失败的文件跳过并记录"""
        files = find_jgs_files(folder)
        for jgs_file in files:
            try:
                self.add_file(jgs_file, color_selector, player_predicate)
            except (GameException, OSError, AssertionError, ValueError) as e:
                self._failed_files.append((jgs_file, str(e)))
        return files

    @property
    def layout_count(self) -> int:
        return self._layout_count

    @property
    def file_count(self) -> int:
        return self._file_count

    @property
    def failed_files(self) -> list[tuple[str, str]]:
        return list(self._failed_files)

    @property
    def skipped_1v1_files(self) -> list[str]:
        return list(self._skipped_1v1_files)

    def position_stat(self, row: int, col: int) -> list[tuple[ItemType, int, float]]:
        """指定位置的统计结果，按绝对频率降序排列

        返回 [(子力类型, 绝对频率, 相对频率), ...]，相对频率 = 次数 / 该位置样本总数
        """
        counter = self._counts[row][col]
        total = sum(counter.values())
        if total == 0:
            return []
        return sorted(
            ((t, c, c / total) for t, c in counter.items()),
            key=lambda x: (-x[1], x[0]),
        )

    def to_dict(self) -> dict:
        """导出为可 JSON 序列化的字典"""
        positions = {}
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                if (row, col) in CAMP_POSITIONS:
                    positions[f"r{row}c{col}"] = []  # 行营留空
                    continue
                positions[f"r{row}c{col}"] = [
                    {
                        "item": item_type2string(t),
                        "type_value": int(t),
                        "count": c,
                        "ratio": ratio,
                    }
                    for t, c, ratio in self.position_stat(row, col)
                ]
        return {
            "file_count": self._file_count,
            "layout_count": self._layout_count,
            "failed_files": [{"file": f, "reason": r} for f, r in self._failed_files],
            "skipped_1v1_files": list(self._skipped_1v1_files),
            "positions": positions,
        }

    def _cell_text(self, row: int, col: int, top_n: int = 5) -> str:
        """单个位置的单元格文本: top_n 频率 + 其它汇总, 格式 '司令： 35.2%(12次)'"""
        if (row, col) in CAMP_POSITIONS:
            return ""
        stats = self.position_stat(row, col)
        if not stats:
            return "(无样本)"
        lines = [
            f"{item_type2string(t)}：{ratio * 100:5.1f}%({c}次)"
            for t, c, ratio in stats[:top_n]
        ]
        rest = stats[top_n:]
        if rest:
            rest_count = sum(c for _, c, _ in rest)
            rest_ratio = rest_count / sum(c for _, c, _ in stats)
            lines.append(f"其它：{rest_ratio * 100:5.1f}%({rest_count}次)")
        return "\n".join(lines)

    def _cell_text_filtered(self, row: int, col: int, piece_filter: set[ItemType]) -> str:
        """棋子维度统计: 只展示 piece_filter 中棋子在该位置的频率"""
        if (row, col) in CAMP_POSITIONS:
            return ""
        counter = self._counts[row][col]
        total = sum(counter.values())
        if total == 0:
            return "(无样本)"
        filtered = [(t, c) for t, c in counter.items() if t in piece_filter]
        if not filtered:
            return "(无命中)"
        filtered.sort(key=lambda x: (-x[1], x[0]))
        lines = [
            f"{item_type2string(t)}：{c / total * 100:5.1f}%({c}次)"
            for t, c in filtered
        ]
        return "\n".join(lines)

    def _cell_text_filtered_mainline(self, row: int, col: int, piece_filter: set[ItemType]) -> str:
        """棋子维度统计(主线模式): 左旗 + 镜像右旗, 只展示选中棋子"""
        if (row, col) in CAMP_POSITIONS:
            return ""
        merged = self._merge_counts_mainline(row, col)
        total = sum(merged.values())
        if total == 0:
            return "(无样本)"
        filtered = [(t, c) for t, c in merged.items() if t in piece_filter]
        if not filtered:
            return "(无命中)"
        filtered.sort(key=lambda x: (-x[1], x[0]))
        lines = [
            f"{item_type2string(t)}：{c / total * 100:5.1f}%({c}次)"
            for t, c in filtered
        ]
        return "\n".join(lines)

    @staticmethod
    def _mirror_col(col: int) -> int:
        """镜像列索引: col <-> 4-col"""
        return JGSLayoutBlock.col_num - 1 - col

    def _merge_counts_mainline(self, row: int, col: int) -> dict[ItemType, int]:
        """主线(旗侧)模式: 左旗统计 + 镜像后的右旗统计"""
        merged: dict[ItemType, int] = {}
        # 左旗布局: 直接取 (row, col)
        for t, c in self._counts_flag_left[row][col].items():
            merged[t] = merged.get(t, 0) + c
        # 右旗布局: 取镜像位置 (row, 4-col)
        mirror_col = self._mirror_col(col)
        for t, c in self._counts_flag_right[row][mirror_col].items():
            merged[t] = merged.get(t, 0) + c
        return merged

    def _cell_text_mainline(self, row: int, col: int, top_n: int = 5) -> str:
        """主线(旗侧)模式的单元格文本: 左旗 + 镜像右旗"""
        if (row, col) in CAMP_POSITIONS:
            return ""
        merged = self._merge_counts_mainline(row, col)
        total = sum(merged.values())
        if total == 0:
            return "(无样本)"
        stats = sorted(
            ((t, c, c / total) for t, c in merged.items()),
            key=lambda x: (-x[1], x[0]),
        )
        lines = [
            f"{item_type2string(t)}：{ratio * 100:5.1f}%({c}次)"
            for t, c, ratio in stats[:top_n]
        ]
        rest = stats[top_n:]
        if rest:
            rest_count = sum(c for _, c, _ in rest)
            rest_ratio = rest_count / total
            lines.append(f"其它：{rest_ratio * 100:5.1f}%({rest_count}次)")
        return "\n".join(lines)

    def save_excel(self, xlsx_file: str | os.PathLike, top_n: int = 5,
                   mirror_cols: bool = False) -> None:
        """导出为 Excel: 6x5 带标题表格, 每个单元格内换行展示 top_n 频率及其它汇总

        mirror_cols=True 时(顺时针行棋习惯), 以中间列为轴镜像翻转列顺序展示"""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        wb = Workbook()
        ws = wb.active
        ws.title = "布阵统计"

        header_font = Font(bold=True)
        header_fill = PatternFill("solid", fgColor="D9E1F2")
        center = Alignment(horizontal="center", vertical="center")
        cell_align = Alignment(wrap_text=True, horizontal="left", vertical="top")
        thin = Side(style="thin")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        # 标题行/列: 左上角 + 列标题(第0~4列) + 行标题(第0~5行)
        corner = ws.cell(row=1, column=1, value="位置")
        corner.font = header_font
        corner.fill = header_fill
        corner.alignment = center
        corner.border = border
        for col in range(JGSLayoutBlock.col_num):
            c = ws.cell(row=1, column=col + 2, value=f"第{col + 1}列")
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border
        row_titles = ["第1行", "第2行", "第3行", "第4行", "第5行", "第6行"]
        for row in range(JGSLayoutBlock.row_num):
            c = ws.cell(row=row + 2, column=1, value=row_titles[row])
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border

        # 数据单元格: 格内换行显示 top_n 频率; 镜像时显示列 col 取数据列 (col_num-1-col)
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                data_col = JGSLayoutBlock.col_num - 1 - col if mirror_cols else col
                c = ws.cell(row=row + 2, column=col + 2,
                            value=self._cell_text(row, data_col, top_n))
                c.alignment = cell_align
                c.border = border

        # 列宽与行高 (每个单元格最多 top_n + 1 行文本)
        ws.column_dimensions["A"].width = 16
        for col in range(JGSLayoutBlock.col_num):
            ws.column_dimensions[chr(ord("B") + col)].width = 26
        for row in range(JGSLayoutBlock.row_num):
            ws.row_dimensions[row + 2].height = 15 * (top_n + 1) + 6

        # 汇总信息
        info_row = JGSLayoutBlock.row_num + 3
        ws.cell(row=info_row, column=1,
                value=f"统计文件数: {self._file_count} (失败 {len(self._failed_files)}, "
                      f"跳过1v1 {len(self._skipped_1v1_files)})")
        ws.cell(row=info_row + 1, column=1,
                value=f"统计布阵数: {self._layout_count}")

        wb.save(str(xlsx_file))

    def save_excel_filtered(self, xlsx_file: str | os.PathLike,
                           piece_filter: set[ItemType],
                           mirror_cols: bool = False) -> None:
        """导出棋子维度统计 Excel: 只展示被选中棋子的频率

        mirror_cols=True 时(顺时针行棋习惯), 以中间列为轴镜像翻转列顺序展示"""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        wb = Workbook()
        ws = wb.active
        ws.title = "棋子维度统计"

        header_font = Font(bold=True)
        header_fill = PatternFill("solid", fgColor="D9E1F2")
        center = Alignment(horizontal="center", vertical="center")
        cell_align = Alignment(wrap_text=True, horizontal="left", vertical="top")
        thin = Side(style="thin")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        corner = ws.cell(row=1, column=1, value="位置")
        corner.font = header_font
        corner.fill = header_fill
        corner.alignment = center
        corner.border = border
        for col in range(JGSLayoutBlock.col_num):
            c = ws.cell(row=1, column=col + 2, value=f"第{col + 1}列")
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border
        row_titles = ["第1行", "第2行", "第3行", "第4行", "第5行", "第6行"]
        for row in range(JGSLayoutBlock.row_num):
            c = ws.cell(row=row + 2, column=1, value=row_titles[row])
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border

        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                data_col = JGSLayoutBlock.col_num - 1 - col if mirror_cols else col
                c = ws.cell(row=row + 2, column=col + 2,
                            value=self._cell_text_filtered(row, data_col, piece_filter))
                c.alignment = cell_align
                c.border = border

        ws.column_dimensions["A"].width = 16
        for col in range(JGSLayoutBlock.col_num):
            ws.column_dimensions[chr(ord("B") + col)].width = 26
        for row in range(JGSLayoutBlock.row_num):
            ws.row_dimensions[row + 2].height = 15 * (len(piece_filter) + 1) + 6

        info_row = JGSLayoutBlock.row_num + 3
        ws.cell(row=info_row, column=1,
                value=f"统计文件数: {self._file_count} (失败 {len(self._failed_files)}, "
                      f"跳过1v1 {len(self._skipped_1v1_files)})")
        ws.cell(row=info_row + 1, column=1,
                value=f"统计布阵数: {self._layout_count}")
        ws.cell(row=info_row + 2, column=1,
                value=f"筛选棋子: {', '.join(item_type2string(t) for t in sorted(piece_filter, key=lambda x: int(x)))}")

        wb.save(str(xlsx_file))

    def report(self) -> str:
        """生成文本统计报告"""
        lines = [
            f"统计文件数  : {self._file_count} (失败 {len(self._failed_files)}, "
            f"跳过1v1 {len(self._skipped_1v1_files)})",
            f"统计布阵数  : {self._layout_count}",
            "",
            "各位置子力频率 (位置为初始布局坐标):",
        ]
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                if (row, col) in CAMP_POSITIONS:
                    lines.append(f"\n[第{row + 1}行, 第{col + 1}列] (行营, 留空)")
                    continue
                stats = self.position_stat(row, col)
                lines.append(f"\n[第{row + 1}行, 第{col + 1}列]")
                if not stats:
                    lines.append("  (无样本)")
                    continue
                for t, c, ratio in stats:
                    lines.append(f"  {item_type2string(t)}: {c:6d}  ({ratio:7.2%})")

        if self._skipped_1v1_files:
            lines.append("\n跳过的 1v1(单挑) 复盘:")
            for f in self._skipped_1v1_files:
                lines.append(f"  {f}")

        if self._failed_files:
            lines.append("\n解析失败的文件:")
            for f, r in self._failed_files:
                lines.append(f"  {f}: {r}")

        return "\n".join(lines)

    def summary_grid(self) -> str:
        """每个位置出现频率最高的子力，按 6x5 布局网格展示"""
        lines = []
        for row in range(JGSLayoutBlock.row_num):
            cells = []
            for col in range(JGSLayoutBlock.col_num):
                if (row, col) in CAMP_POSITIONS:
                    cells.append("        ")
                    continue
                stats = self.position_stat(row, col)
                if not stats:
                    cells.append("  --   ")
                else:
                    t, _, ratio = stats[0]
                    cells.append(f"{item_type2string(t)}{ratio:6.1%}")
            lines.append("  ".join(cells))
        return "\n".join(lines)
