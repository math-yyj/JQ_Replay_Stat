# 布阵统计可视化界面 (tkinter)
# 功能: 选择文件夹递归统计 .jgs/.jqh 布阵, 6x5 网格展示频率, 可导出 Excel
# 四个互相独立的统计页:
#   1. 主视角统计: 勾选 自家/上家/对家/下家, 取并集 (仅QQ)
#   2. 颜色统计:   勾选 黄/紫/绿/蓝, 取并集 (仅QQ)
#   3. QQ号码统计: 输入QQ号, 只统计该玩家的布阵 (仅QQ)
#   4. 棋子维度统计: 勾选12种棋子, 只统计选中棋子在各位置的频率 (QQ+联众)
#   5. 高级筛选: 按组合条件筛选、按布阵文件筛选、按手工布阵筛选 (QQ+联众)
# 用法: python stats_gui.py

from __future__ import annotations

import glob
import json
import math
import os
import queue
import struct
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import LayoutStats, ItemColor, ItemType
from src.block import JGSLayoutBlock
from src.layout_stats import next_mover, partner, prev_mover
from src.qq_board import (
    CANONICAL_SEAT, COLOR_FILL, COLOR_LINE, COLOR_TEXT, COLOR_TEXT_ON,
    PIECE_ORDER, RAIL_LINES, REGION_FILL, SEAT_COLORS, can_hold_piece,
    cell_kind, iter_states, layout_is_valid, legal_cells,
    normalize_board, region_of, rotation_times,
)
from src.qq_replay import COLOR_ORDER, DIR_CN, PIECE_SHORT, parse_jgs
from src.glworld_replay import (
    COLOR_CN as GL_COLOR_CN,
    COLOR_ORDER as GL_COLOR_ORDER,
    PIECE_NAME as GL_PIECE_NAME,
    ROW_LETTERS as GL_ROW_LETTERS,
    parse_glworld_text,
)

ROW_TITLES = ["第1行", "第2行", "第3行", "第4行", "第5行", "第6行"]

HEADER_BG = "#D9E1F2"
CELL_BG = "#FFFFFF"
CAMP_BG = "#D0D0D0"  # 行营灰色背景
EMPTY_TEXT = "(无样本)"

# 行营位置 (0-indexed)
CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}


class BasePanel(ttk.Frame):
    """面板基类: 提供行营位置常量和公共方法"""

    CAMP_POSITIONS = CAMP_POSITIONS

    @property
    def _top(self):
        """获取顶层窗口，用于对话框parent参数"""
        return self.winfo_toplevel()

    def _is_camp(self, row: int, col: int) -> bool:
        """判断是否为行营位置"""
        return (row, col) in self.CAMP_POSITIONS

    def _layout_to_key(self, layout_data) -> tuple:
        """将布阵转换为可哈希的元组用于去重"""
        if isinstance(layout_data, JGSLayoutBlock):
            return tuple(layout_data.item(r, c) for r in range(6) for c in range(5))
        else:
            return tuple(layout_data[r][c] for r in range(6) for c in range(5))

    def _layout_has_invalid_piece(self, layout_data) -> str | None:
        """检查布阵非行营位置是否含空位/非法棋子/缺少军旗
        
        Returns:
            错误信息字符串，合法返回 None
        """
        has_flag = False
        for row in range(6):
            for col in range(5):
                if (row, col) in self.CAMP_POSITIONS:
                    continue
                if isinstance(layout_data, JGSLayoutBlock):
                    item = layout_data.item(row, col)
                else:
                    item = layout_data[row][col]
                if item == ItemType.JQNone:
                    return f"位置({row},{col})为空"
                if item == ItemType.Junqi:
                    has_flag = True
        if not has_flag:
            return "布阵缺少军旗"
        return None

    def _create_camp_label(self, parent, row: int, col: int, **kwargs) -> tk.Label:
        """创建行营格子标签"""
        defaults = {
            "text": "",
            "bg": CAMP_BG,
            "relief": tk.GROOVE,
            "bd": 1,
            "height": 6,
        }
        defaults.update(kwargs)
        lbl = tk.Label(parent, **defaults)
        lbl.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
        return lbl


class StatPanel(BasePanel):
    """独立统计面板基类: 自带统计/导出按钮/汇总栏/6x5 网格与统计结果,
    筛选控件由子类实现 (_build_filter/_get_filter), 多个面板之间互不干扰"""

    def __init__(self, parent, folder_var: tk.StringVar, export_name: str,
                 mirror_getter=None, mainline_getter=None, dedup_getter=None,
                 display_mode_getter=None):
        super().__init__(parent, padding=2)
        self._folder_var = folder_var  # 与其它面板共享文件夹输入, 只读
        self._export_name = export_name
        self._mirror_getter = mirror_getter or (lambda: False)  # 行棋习惯: 顺时针时镜像显示
        self._mainline_getter = mainline_getter or (lambda: False)
        self._dedup_getter = dedup_getter or (lambda: False)
        self._display_mode_getter = display_mode_getter or (lambda: "百分比")
        self.stats: LayoutStats | None = None
        self._filter_desc = ""

        self._build_toolbar()
        self._build_summary()
        self._build_grid()

    # ---------- 子类需实现 ----------

    def _build_filter(self, bar: ttk.Frame) -> None:
        """在工具栏上构建筛选控件"""
        raise NotImplementedError

    def _get_filter(self):
        """返回 (color_selector, player_predicate, 描述文本); 输入非法时提示并返回 None"""
        raise NotImplementedError

    # ---------- UI 构建 ----------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))

        self._build_filter(bar)

        self.run_btn = ttk.Button(bar, text="开始统计", command=self._start_stat)
        self.run_btn.pack(side=tk.LEFT)
        self.export_btn = ttk.Button(
            bar, text="导出 Excel", command=self._export_excel, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=(8, 0))

    def _build_summary(self) -> None:
        self.summary_var = tk.StringVar(value="请选择文件夹并点击\"开始统计\"")
        ttk.Label(self, textvariable=self.summary_var).pack(anchor=tk.W, pady=(0, 4))

    def _build_grid(self) -> None:
        grid = ttk.Frame(self)
        grid.pack(fill=tk.BOTH, expand=True)

        # 表头: 左上角 + 列标题
        tk.Label(grid, text="位置", bg=HEADER_BG, relief=tk.RIDGE, bd=1,
                 font=("", 10, "bold")).grid(row=0, column=0, sticky="nsew")
        for col in range(JGSLayoutBlock.col_num):
            tk.Label(grid, text=f"第{col + 1}列", bg=HEADER_BG, relief=tk.RIDGE, bd=1,
                     font=("", 10, "bold")).grid(row=0, column=col + 1, sticky="nsew")

        # 行标题 + 数据单元格 (使用 Text 控件支持彩色文字)
        self.cell_texts: list[list[tk.Text]] = []
        for row in range(JGSLayoutBlock.row_num):
            tk.Label(grid, text=ROW_TITLES[row], bg=HEADER_BG, relief=tk.RIDGE, bd=1,
                     font=("", 10, "bold")).grid(row=row + 1, column=0, sticky="nsew")
            row_texts = []
            for col in range(JGSLayoutBlock.col_num):
                is_camp = (row, col) in CAMP_POSITIONS
                txt = tk.Text(grid,
                              bg=CAMP_BG if is_camp else CELL_BG,
                              relief=tk.RIDGE, bd=1,
                              wrap=tk.WORD,
                              font=("Consolas", 10), height=6, width=26,
                              state=tk.DISABLED if is_camp else tk.NORMAL)
                txt.grid(row=row + 1, column=col + 1, sticky="nsew")
                if not is_camp:
                    txt.insert("1.0", EMPTY_TEXT)
                    txt.config(state=tk.DISABLED)
                row_texts.append(txt)
            self.cell_texts.append(row_texts)

        for col in range(JGSLayoutBlock.col_num + 1):
            grid.columnconfigure(col, weight=0 if col == 0 else 1)
        for row in range(JGSLayoutBlock.row_num + 1):
            grid.rowconfigure(row, weight=0 if row == 0 else 1)

    # ---------- 清空统计 ----------

    def clear_stats(self) -> None:
        """清空统计结果并重置网格显示"""
        self.stats = None
        self._filter_desc = ""
        self.summary_var.set("请选择文件夹并点击\"开始统计\"")
        self.export_btn.config(state=tk.DISABLED)
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                is_camp = (row, col) in CAMP_POSITIONS
                txt = self.cell_texts[row][col]
                txt.config(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                if not is_camp:
                    txt.insert("1.0", EMPTY_TEXT)
                    txt.config(state=tk.DISABLED)

    # ---------- 事件处理 ----------

    def _start_stat(self) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        flt = self._get_filter()
        if flt is None:
            return
        selector, predicate, self._filter_desc = flt

        self.run_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.summary_var.set("统计中……")

        total_files_ref = [0]  # 用列表以便闭包修改

        def progress_callback(done, total):
            total_files_ref[0] = total
            self.summary_var.set(f"统计中…… {done}/{total}")

        # 后台线程统计, 避免大量文件时界面卡死
        dedup = self._dedup_getter()

        def worker():
            stats = LayoutStats(dedup=dedup)
            try:
                files = stats.add_folder(folder, selector, predicate, progress_callback)
                self._result = (stats, len(files), None)
            except Exception as e:  # 兜底, 正常失败已被 add_folder 记录
                self._result = (None, 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _poll_worker(self) -> None:
        if self._worker.is_alive():
            self.after(100, self._poll_worker)
            return

        stats, total_files, err = self._result
        self.run_btn.config(state=tk.NORMAL)

        if err is not None:
            self.summary_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err, parent=self._top)
            return
        if total_files == 0:
            self.summary_var.set("未找到 .jgs 文件")
            messagebox.showinfo("提示", "该文件夹下未找到 .jgs 文件", parent=self._top)
            return

        self.stats = stats
        self._refresh_grid()
        failed = len(stats.failed_files)
        skipped = len(stats.skipped_1v1_files)
        abnormal = len(stats.skipped_abnormal_files)
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"跳过1v1: {skipped}    跳过异常: {abnormal}    "
                   f"统计布阵数: {stats.layout_count}")
        self.summary_var.set(summary)
        self.export_btn.config(state=tk.NORMAL)

        # 合并异常提示
        if skipped or abnormal or failed:
            parts = []
            if skipped:
                paths = [str(Path(f).name) for f in stats.skipped_1v1_files[:10]]
                block = "\n".join(paths)
                if skipped > 10:
                    block += f"\n……共 {skipped} 个"
                parts.append(f"【跳过1v1({skipped})】\n{block}")
            if abnormal:
                paths = [str(Path(f).name) for f, _ in stats.skipped_abnormal_files[:10]]
                block = "\n".join(paths)
                if abnormal > 10:
                    block += f"\n……共 {abnormal} 个"
                parts.append(f"【跳过异常({abnormal})】\n{block}")
            if failed:
                paths = [str(Path(f).name) for f, _ in stats.failed_files[:10]]
                block = "\n".join(paths)
                if failed > 10:
                    block += f"\n……共 {failed} 个"
                parts.append(f"【解析失败({failed})】\n{block}")
            messagebox.showwarning("部分复盘未计入统计", "\n\n".join(parts), parent=self._top)

    @staticmethod
    def _rank_color(rank: int) -> str:
        """根据排名返回颜色: top1=红(255,0,0), top2=(127,0,0), top3=(63,0,0), 其它=黑"""
        colors = {1: "#FF0000", 2: "#7F0000", 3: "#3F0000"}
        return colors.get(rank, "#000000")

    def _refresh_grid(self) -> None:
        """刷新网格, 支持彩色文字"""
        if self.stats is None:
            return
        is_mainline = self._mainline_getter()
        mirror = self._mirror_getter()
        show_pct = self._display_mode_getter() == "百分比"
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                is_camp = (row, col) in CAMP_POSITIONS
                txt = self.cell_texts[row][col]
                txt.config(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                if is_camp:
                    txt.config(state=tk.DISABLED)
                    continue
                data_col = JGSLayoutBlock.col_num - 1 - col if mirror else col
                if is_mainline:
                    lines = self.stats._cell_text_mainline_with_ranks(row, data_col, show_pct)
                else:
                    lines = self.stats._cell_text_with_ranks(row, data_col, show_pct)
                for i, (line_text, segs) in enumerate(lines):
                    if i > 0:
                        txt.insert(tk.END, "\n")
                    for j, (seg_text, rank) in enumerate(segs):
                        if j > 0:
                            txt.insert(tk.END, "  ")  # 两个棋子之间空格分隔
                        color = self._rank_color(rank)
                        tag = f"r{i}_{j}"
                        txt.tag_configure(tag, foreground=color)
                        start = txt.index(tk.INSERT)
                        txt.insert(tk.END, seg_text)
                        end = txt.index(tk.INSERT)
                        txt.tag_add(tag, start, end)
                txt.config(state=tk.DISABLED)

    def _export_excel(self) -> None:
        if self.stats is None:
            return
        xlsx_file = filedialog.asksaveasfilename(
            title="导出 Excel", defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx")], initialfile=self._export_name,
            parent=self.winfo_toplevel())
        if not xlsx_file:
            return
        try:
            self.stats.save_excel(xlsx_file, mirror_cols=self._mirror_getter())
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            return
        messagebox.showinfo("完成", f"已导出: {xlsx_file}", parent=self._top)


class PerspectivePanel(StatPanel):
    """主视角统计: 勾选 自家/上家/对家/下家, 并集筛选"""

    RELATIONS = [
        ("自家", lambda self_color: self_color),
        ("上家", prev_mover),
        ("对家", partner),
        ("下家", next_mover),
    ]

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="筛选(并集):").pack(side=tk.LEFT)
        self.relation_vars: dict[str, tk.BooleanVar] = {}
        for name, _ in self.RELATIONS:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text=name, variable=var).pack(side=tk.LEFT, padx=(4, 0))
            self.relation_vars[name] = var
        ttk.Label(bar, text="  ").pack(side=tk.LEFT)

    def _get_filter(self):
        chosen = [(name, fn) for name, fn in self.RELATIONS
                  if self.relation_vars[name].get()]
        if not chosen:
            messagebox.showwarning("提示", "请至少勾选一个筛选条件", parent=self._top)
            return None
        fns = [fn for _, fn in chosen]
        selector = lambda self_color: {fn(self_color) for fn in fns}
        desc = "主视角[" + "+".join(name for name, _ in chosen) + "]"
        return selector, None, desc


class ColorPanel(StatPanel):
    """颜色(兼座次)统计: 勾选 颜色/座次, 并集筛选"""

    COLORS = [
        ("红(先手位)", ItemColor.YELLOW),
        ("紫(二号位)", ItemColor.PURPLE),
        ("绿(三号位)", ItemColor.GREEN),
        ("蓝(四号位)", ItemColor.BLUE),
    ]

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="筛选(并集):").pack(side=tk.LEFT)
        self.color_vars: dict[str, tk.BooleanVar] = {}
        for name, _ in self.COLORS:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text=name, variable=var).pack(side=tk.LEFT, padx=(4, 0))
            self.color_vars[name] = var
        ttk.Label(bar, text="  ").pack(side=tk.LEFT)

    def _get_filter(self):
        chosen = [(name, color) for name, color in self.COLORS
                  if self.color_vars[name].get()]
        if not chosen:
            messagebox.showwarning("提示", "请至少勾选一个颜色", parent=self._top)
            return None
        colors = {color for _, color in chosen}
        selector = lambda self_color: colors
        desc = "颜色[" + "+".join(name for name, _ in chosen) + "]"
        return selector, None, desc


class QQPanel(StatPanel):
    """QQ号码统计: 白名单/黑名单筛选QQ号布阵, 支持导出筛选结果"""

    def __init__(self, parent, folder_var, export_name, mirror_getter=None,
                 mainline_getter=None, dedup_getter=None, display_mode_getter=None,
                 source_var=None):
        self._source_var = source_var
        self.matched_layouts = []  # 保存筛选结果用于导出
        super().__init__(parent, folder_var, export_name, mirror_getter, mainline_getter, dedup_getter, display_mode_getter)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))

        self._build_filter(bar)

        self.run_btn = ttk.Button(bar, text="开始统计", command=self._start_stat)
        self.run_btn.pack(side=tk.LEFT)
        self.export_replay_btn = ttk.Button(
            bar, text="导出复盘", command=self._export_replays, state=tk.DISABLED)
        self.export_replay_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.export_btn = ttk.Button(
            bar, text="导出布阵", command=self._export_layouts, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=(4, 0))

        # 导出格式选择
        self.export_format_var = tk.StringVar(value="联众(jqs)")
        for fmt in ("联众(jqs)", "QQ(jql)"):
            ttk.Radiobutton(bar, text=fmt, value=fmt,
                            variable=self.export_format_var).pack(side=tk.LEFT, padx=(4, 0))

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="白名单:").pack(side=tk.LEFT)
        self.whitelist_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.whitelist_var, width=20).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(bar, text="黑名单:").pack(side=tk.LEFT)
        self.blacklist_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.blacklist_var, width=20).pack(side=tk.LEFT, padx=(4, 8))

    def _parse_qq_list(self, text: str) -> set[int]:
        """解析逗号分隔的QQ号码列表"""
        result = set()
        for item in text.split(","):
            item = item.strip()
            if item.isdigit() and int(item) > 0:
                result.add(int(item))
        return result

    def _get_filter(self):
        whitelist = self._parse_qq_list(self.whitelist_var.get())
        blacklist = self._parse_qq_list(self.blacklist_var.get())

        if not whitelist and not blacklist:
            messagebox.showwarning("提示", "请至少填写白名单或黑名单中的一个QQ号码", parent=self._top)
            return None

        wl = whitelist
        bl = blacklist

        def predicate(pib, self_color):
            qq = pib.player_qqnumber
            if qq == 0:
                return False  # QQ未知的不统计
            if wl and qq not in wl:
                return False  # 有白名单但不在白名单中
            if bl and qq in bl:
                return False  # 在黑名单中
            return True

        parts = []
        if wl:
            parts.append(f"白名单[{','.join(str(q) for q in sorted(wl))}]")
        if bl:
            parts.append(f"黑名单[{','.join(str(q) for q in sorted(bl))}]")
        desc = "+".join(parts)

        return None, predicate, desc

    def _start_stat(self) -> None:
        """重写统计方法，保存匹配的布局用于导出"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        flt = self._get_filter()
        if flt is None:
            return
        selector, predicate, self._filter_desc = flt

        self.run_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.summary_var.set("统计中……")

        dedup = self._dedup_getter()

        def progress_callback(done, total):
            self.summary_var.set(f"统计中…… {done}/{total}")

        def worker():
            stats = LayoutStats(dedup=dedup)
            matched = []
            try:
                from src.layout_stats import find_jgs_files, load_total_info, _layout_is_empty
                files = find_jgs_files(folder)
                total = len(files)
                for i, jgs_file in enumerate(files):
                    try:
                        total_info = load_total_info(jgs_file)
                        # 检查是否有非法棋子
                        skip = False
                        for pib in total_info.player_info_block:
                            err = self._layout_has_invalid_piece(pib.player_layout)
                            if err:
                                stats._skipped_abnormal_files.append((jgs_file, err))
                                skip = True
                                break
                        if skip:
                            continue
                        self_color = total_info.header_block.self_color
                        for pib in total_info.player_info_block:
                            if _layout_is_empty(pib.player_layout):
                                continue
                            if predicate(pib, self_color):
                                stats.add_layout(pib.player_layout)
                                matched.append((jgs_file, pib.player_color, pib.player_layout))
                        stats._file_count += 1
                    except Exception:
                        stats._failed_files.append((jgs_file, "解析失败"))
                    if progress_callback:
                        progress_callback(i + 1, total)
                self._result = (stats, len(files), None, matched)
            except Exception as e:
                self._result = (None, 0, str(e), [])

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _poll_worker(self) -> None:
        if self._worker.is_alive():
            self.after(100, self._poll_worker)
            return

        stats, total_files, err, matched = self._result
        self.run_btn.config(state=tk.NORMAL)

        if err is not None:
            self.summary_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err, parent=self._top)
            return
        if total_files == 0:
            self.summary_var.set("未找到 .jgs 文件")
            messagebox.showinfo("提示", "该文件夹下未找到 .jgs 文件", parent=self._top)
            return

        self.stats = stats
        self.matched_layouts = matched
        self._refresh_grid()
        failed = len(stats.failed_files)
        # 计算去重后的复盘文件数
        unique_replay_files = len(set(fp for fp, _, _ in matched)) if matched else 0
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"统计布阵数: {stats.layout_count}    去重复盘数: {unique_replay_files}")
        self.summary_var.set(summary)
        self.export_btn.config(state=tk.NORMAL if matched else tk.DISABLED)
        self.export_replay_btn.config(state=tk.NORMAL if matched else tk.DISABLED)

        if failed:
            paths = [str(Path(f).name) for f, _ in stats.failed_files[:10]]
            detail = "\n".join(paths)
            if failed > 10:
                detail += f"\n……共 {failed} 个"
            messagebox.showwarning("部分文件解析失败", detail, parent=self._top)

    def _export_layouts(self) -> None:
        """导出筛选后的布阵文件"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布阵", parent=self._top)
            return

        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
        if not save_dir:
            return

        try:
            is_jqs = self.export_format_var.get() == "联众(jqs)"

            # JQS格式常量
            JQS_HEADER = bytes([
                0x47, 0x4C, 0xE9, 0x03, 0xCE, 0x07, 0xC4, 0x02, 0x53, 0x61, 0x74, 0x20,
                0x41, 0x75, 0x67, 0x20, 0x30, 0x31, 0x20, 0x32, 0x32, 0x3A, 0x34, 0x35,
                0x3A, 0x30, 0x33, 0x20, 0x32, 0x30, 0x32, 0x36, 0x0A, 0x00, 0x00, 0x00,
                0x62, 0x79, 0x20, 0x5B, 0xD1, 0xD0, 0xBE, 0xBF, 0x31, 0x5D, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00
            ])
            JQS_LAYOUT_OFFSET = 0x370
            JQS_ROW_STRIDE = 17
            JQS_FILE_SIZE = 1264
            PIECE_TO_JQS = {
                ItemType.JQNone: 0x00, ItemType.Junzhang: 0x01, ItemType.Shizhang: 0x02,
                ItemType.Lvzhang: 0x03, ItemType.Tuanzhang: 0x04, ItemType.Yinzhang: 0x05,
                ItemType.Lianzhang: 0x06, ItemType.Paizhang: 0x07, ItemType.Gongbing: 0x08,
                ItemType.Junqi: 0x09, ItemType.Silin: 0x0A, ItemType.Dilei: 0x0B,
                ItemType.Zhadan: 0x0C,
            }

            # JQL格式常量
            JQL_HEADER = bytes([0x51, 0x51, 0x47, 0x61, 0x6D, 0x65, 0x20, 0x4A,
                                0x51, 0x4C, 0x20, 0x46, 0x69, 0x6C, 0x65, 0x00,
                                0x57, 0x04, 0x00, 0x00])
            JQL_LAYOUT_OFFSET = 0x14
            JQL_FILE_SIZE = 50

            # 按布阵内容去重
            seen_layouts = set()
            unique_layouts = []
            for file_path, color, layout_data in self.matched_layouts:
                key = self._layout_to_key(layout_data)
                if key not in seen_layouts:
                    seen_layouts.add(key)
                    unique_layouts.append((file_path, color, layout_data))

            total_count = len(unique_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported_count = 0
            for i, (file_path, color, layout_data) in enumerate(unique_layouts):
                original_name = Path(file_path).stem
                ext = ".jqs" if is_jqs else ".jql"
                new_filename = f"{i:0{prefix_digits}d}_{original_name}{ext}"
                output_path = os.path.join(save_dir, new_filename)

                if is_jqs:
                    data = bytearray(JQS_FILE_SIZE)
                    data[0:len(JQS_HEADER)] = JQS_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQS_LAYOUT_OFFSET + (row * JQS_ROW_STRIDE + col) * 4
                            pt = layout_data.item(row, col)
                            code = 0x00 if (row, col) in CAMP_POSITIONS else PIECE_TO_JQS.get(pt, 0x00)
                            struct.pack_into('<I', data, offset, code)
                else:
                    data = bytearray(JQL_FILE_SIZE)
                    data[0:20] = JQL_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQL_LAYOUT_OFFSET + row * 5 + col
                            data[offset] = int(layout_data.item(row, col))

                with open(output_path, 'wb') as f:
                    f.write(data)

                exported_count += 1
                self.summary_var.set(f"导出中…… {exported_count}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported_count} 个布局文件到:\n{save_dir}", parent=self._top)
            self.summary_var.set(f"已导出 {exported_count} 个布局文件")

        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.summary_var.set(f"导出失败: {e}")

    def _export_replays(self) -> None:
        """导出筛选后的复盘文件（按文件去重，仅导出解析正常的文件）"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            # 按复盘文件去重
            seen_files = set()
            unique_files = []
            for file_path, color, layout_data in self.matched_layouts:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)

            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            import shutil
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                shutil.copy2(src_path, output_path)
                exported += 1
                self.summary_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
            self.summary_var.set(f"已导出 {exported} 个复盘文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.summary_var.set(f"导出失败: {e}")


class LianzhongPanel(StatPanel):
    """联众复盘统计: 解析 .jqh 文件, 按红/绿/蓝/黑四色统计布阵频率"""

    COLORS = ["红", "绿", "蓝", "黑"]

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="颜色(并集):").pack(side=tk.LEFT)
        self.color_vars: dict[str, tk.BooleanVar] = {}
        for name in self.COLORS:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text=name, variable=var).pack(side=tk.LEFT, padx=(4, 0))
            self.color_vars[name] = var
        ttk.Label(bar, text="  ").pack(side=tk.LEFT)

    def _get_filter(self):
        chosen = [name for name in self.COLORS if self.color_vars[name].get()]
        if not chosen:
            messagebox.showwarning("提示", "请至少勾选一个颜色", parent=self._top)
            return None
        desc = "颜色[" + "+".join(chosen) + "]"
        return None, None, desc

    def _start_stat(self) -> None:
        """联众模式重写: 用 add_jqh_folder 替代 add_folder"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        flt = self._get_filter()
        if flt is None:
            return
        _, _, self._filter_desc = flt

        chosen_colors = {name for name in self.COLORS if self.color_vars[name].get()}

        self.run_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.summary_var.set("统计中……")

        from src.lianzhong import get_char_to_itemtype
        c2i = get_char_to_itemtype()

        def progress_callback(done, total):
            self.summary_var.set(f"统计中…… {done}/{total}")

        dedup = self._dedup_getter()

        def worker():
            stats = LayoutStats(dedup=dedup)
            try:
                files = stats.add_jqh_folder(folder, chosen_colors, c2i, progress_callback)
                self._result = (stats, len(files), None)
            except Exception as e:
                self._result = (None, 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)


class LianzhongFirstPanel(LianzhongPanel):
    """联众先手方视角统计: 按座次(先手位/二号位/三号位/四号位)统计布阵频率

    逻辑与 LianzhongPanel 完全一致，仅颜色名称和按钮状态不同。"""

    COLORS = ["先手位", "二号位", "三号位", "四号位"]

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="座次(并集):").pack(side=tk.LEFT)
        self.color_vars: dict[str, tk.BooleanVar] = {}
        for name in self.COLORS:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text=name, variable=var).pack(side=tk.LEFT, padx=(4, 0))
            self.color_vars[name] = var
        ttk.Label(bar, text="  ").pack(side=tk.LEFT)

    def _build_toolbar(self) -> None:
        """重写工具栏: 开始统计按钮暂时禁用"""
        super()._build_toolbar()
        self.run_btn.config(state=tk.DISABLED)


class LianzhongIDPanel(StatPanel):
    """联众ID统计: 按用户名黑白名单筛选联众布阵, 支持导出筛选结果"""

    def __init__(self, parent, folder_var, export_name, mirror_getter=None,
                 mainline_getter=None, dedup_getter=None, display_mode_getter=None):
        self.matched_layouts = []
        super().__init__(parent, folder_var, export_name, mirror_getter, mainline_getter, dedup_getter, display_mode_getter)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))
        self._build_filter(bar)
        self.run_btn = ttk.Button(bar, text="开始统计", command=self._start_stat)
        self.run_btn.pack(side=tk.LEFT)
        self.export_replay_btn = ttk.Button(bar, text="导出复盘", command=self._export_replays, state=tk.DISABLED)
        self.export_replay_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.export_btn = ttk.Button(bar, text="导出布阵", command=self._export_layouts, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.export_format_var = tk.StringVar(value="联众(jqs)")
        for fmt in ("联众(jqs)", "QQ(jql)"):
            ttk.Radiobutton(bar, text=fmt, value=fmt,
                            variable=self.export_format_var).pack(side=tk.LEFT, padx=(4, 0))

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="白名单:").pack(side=tk.LEFT)
        self.whitelist_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.whitelist_var, width=20).pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(bar, text="黑名单:").pack(side=tk.LEFT)
        self.blacklist_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.blacklist_var, width=20).pack(side=tk.LEFT, padx=(4, 8))

    def _parse_id_list(self, text: str) -> set[str]:
        return {item.strip() for item in text.split(",") if item.strip()}

    def _get_filter(self):
        whitelist = self._parse_id_list(self.whitelist_var.get())
        blacklist = self._parse_id_list(self.blacklist_var.get())
        if not whitelist and not blacklist:
            messagebox.showwarning("提示", "请至少填写白名单或黑名单中的一个ID", parent=self._top)
            return None
        wl = whitelist
        bl = blacklist
        def predicate(layout_info_block, self_color):
            # 联众模式下, 通过 player_name 判断
            return True  # 实际过滤在 _start_stat 中完成
        parts = []
        if wl:
            parts.append(f"白名单[{','.join(sorted(wl))}]")
        if bl:
            parts.append(f"黑名单[{','.join(sorted(bl))}]")
        desc = "+".join(parts)
        return None, predicate, desc

    def _start_stat(self) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        flt = self._get_filter()
        if flt is None:
            return
        _, _, self._filter_desc = flt

        whitelist = self._parse_id_list(self.whitelist_var.get())
        blacklist = self._parse_id_list(self.blacklist_var.get())

        self.run_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.summary_var.set("统计中……")

        dedup = self._dedup_getter()

        def progress_callback(done, total):
            self.summary_var.set(f"统计中…… {done}/{total}")

        def worker():
            stats = LayoutStats(dedup=dedup)
            matched = []
            try:
                from src.lianzhong import decode_jqh_all_boards, decode_jqh_all_usernames, get_char_to_itemtype
                c2i = get_char_to_itemtype()
                ALL_COLORS = ["红", "绿", "蓝", "黑"]
                pattern = os.path.join(folder, "**", "*.jqh")
                files = sorted(glob.glob(pattern, recursive=True))
                total = len(files)
                for i, jqh_file in enumerate(files):
                    try:
                        raw = open(jqh_file, "rb").read()
                        boards, _ = decode_jqh_all_boards(raw)
                        usernames = decode_jqh_all_usernames(raw)
                        # 检查是否有非法棋子(非行营位置为空)
                        skip = False
                        for color in ALL_COLORS:
                            board = boards.get(color)
                            if board is None:
                                continue
                            for row in range(6):
                                for col in range(5):
                                    if (row, col) in self.CAMP_POSITIONS:
                                        continue
                                    ch = board[row][col] if row < len(board) and col < len(board[row]) else "空"
                                    if ch == "空":
                                        stats._skipped_abnormal_files.append((jqh_file, f"{color}方位置({row},{col})为空"))
                                        skip = True
                                        break
                                if skip:
                                    break
                            if skip:
                                break
                        if skip:
                            continue
                        for color_idx, color in enumerate(ALL_COLORS):
                            board = boards.get(color)
                            if board is None:
                                continue
                            if all(ch == "空" for row in board for ch in row):
                                continue
                            username = usernames[color_idx] if color_idx < len(usernames) else ""
                            if whitelist and username not in whitelist:
                                continue
                            if blacklist and username in blacklist:
                                continue
                            stats.add_matrix(board, c2i)
                            matched.append((jqh_file, color, board))
                        stats._file_count += 1
                    except Exception as e:
                        stats._failed_files.append((jqh_file, str(e)))
                    if progress_callback:
                        progress_callback(i + 1, total)
                self._result = (stats, len(files), None, matched)
            except Exception as e:
                self._result = (None, 0, str(e), [])

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _poll_worker(self) -> None:
        if self._worker.is_alive():
            self.after(100, self._poll_worker)
            return
        stats, total_files, err, matched = self._result
        self.run_btn.config(state=tk.NORMAL)
        if err is not None:
            self.summary_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err, parent=self._top)
            return
        if total_files == 0:
            self.summary_var.set("未找到 .jqh 文件")
            messagebox.showinfo("提示", "该文件夹下未找到 .jqh 文件", parent=self._top)
            return
        self.stats = stats
        self.matched_layouts = matched
        self._refresh_grid()
        failed = len(stats.failed_files)
        # 计算去重后的复盘文件数
        unique_replay_files = len(set(fp for fp, _, _ in matched)) if matched else 0
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"统计布阵数: {stats.layout_count}    去重复盘数: {unique_replay_files}")
        self.summary_var.set(summary)
        self.export_btn.config(state=tk.NORMAL if matched else tk.DISABLED)
        self.export_replay_btn.config(state=tk.NORMAL if matched else tk.DISABLED)
        if failed:
            paths = [str(Path(f).name) for f, _ in stats.failed_files[:10]]
            detail = "\n".join(paths)
            if failed > 10:
                detail += f"\n……共 {failed} 个"
            messagebox.showwarning("部分文件解析失败", detail, parent=self._top)

    def _export_layouts(self) -> None:
        """导出筛选后的布阵文件"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布阵", parent=self._top)
            return
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
        if not save_dir:
            return
        try:
            is_jqs = self.export_format_var.get() == "联众(jqs)"
            JQS_HEADER = bytes([
                0x47, 0x4C, 0xE9, 0x03, 0xCE, 0x07, 0xC4, 0x02, 0x53, 0x61, 0x74, 0x20,
                0x41, 0x75, 0x67, 0x20, 0x30, 0x31, 0x20, 0x32, 0x32, 0x3A, 0x34, 0x35,
                0x3A, 0x30, 0x33, 0x20, 0x32, 0x30, 0x32, 0x36, 0x0A, 0x00, 0x00, 0x00,
                0x62, 0x79, 0x20, 0x5B, 0xD1, 0xD0, 0xBE, 0xBF, 0x31, 0x5D, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00
            ])
            JQS_LAYOUT_OFFSET = 0x370
            JQS_ROW_STRIDE = 17
            JQS_FILE_SIZE = 1264
            PIECE_TO_JQS = {
                ItemType.JQNone: 0x00, ItemType.Junzhang: 0x01, ItemType.Shizhang: 0x02,
                ItemType.Lvzhang: 0x03, ItemType.Tuanzhang: 0x04, ItemType.Yinzhang: 0x05,
                ItemType.Lianzhang: 0x06, ItemType.Paizhang: 0x07, ItemType.Gongbing: 0x08,
                ItemType.Junqi: 0x09, ItemType.Silin: 0x0A, ItemType.Dilei: 0x0B,
                ItemType.Zhadan: 0x0C,
            }
            JQL_HEADER = bytes([0x51, 0x51, 0x47, 0x61, 0x6D, 0x65, 0x20, 0x4A,
                                0x51, 0x4C, 0x20, 0x46, 0x69, 0x6C, 0x65, 0x00,
                                0x57, 0x04, 0x00, 0x00])
            JQL_LAYOUT_OFFSET = 0x14
            JQL_FILE_SIZE = 50
            PIECE_TO_JQL = {
                ItemType.JQNone: 0x00, ItemType.Junzhang: 0x01, ItemType.Shizhang: 0x02,
                ItemType.Lvzhang: 0x03, ItemType.Tuanzhang: 0x04, ItemType.Yinzhang: 0x05,
                ItemType.Lianzhang: 0x06, ItemType.Paizhang: 0x07, ItemType.Gongbing: 0x08,
                ItemType.Junqi: 0x09, ItemType.Silin: 0x0A, ItemType.Dilei: 0x0B,
                ItemType.Zhadan: 0x0C,
            }
            PIECE_TO_EXPORT = PIECE_TO_JQS if is_jqs else PIECE_TO_JQL

            # 按布阵内容去重
            seen_layouts = set()
            unique_layouts = []
            for file_path, color, layout_data in self.matched_layouts:
                key = self._layout_to_key(layout_data)
                if key not in seen_layouts:
                    seen_layouts.add(key)
                    unique_layouts.append((file_path, color, layout_data))

            total_count = len(unique_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported_count = 0
            for i, (file_path, color, layout_data) in enumerate(unique_layouts):
                original_name = Path(file_path).stem
                ext = ".jqs" if is_jqs else ".jql"
                new_filename = f"{i:0{prefix_digits}d}_{original_name}{ext}"
                output_path = os.path.join(save_dir, new_filename)
                def get_piece_type(row, col):
                    if isinstance(layout_data, JGSLayoutBlock):
                        return layout_data.item(row, col)
                    else:
                        from src.lianzhong import get_char_to_itemtype
                        c2i = get_char_to_itemtype()
                        return c2i.get(layout_data[row][col], ItemType.JQNone)
                if is_jqs:
                    data = bytearray(JQS_FILE_SIZE)
                    data[0:len(JQS_HEADER)] = JQS_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQS_LAYOUT_OFFSET + (row * JQS_ROW_STRIDE + col) * 4
                            pt = get_piece_type(row, col)
                            code = 0x00 if (row, col) in CAMP_POSITIONS else PIECE_TO_EXPORT.get(pt, 0x00)
                            struct.pack_into('<I', data, offset, code)
                else:
                    data = bytearray(JQL_FILE_SIZE)
                    data[0:20] = JQL_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQL_LAYOUT_OFFSET + row * 5 + col
                            pt = get_piece_type(row, col)
                            data[offset] = int(pt)
                with open(output_path, 'wb') as f:
                    f.write(data)
                exported_count += 1
                self.summary_var.set(f"导出中…… {exported_count}/{total_count}")
            messagebox.showinfo("完成", f"成功导出 {exported_count} 个布局文件到:\n{save_dir}", parent=self._top)
            self.summary_var.set(f"已导出 {exported_count} 个布局文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.summary_var.set(f"导出失败: {e}")

    def _export_replays(self) -> None:
        """导出筛选后的复盘文件（按文件去重，仅导出解析正常的文件）"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            # 按复盘文件去重
            seen_files = set()
            unique_files = []
            for file_path, color, layout_data in self.matched_layouts:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)

            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            import shutil
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                shutil.copy2(src_path, output_path)
                exported += 1
                self.summary_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
            self.summary_var.set(f"已导出 {exported} 个复盘文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.summary_var.set(f"导出失败: {e}")


class PieceFilterPanel(StatPanel):
    """棋子维度统计: 12选1, 只统计被选中棋子在各位置的出现频率

    同时支持 QQ(.jgs) 和联众(.jqh) 两种复盘来源, 由外部 source_var 控制"""

    PIECE_OPTIONS = [
        ("司", ItemType.Silin),
        ("军", ItemType.Junzhang),
        ("师", ItemType.Shizhang),
        ("旅", ItemType.Lvzhang),
        ("团", ItemType.Tuanzhang),
        ("营", ItemType.Yinzhang),
        ("连", ItemType.Lianzhang),
        ("排", ItemType.Paizhang),
        ("兵", ItemType.Gongbing),
        ("弹", ItemType.Zhadan),
        ("雷", ItemType.Dilei),
        ("旗", ItemType.Junqi),
    ]

    def __init__(self, parent, folder_var, export_name, mirror_getter=None,
                 mainline_getter=None, dedup_getter=None, display_mode_getter=None,
                 source_var=None):
        self._source_var = source_var
        super().__init__(parent, folder_var, export_name, mirror_getter, mainline_getter, dedup_getter, display_mode_getter)

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="棋子(单选):").pack(side=tk.LEFT)
        self.piece_var = tk.StringVar(value="司")
        for name, _ in self.PIECE_OPTIONS:
            ttk.Radiobutton(bar, text=name, value=name, variable=self.piece_var).pack(
                side=tk.LEFT, padx=(2, 0))
        ttk.Label(bar, text="  ").pack(side=tk.LEFT)

    def _get_filter(self):
        selected = self.piece_var.get()
        chosen = [(name, it) for name, it in self.PIECE_OPTIONS if name == selected]
        if not chosen:
            messagebox.showwarning("提示", "请至少勾选一个棋子", parent=self._top)
            return None
        piece_set = {it for _, it in chosen}
        desc = "棋子[" + "+".join(name for name, _ in chosen) + "]"
        return piece_set, None, desc

    def _start_stat(self) -> None:
        """根据来源模式(QQ/联众)选择不同的文件加载方式"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        flt = self._get_filter()
        if flt is None:
            return
        self._piece_filter, _, self._filter_desc = flt

        self.run_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.summary_var.set("统计中……")

        is_lianzhong = self._source_var is not None and self._source_var.get() == "联众"

        def progress_callback(done, total):
            self.summary_var.set(f"统计中…… {done}/{total}")

        dedup = self._dedup_getter()

        def worker():
            stats = LayoutStats(dedup=dedup)
            try:
                if is_lianzhong:
                    from src.lianzhong import get_char_to_itemtype
                    c2i = get_char_to_itemtype()
                    colors = {"红", "绿", "蓝", "黑"}
                    files = stats.add_jqh_folder(folder, colors, c2i, progress_callback)
                else:
                    files = stats.add_folder(folder, progress_callback=progress_callback)
                self._result = (stats, len(files), None)
            except Exception as e:
                self._result = (None, 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _refresh_grid(self) -> None:
        """刷新网格, 只显示被选中棋子的频率"""
        if self.stats is None:
            return
        piece_filter = getattr(self, '_piece_filter', None)
        if piece_filter is None:
            return
        is_mainline = self._mainline_getter()
        mirror = self._mirror_getter()
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                is_camp = (row, col) in CAMP_POSITIONS
                txt = self.cell_texts[row][col]
                txt.config(state=tk.NORMAL)
                txt.delete("1.0", tk.END)
                if is_camp:
                    txt.config(state=tk.DISABLED)
                    continue
                if is_mainline:
                    text = self.stats._cell_text_filtered_mainline(row, col, piece_filter)
                else:
                    data_col = JGSLayoutBlock.col_num - 1 - col if mirror else col
                    text = self.stats._cell_text_filtered(row, data_col, piece_filter)
                txt.insert("1.0", text)
                txt.config(state=tk.DISABLED)

    def _export_excel(self) -> None:
        if self.stats is None:
            return
        piece_filter = getattr(self, '_piece_filter', None)
        if piece_filter is None:
            return
        xlsx_file = filedialog.asksaveasfilename(
            title="导出 Excel", defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx")], initialfile=self._export_name,
            parent=self.winfo_toplevel())
        if not xlsx_file:
            return
        try:
            self.stats.save_excel_filtered(
                xlsx_file, piece_filter, mirror_cols=self._mirror_getter())
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            return
        messagebox.showinfo("完成", f"已导出: {xlsx_file}", parent=self._top)


class AdvancedFilterPanel(ttk.Frame):
    """高级筛选面板: 支持多种筛选方式"""

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar,
                 direction_getter=None, is_mainline_getter=None):
        super().__init__(parent, padding=4)
        self._folder_var = folder_var
        self._source_var = source_var
        self._direction_getter = direction_getter or (lambda: "逆时针")
        self._is_mainline_getter = is_mainline_getter or (lambda: False)
        self._build_ui()

    def _build_ui(self) -> None:
        # 子选项单选按钮
        self.sub_option_var = tk.StringVar(value="按组合条件筛选")
        sub_options = ["按组合条件筛选", "按布阵文件筛选"]

        option_frame = ttk.Frame(self)
        option_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(option_frame, text="筛选方式:").pack(side=tk.LEFT)
        for option in sub_options:
            ttk.Radiobutton(option_frame, text=option, value=option,
                           variable=self.sub_option_var,
                           command=self._on_sub_option_change).pack(side=tk.LEFT, padx=(10, 0))

        # 内容区域
        self.content_frame = ttk.Frame(self)
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # 初始显示第一个子选项
        self._show_combination_filter()

    def _on_sub_option_change(self) -> None:
        """切换子选项时更新界面"""
        # 清空内容区域
        for widget in self.content_frame.winfo_children():
            widget.destroy()

        option = self.sub_option_var.get()
        if option == "按组合条件筛选":
            self._show_combination_filter()
        elif option == "按布阵文件筛选":
            self._show_file_filter()

    def _show_combination_filter(self) -> None:
        """显示按组合条件筛选界面"""
        # 棋子类型定义
        PIECE_OPTIONS = [
            ("司", ItemType.Silin),
            ("军", ItemType.Junzhang),
            ("师", ItemType.Shizhang),
            ("旅", ItemType.Lvzhang),
            ("团", ItemType.Tuanzhang),
            ("营", ItemType.Yinzhang),
            ("连", ItemType.Lianzhang),
            ("排", ItemType.Paizhang),
            ("兵", ItemType.Gongbing),
            ("弹", ItemType.Zhadan),
            ("雷", ItemType.Dilei),
            ("旗", ItemType.Junqi),
        ]
        
        # 创建主框架
        main_frame = ttk.Frame(self.content_frame)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 添加统计和导出按钮（放在网格上方）
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(0, 5))
        
        self.stat_btn = ttk.Button(button_frame, text="统计", command=self._start_combination_stat)
        self.stat_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.export_layout_btn = ttk.Button(button_frame, text="导出布阵", command=self._export_layouts_dedup, state=tk.DISABLED)
        self.export_layout_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.export_replay_btn = ttk.Button(button_frame, text="导出复盘", command=self._export_replays_dedup, state=tk.DISABLED)
        self.export_replay_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_btn = ttk.Button(button_frame, text="清空所有勾选", command=self._clear_all_checkboxes)
        self.clear_btn.pack(side=tk.LEFT)

        # 导出格式选择
        self.export_format_var = tk.StringVar(value="联众(jqs)")
        for fmt in ("联众(jqs)", "QQ(jql)"):
            ttk.Radiobutton(button_frame, text=fmt, value=fmt, variable=self.export_format_var).pack(side=tk.LEFT, padx=(4, 0))

        # 结果显示区域
        self.result_var = tk.StringVar(value="请选择条件并点击统计")
        result_label = ttk.Label(button_frame, textvariable=self.result_var)
        result_label.pack(side=tk.LEFT, padx=(20, 0))
        
        # 创建网格框架
        grid_frame = ttk.Frame(main_frame)
        grid_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 存储所有复选框变量
        self.checkbox_vars = {}  # {(row, col, piece_name): tk.BooleanVar}
        
        # 创建6x5网格
        for row in range(6):
            for col in range(5):
                # 行营位置显示空白占位
                if (row, col) in CAMP_POSITIONS:
                    placeholder = ttk.Label(grid_frame, text="(行营)", anchor=tk.CENTER)
                    placeholder.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
                    continue
                
                # 创建单元格框架
                cell_frame = ttk.LabelFrame(grid_frame, text=f"({row+1},{col+1})")
                cell_frame.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
                
                # 4行3列排列棋子复选框
                for idx, (piece_name, piece_type) in enumerate(PIECE_OPTIONS):
                    r, c = divmod(idx, 3)  # 4行3列
                    var = tk.BooleanVar(value=False)
                    self.checkbox_vars[(row, col, piece_name)] = var
                    
                    # 检查是否应禁用
                    should_disable = False
                    if piece_name == "弹" and row == 0:
                        should_disable = True
                    if piece_name == "旗" and (row, col) not in [(5, 1), (5, 3)]:
                        should_disable = True
                    if piece_name == "雷" and row not in [4, 5]:
                        should_disable = True
                    
                    state = tk.DISABLED if should_disable else tk.NORMAL
                    cb = ttk.Checkbutton(cell_frame, text=piece_name, variable=var, state=state)
                    cb.grid(row=r, column=c, padx=2, pady=1, sticky=tk.W)
        
        # 配置网格权重，使单元格均匀分布
        for row in range(6):
            grid_frame.rowconfigure(row, weight=1)
        for col in range(5):
            grid_frame.columnconfigure(col, weight=1)
        
        # 存储匹配的布局数据
        self.matched_layouts = []
        self.total_layouts = 0

    def _start_combination_stat(self) -> None:
        """开始按组合条件筛选统计"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return
        
        # 收集筛选条件
        conditions = {}  # {(row, col): set(piece_type)}
        PIECE_NAME_TO_TYPE = {
            "司": ItemType.Silin,
            "军": ItemType.Junzhang,
            "师": ItemType.Shizhang,
            "旅": ItemType.Lvzhang,
            "团": ItemType.Tuanzhang,
            "营": ItemType.Yinzhang,
            "连": ItemType.Lianzhang,
            "排": ItemType.Paizhang,
            "兵": ItemType.Gongbing,
            "弹": ItemType.Zhadan,
            "雷": ItemType.Dilei,
            "旗": ItemType.Junqi,
        }
        
        for (row, col, piece_name), var in self.checkbox_vars.items():
            if var.get():
                if (row, col) not in conditions:
                    conditions[(row, col)] = set()
                conditions[(row, col)].add(PIECE_NAME_TO_TYPE[piece_name])
        
        if not conditions:
            messagebox.showwarning("提示", "请至少选择一个筛选条件", parent=self._top)
            return
        
        # 禁用按钮，显示进度
        self.stat_btn.config(state=tk.DISABLED)
        self.export_layout_btn.config(state=tk.DISABLED)
        self.export_replay_btn.config(state=tk.DISABLED)
        self.result_var.set("统计中……")

        is_mainline = self._is_mainline_getter()
        normal_mirror = self._direction_getter() == "顺时针" and not is_mainline

        # 后台线程统计
        def worker():
            try:
                is_lianzhong = self._source_var.get() == "联众"
                matched = []
                total = 0

                if is_lianzhong:
                    from src.lianzhong import decode_jqh_all_boards, get_char_to_itemtype
                    c2i = get_char_to_itemtype()
                    pattern = os.path.join(folder, "**", "*.jqh")
                    files = sorted(glob.glob(pattern, recursive=True))
                    total = len(files)

                    for i, jqh_file in enumerate(files):
                        try:
                            raw = open(jqh_file, "rb").read()
                            boards, _ = decode_jqh_all_boards(raw)
                            for color, board in boards.items():
                                if all(ch == "空" for row in board for ch in row):
                                    continue
                                if is_mainline:
                                    flag_col = self._find_flag_col_matrix(board, c2i)
                                    mirror_this = (flag_col == 3)
                                else:
                                    mirror_this = normal_mirror
                                if self._match_layout_matrix(board, conditions, c2i, mirror_this):
                                    matched.append((jqh_file, color, board))
                        except Exception:
                            pass
                        self.result_var.set(f"统计中…… {i+1}/{total}")
                else:
                    from src.layout_stats import find_jgs_files, load_total_info
                    files = find_jgs_files(folder)
                    total = len(files)

                    for i, jgs_file in enumerate(files):
                        try:
                            total_info = load_total_info(jgs_file)
                            for pib in total_info.player_info_block:
                                layout = pib.player_layout
                                if all(layout.item(r, c) == ItemType.JQNone
                                       for r in range(JGSLayoutBlock.row_num)
                                       for c in range(JGSLayoutBlock.col_num)):
                                    continue
                                if is_mainline:
                                    flag_col = self._find_flag_col_block(layout)
                                    mirror_this = (flag_col == 3)
                                else:
                                    mirror_this = normal_mirror
                                if self._match_layout_block(layout, conditions, mirror_this):
                                    matched.append((jgs_file, pib.player_color, layout))
                        except Exception:
                            pass
                        self.result_var.set(f"统计中…… {i+1}/{total}")

                self._stat_result = (matched, total, None)
            except Exception as e:
                self._stat_result = ([], 0, str(e))
        
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_stat_worker)

    @staticmethod
    def _find_flag_col_block(layout: JGSLayoutBlock) -> int:
        """查找QQ布局中军旗所在列, 默认返回1(左)"""
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                if layout.item(row, col) == ItemType.Junqi:
                    return col
        return 1

    @staticmethod
    def _find_flag_col_matrix(board, c2i: dict) -> int:
        """查找联众布局中军旗所在列, 默认返回1(左)"""
        for row in range(len(board)):
            for col in range(len(board[row])):
                if c2i.get(board[row][col]) == ItemType.Junqi:
                    return col
        return 1

    def _match_layout_block(self, layout: JGSLayoutBlock, conditions: dict, mirror: bool) -> bool:
        """检查QQ布阵是否匹配所有条件"""
        for (row, col), allowed_types in conditions.items():
            c = 4 - col if mirror else col
            piece = layout.item(row, c)
            if piece not in allowed_types:
                return False
        return True

    def _match_layout_matrix(self, board: list[list[str]], conditions: dict, c2i: dict, mirror: bool) -> bool:
        """检查联众布阵是否匹配所有条件"""
        for (row, col), allowed_types in conditions.items():
            c = 4 - col if mirror else col
            if row >= len(board) or c >= len(board[row]):
                return False
            piece_name = board[row][c]
            piece_type = c2i.get(piece_name, ItemType.JQNone)
            if piece_type not in allowed_types:
                return False
        return True

    def _poll_stat_worker(self) -> None:
        """轮询统计线程结果"""
        try:
            if self._worker.is_alive():
                self.after(100, self._poll_stat_worker)
                return

            matched, total, err = self._stat_result
            if hasattr(self, 'stat_btn') and self.stat_btn.winfo_exists():
                self.stat_btn.config(state=tk.NORMAL)

            if err is not None:
                self.result_var.set(f"统计失败: {err}")
                messagebox.showerror("错误", err, parent=self._top)
                return

            self.matched_layouts = matched
            self.total_layouts = total

            if total == 0:
                self.result_var.set("未找到布局文件")
                return

            match_count = len(matched)
            percentage = (match_count / total * 100) if total > 0 else 0
            self.result_var.set(f"匹配布局: {match_count}/{total} ({percentage:.1f}%)")
            has_match = match_count > 0
            if hasattr(self, 'export_layout_btn') and self.export_layout_btn.winfo_exists():
                self.export_layout_btn.config(state=tk.NORMAL if has_match else tk.DISABLED)
            if hasattr(self, 'export_replay_btn') and self.export_replay_btn.winfo_exists():
                self.export_replay_btn.config(state=tk.NORMAL if has_match else tk.DISABLED)
        except Exception:
            pass

    def _clear_all_checkboxes(self) -> None:
        """清空所有复选框"""
        try:
            for var in self.checkbox_vars.values():
                var.set(False)
            if hasattr(self, 'result_var'):
                self.result_var.set("请选择条件并点击统计")
            if hasattr(self, 'export_layout_btn') and self.export_layout_btn.winfo_exists():
                self.export_layout_btn.config(state=tk.DISABLED)
            if hasattr(self, 'export_replay_btn') and self.export_replay_btn.winfo_exists():
                self.export_replay_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def _get_export_constants(self):
        """获取导出格式常量"""
        JQS_HEADER = bytes([
            0x47, 0x4C, 0xE9, 0x03, 0xCE, 0x07, 0xC4, 0x02, 0x53, 0x61, 0x74, 0x20,
            0x41, 0x75, 0x67, 0x20, 0x30, 0x31, 0x20, 0x32, 0x32, 0x3A, 0x34, 0x35,
            0x3A, 0x30, 0x33, 0x20, 0x32, 0x30, 0x32, 0x36, 0x0A, 0x00, 0x00, 0x00,
            0x62, 0x79, 0x20, 0x5B, 0xD1, 0xD0, 0xBE, 0xBF, 0x31, 0x5D, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00
        ])
        PIECE_TO_JQS = {
            ItemType.JQNone: 0x00, ItemType.Junzhang: 0x01, ItemType.Shizhang: 0x02,
            ItemType.Lvzhang: 0x03, ItemType.Tuanzhang: 0x04, ItemType.Yinzhang: 0x05,
            ItemType.Lianzhang: 0x06, ItemType.Paizhang: 0x07, ItemType.Gongbing: 0x08,
            ItemType.Junqi: 0x09, ItemType.Silin: 0x0A, ItemType.Dilei: 0x0B,
            ItemType.Zhadan: 0x0C,
        }
        JQL_HEADER = bytes([0x51, 0x51, 0x47, 0x61, 0x6D, 0x65, 0x20, 0x4A,
                            0x51, 0x4C, 0x20, 0x46, 0x69, 0x6C, 0x65, 0x00,
                            0x57, 0x04, 0x00, 0x00])
        return JQS_HEADER, PIECE_TO_JQS, JQL_HEADER

    def _write_layout_file(self, layout_data, output_path, is_jqs, JQS_HEADER, PIECE_TO_JQS, JQL_HEADER):
        """将布阵数据写入文件"""
        def get_piece_type(row, col):
            if isinstance(layout_data, JGSLayoutBlock):
                return layout_data.item(row, col)
            else:
                from src.lianzhong import get_char_to_itemtype
                c2i = get_char_to_itemtype()
                return c2i.get(layout_data[row][col], ItemType.JQNone)

        if is_jqs:
            JQS_LAYOUT_OFFSET = 0x370
            JQS_ROW_STRIDE = 17
            JQS_FILE_SIZE = 1264
            data = bytearray(JQS_FILE_SIZE)
            data[0:len(JQS_HEADER)] = JQS_HEADER
            for row in range(6):
                for col in range(5):
                    offset = JQS_LAYOUT_OFFSET + (row * JQS_ROW_STRIDE + col) * 4
                    pt = get_piece_type(row, col)
                    code = 0x00 if (row, col) in CAMP_POSITIONS else PIECE_TO_JQS.get(pt, 0x00)
                    struct.pack_into('<I', data, offset, code)
        else:
            JQL_LAYOUT_OFFSET = 0x14
            JQL_FILE_SIZE = 50
            data = bytearray(JQL_FILE_SIZE)
            data[0:20] = JQL_HEADER
            for row in range(6):
                for col in range(5):
                    offset = JQL_LAYOUT_OFFSET + row * 5 + col
                    pt = get_piece_type(row, col)
                    data[offset] = int(pt)

        with open(output_path, 'wb') as f:
            f.write(data)

    def _layout_to_key(self, layout_data) -> tuple:
        """将布阵转换为可哈希的元组用于去重"""
        if isinstance(layout_data, JGSLayoutBlock):
            return tuple(layout_data.item(r, c) for r in range(6) for c in range(5))
        else:
            return tuple(layout_data[r][c] for r in range(6) for c in range(5))

    def _export_layouts_dedup(self) -> None:
        """导出布阵（按布阵内容去重）"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布阵", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            is_jqs = self.export_format_var.get() == "联众(jqs)"
            JQS_HEADER, PIECE_TO_JQS, JQL_HEADER = self._get_export_constants()
            ext = ".jqs" if is_jqs else ".jql"

            # 按布阵内容去重
            seen_layouts = set()
            unique_layouts = []
            for file_path, color, layout_data in self.matched_layouts:
                key = self._layout_to_key(layout_data)
                if key not in seen_layouts:
                    seen_layouts.add(key)
                    unique_layouts.append((file_path, color, layout_data))

            total_count = len(unique_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            for i, (file_path, color, layout_data) in enumerate(unique_layouts):
                original_name = Path(file_path).stem
                new_filename = f"{i:0{prefix_digits}d}_{original_name}{ext}"
                # 避免同名文件
                counter = 1
                base_name = new_filename
                while new_filename in used_names:
                    name_part = Path(base_name).stem
                    new_filename = f"{name_part}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                self._write_layout_file(layout_data, output_path, is_jqs, JQS_HEADER, PIECE_TO_JQS, JQL_HEADER)
                exported += 1
                self.result_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个布阵文件(去重后)到:\n{save_dir}", parent=self._top)
            self.result_var.set(f"已导出 {exported} 个布阵文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.result_var.set(f"导出失败: {e}")

    def _export_replays_dedup(self) -> None:
        """导出复盘文件（按复盘文件去重）"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            # 按复盘文件去重
            seen_files = set()
            unique_files = []
            for file_path, color, layout_data in self.matched_layouts:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)

            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                # 避免同名文件
                counter = 1
                base_name = new_filename
                while new_filename in used_names:
                    name_part = Path(base_name).stem
                    new_filename = f"{name_part}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                import shutil
                shutil.copy2(src_path, output_path)
                exported += 1
                self.result_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
            self.result_var.set(f"已导出 {exported} 个复盘文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
            self.result_var.set(f"导出失败: {e}")

    def _show_file_filter(self) -> None:
        """显示按布阵文件筛选界面"""
        main_frame = ttk.Frame(self.content_frame)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 第一行: 加载布阵文件 + 状态
        row1 = ttk.Frame(main_frame)
        row1.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(row1, text="加载布阵文件", command=self._load_layout_file).pack(side=tk.LEFT)
        self.file_filter_result_var = tk.StringVar(value="请加载 .jqs 或 .jql 布阵文件")
        ttk.Label(row1, textvariable=self.file_filter_result_var).pack(side=tk.LEFT, padx=(10, 0))

        # 第二行: 统计 + 导出 + 结果
        row2 = ttk.Frame(main_frame)
        row2.pack(fill=tk.X, pady=(0, 5))
        self.file_stat_btn = ttk.Button(row2, text="统计匹配", command=self._start_file_filter_stat)
        self.file_stat_btn.pack(side=tk.LEFT)
        self.file_export_layout_btn = ttk.Button(row2, text="导出布阵", command=self._export_file_filter_layouts, state=tk.DISABLED)
        self.file_export_layout_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.file_export_replay_btn = ttk.Button(row2, text="导出复盘", command=self._export_file_filter_replays, state=tk.DISABLED)
        self.file_export_replay_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.file_filter_match_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.file_filter_match_var).pack(side=tk.LEFT, padx=(15, 0))

        # 布阵显示网格
        self.file_layout_grid = ttk.Frame(main_frame)
        self.file_layout_grid.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.file_layout_labels = []
        for row in range(6):
            row_labels = []
            for col in range(5):
                lbl = tk.Label(self.file_layout_grid, text="", bg="#FFFFFF",
                               relief=tk.RIDGE, bd=1, font=("Consolas", 10),
                               width=8, height=2)
                lbl.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
                row_labels.append(lbl)
            self.file_layout_labels.append(row_labels)
        for col in range(5):
            self.file_layout_grid.columnconfigure(col, weight=1)

        self._loaded_layout = None
        self._loaded_layout_source = ""
        self._file_filter_matched = []

    def _load_layout_file(self) -> None:
        """加载布阵文件 (.jqs 或 .jql)"""
        file_path = filedialog.askopenfilename(
            title="选择布阵文件",
            filetypes=[("布阵文件", "*.jqs *.jql"), ("联众布阵", "*.jqs"), ("QQ布阵", "*.jql")],
            parent=self.winfo_toplevel())
        if not file_path:
            return

        try:
            ext = Path(file_path).suffix.lower()
            raw = open(file_path, "rb").read()

            if ext == ".jqs":
                from src.lianzhong import parse_jqs_layout, JQS_CODE_MAP
                board_str = parse_jqs_layout(raw)
                # 转换为 ItemType 矩阵
                from src.lianzhong import get_char_to_itemtype
                c2i = get_char_to_itemtype()
                self._loaded_layout = [[c2i.get(ch, ItemType.JQNone) for ch in row] for row in board_str]
                self._loaded_layout_source = "联众"
            elif ext == ".jql":
                JQL_LAYOUT_OFFSET = 0x14
                layout = []
                for row in range(6):
                    row_items = []
                    for col in range(5):
                        offset = JQL_LAYOUT_OFFSET + row * 5 + col
                        v = raw[offset] if offset < len(raw) else 0
                        try:
                            row_items.append(ItemType(v))
                        except ValueError:
                            row_items.append(ItemType.JQNone)
                    layout.append(row_items)
                self._loaded_layout = layout
                self._loaded_layout_source = "QQ"
            else:
                messagebox.showerror("错误", f"不支持的文件格式: {ext}", parent=self._top)
                return

            # 显示布阵
            self._display_loaded_layout()
            self.file_filter_result_var.set(
                f"已加载 {Path(file_path).name} ({self._loaded_layout_source}格式)")
            self.file_filter_match_var.set("")
            self._file_filter_matched = []
            self.file_export_layout_btn.config(state=tk.DISABLED)
            self.file_export_replay_btn.config(state=tk.DISABLED)

        except Exception as e:
            messagebox.showerror("加载失败", str(e), parent=self._top)

    def _display_loaded_layout(self) -> None:
        """在网格中显示已加载的布阵"""
        from src.jgs_types import item_type2string
        CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}
        for row in range(6):
            for col in range(5):
                lbl = self.file_layout_labels[row][col]
                if (row, col) in CAMP_POSITIONS:
                    lbl.config(text="", bg="#D0D0D0")
                elif self._loaded_layout:
                    pt = self._loaded_layout[row][col]
                    lbl.config(text=item_type2string(pt), bg="#FFFFFF")
                else:
                    lbl.config(text="", bg="#FFFFFF")

    def _start_file_filter_stat(self) -> None:
        """开始按布阵文件筛选统计"""
        if self._loaded_layout is None:
            messagebox.showwarning("提示", "请先加载布阵文件", parent=self._top)
            return

        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        self.file_stat_btn.config(state=tk.DISABLED)
        self.file_export_layout_btn.config(state=tk.DISABLED)
        self.file_export_replay_btn.config(state=tk.DISABLED)
        self.file_filter_match_var.set("统计中……")

        target_layout = self._loaded_layout
        progress = [0]  # [processed]
        total_ref = [0]  # [total]

        def update_progress():
            self.file_filter_match_var.set(f"统计中…… {progress[0]}/{total_ref[0]}")

        def worker():
            try:
                matched = []

                # 搜索 .jgs 文件
                from src.layout_stats import find_jgs_files, load_total_info, _layout_is_empty
                from src.layout_stats import CAMP_POSITIONS as CP
                jgs_files = find_jgs_files(folder)
                total_ref[0] += len(jgs_files)
                for jgs_file in jgs_files:
                    try:
                        total_info = load_total_info(jgs_file)
                        for pib in total_info.player_info_block:
                            layout = pib.player_layout
                            if _layout_is_empty(layout):
                                continue
                            if self._match_layout_vs_target(layout, target_layout, CP):
                                matched.append((jgs_file, "QQ", layout))
                    except Exception:
                        pass
                    progress[0] += 1

                # 搜索 .jqh 文件
                from src.lianzhong import decode_jqh_all_boards, get_char_to_itemtype
                c2i = get_char_to_itemtype()
                pattern = os.path.join(folder, "**", "*.jqh")
                jqh_files = sorted(glob.glob(pattern, recursive=True))
                total_ref[0] += len(jqh_files)
                for jqh_file in jqh_files:
                    try:
                        raw = open(jqh_file, "rb").read()
                        boards, _ = decode_jqh_all_boards(raw)
                        for color, board in boards.items():
                            if all(ch == "空" for row in board for ch in row):
                                continue
                            board_types = [[c2i.get(ch, ItemType.JQNone) for ch in row] for row in board]
                            if self._match_layout_vs_target_matrix(board_types, target_layout, CP):
                                matched.append((jqh_file, color, board_types))
                    except Exception:
                        pass
                    progress[0] += 1

                self._file_filter_result = (matched, total_ref[0], None)
            except Exception as e:
                self._file_filter_result = ([], 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._progress_timer = self.after(200, self._update_file_filter_progress, progress, total_ref)
        self.after(100, self._poll_file_filter_worker)

    def _update_file_filter_progress(self, progress, total_ref):
        """更新进度显示"""
        if hasattr(self, '_worker') and self._worker.is_alive():
            self.file_filter_match_var.set(f"统计中…… {progress[0]}/{total_ref[0]}")
            self._progress_timer = self.after(200, self._update_file_filter_progress, progress, total_ref)

    def _match_layout_vs_target(self, layout, target, camp_positions) -> bool:
        """检查QQ布阵是否与目标布阵完全匹配"""
        for row in range(6):
            for col in range(5):
                if (row, col) in camp_positions:
                    continue
                if layout.item(row, col) != target[row][col]:
                    return False
        return True

    def _match_layout_vs_target_matrix(self, board_types, target, camp_positions) -> bool:
        """检查联众布阵是否与目标布阵完全匹配"""
        for row in range(6):
            for col in range(5):
                if (row, col) in camp_positions:
                    continue
                if board_types[row][col] != target[row][col]:
                    return False
        return True

    def _poll_file_filter_worker(self) -> None:
        try:
            if self._worker.is_alive():
                self.after(100, self._poll_file_filter_worker)
                return

            matched, total, err = self._file_filter_result
            if hasattr(self, 'file_stat_btn') and self.file_stat_btn.winfo_exists():
                self.file_stat_btn.config(state=tk.NORMAL)

            if err:
                self.file_filter_match_var.set(f"统计失败: {err}")
                messagebox.showerror("错误", err, parent=self._top)
                return

            self._file_filter_matched = matched
            match_count = len(matched)
            self.file_filter_match_var.set(f"匹配: {match_count}/{total} 文件")
            has_match = match_count > 0
            if hasattr(self, 'file_export_layout_btn') and self.file_export_layout_btn.winfo_exists():
                self.file_export_layout_btn.config(state=tk.NORMAL if has_match else tk.DISABLED)
            if hasattr(self, 'file_export_replay_btn') and self.file_export_replay_btn.winfo_exists():
                self.file_export_replay_btn.config(state=tk.NORMAL if has_match else tk.DISABLED)
        except Exception:
            pass

    def _export_file_filter_layouts(self) -> None:
        """导出按布阵文件筛选的布阵（按布阵内容去重）"""
        if not self._file_filter_matched:
            messagebox.showwarning("提示", "没有可导出的布阵", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return
    
        try:
            # 按布阵内容去重
            seen = set()
            unique = []
            for file_path, source, layout_data in self._file_filter_matched:
                key = tuple(layout_data[r][c] for r in range(6) for c in range(5))
                if key not in seen:
                    seen.add(key)
                    unique.append((file_path, source, layout_data))
    
            total_count = len(unique)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
    
            exported = 0
            used_names = set()
            for i, (file_path, source, layout_data) in enumerate(unique):
                original_name = Path(file_path).stem
                new_filename = f"{i:0{prefix_digits}d}_{original_name}.txt"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}.txt"
                    counter += 1
                used_names.add(new_filename)
    
                output_path = os.path.join(save_dir, new_filename)
                from src.jgs_types import item_type2string
                lines = [f"来源: {source}"]
                for row in range(6):
                    line = " ".join(item_type2string(layout_data[row][col]) for col in range(5))
                    lines.append(line)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                exported += 1
    
            messagebox.showinfo("完成", f"成功导出 {exported} 个布阵文件(去重后)到:\n{save_dir}", parent=self._top)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)
    
    def _export_file_filter_replays(self) -> None:
        """导出按布阵文件筛选的复盘文件（按文件去重）"""
        if not self._file_filter_matched:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return
    
        try:
            seen_files = set()
            unique_files = []
            for file_path, source, layout_data in self._file_filter_matched:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)
    
            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
    
            exported = 0
            used_names = set()
            import shutil
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)
    
                output_path = os.path.join(save_dir, new_filename)
                shutil.copy2(src_path, output_path)
                exported += 1
    
            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)

    def clear_stats(self) -> None:
        """清空统计结果"""
        self.matched_layouts = []
        self.total_layouts = 0
        try:
            if hasattr(self, 'result_var'):
                self.result_var.set("请选择条件并点击统计")
            if hasattr(self, 'export_btn') and self.export_btn.winfo_exists():
                self.export_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def _refresh_grid(self) -> None:
        """方向变化时清空结果"""
        self.clear_stats()


class LayoutFilterPanel(BasePanel):
    """按布阵文件筛选面板: 加载 .jqs/.jql 布阵文件，直接比对复盘布阵

    不做顺逆时针旋转，不做旗侧视角镜像，直接逐格比对。
    同时支持 QQ(.jgs) 和联众(.jqh) 两种复盘来源。
    """

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar):
        super().__init__(parent, padding=2)
        self._folder_var = folder_var
        self._source_var = source_var
        self._loaded_layout = None
        self._loaded_layout_source = ""
        self._file_filter_matched = []
        self._build_ui()

    def _build_ui(self) -> None:
        # 第一行: 加载布阵文件 + 状态
        row1 = ttk.Frame(self)
        row1.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(row1, text="加载布阵文件", command=self._load_layout_file).pack(side=tk.LEFT)
        self.file_filter_result_var = tk.StringVar(value="请加载 .jqs 或 .jql 布阵文件")
        ttk.Label(row1, textvariable=self.file_filter_result_var).pack(side=tk.LEFT, padx=(10, 0))

        # 第二行: 统计 + 导出复盘 + 结果
        row2 = ttk.Frame(self)
        row2.pack(fill=tk.X, pady=(0, 5))
        self.file_stat_btn = ttk.Button(row2, text="统计匹配", command=self._start_stat)
        self.file_stat_btn.pack(side=tk.LEFT)
        self.export_replay_btn = ttk.Button(row2, text="导出复盘", command=self._export_replays, state=tk.DISABLED)
        self.export_replay_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.file_filter_match_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.file_filter_match_var).pack(side=tk.LEFT, padx=(15, 0))

        # 布阵显示网格
        self.file_layout_grid = ttk.Frame(self)
        self.file_layout_grid.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.file_layout_labels = []
        for row in range(6):
            row_labels = []
            for col in range(5):
                lbl = tk.Label(self.file_layout_grid, text="", bg="#FFFFFF",
                               relief=tk.RIDGE, bd=1, font=("Consolas", 10),
                               width=8, height=2)
                lbl.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
                row_labels.append(lbl)
            self.file_layout_labels.append(row_labels)
        for col in range(5):
            self.file_layout_grid.columnconfigure(col, weight=1)

    def _load_layout_file(self) -> None:
        """加载布阵文件 (.jqs 或 .jql)"""
        top = self.winfo_toplevel()
        geo = top.geometry()
        file_path = filedialog.askopenfilename(
            title="选择布阵文件",
            filetypes=[("布阵文件", "*.jqs *.jql"), ("联众布阵", "*.jqs"), ("QQ布阵", "*.jql")],
            parent=top)
        top.geometry(geo)
        if not file_path:
            return

        try:
            ext = Path(file_path).suffix.lower()
            raw = open(file_path, "rb").read()

            if ext == ".jqs":
                from src.lianzhong import parse_jqs_layout
                from src.lianzhong import get_char_to_itemtype
                c2i = get_char_to_itemtype()
                board_str = parse_jqs_layout(raw)
                self._loaded_layout = [[c2i.get(ch, ItemType.JQNone) for ch in row] for row in board_str]
                self._loaded_layout_source = "联众"
            elif ext == ".jql":
                JQL_LAYOUT_OFFSET = 0x14
                layout = []
                for row in range(6):
                    row_items = []
                    for col in range(5):
                        offset = JQL_LAYOUT_OFFSET + row * 5 + col
                        v = raw[offset] if offset < len(raw) else 0
                        try:
                            row_items.append(ItemType(v))
                        except ValueError:
                            row_items.append(ItemType.JQNone)
                    layout.append(row_items)
                self._loaded_layout = layout
                self._loaded_layout_source = "QQ"
            else:
                messagebox.showerror("错误", f"不支持的文件格式: {ext}", parent=self._top)
                return

            # 显示布阵
            self._display_loaded_layout()
            self.file_filter_result_var.set(
                f"已加载 {Path(file_path).name} ({self._loaded_layout_source}格式)")
            self.file_filter_match_var.set("")
            self._file_filter_matched = []
            self.export_replay_btn.config(state=tk.DISABLED)

        except Exception as e:
            messagebox.showerror("加载失败", str(e), parent=self._top)

    def _display_loaded_layout(self) -> None:
        """在网格中显示已加载的布阵"""
        from src.jgs_types import item_type2string
        for row in range(6):
            for col in range(5):
                lbl = self.file_layout_labels[row][col]
                if (row, col) in self.CAMP_POSITIONS:
                    lbl.config(text="", bg="#D0D0D0")
                elif self._loaded_layout:
                    pt = self._loaded_layout[row][col]
                    lbl.config(text=item_type2string(pt), bg="#FFFFFF")
                else:
                    lbl.config(text="", bg="#FFFFFF")

    def _match_layout_vs_target(self, layout, target) -> bool:
        """检查QQ布阵是否与目标布阵完全匹配 (不做镜像)"""
        for row in range(6):
            for col in range(5):
                if (row, col) in self.CAMP_POSITIONS:
                    continue
                if layout.item(row, col) != target[row][col]:
                    return False
        return True

    def _match_layout_vs_target_matrix(self, board_types, target) -> bool:
        """检查联众布阵是否与目标布阵完全匹配 (不做镜像)"""
        for row in range(6):
            for col in range(5):
                if (row, col) in self.CAMP_POSITIONS:
                    continue
                if board_types[row][col] != target[row][col]:
                    return False
        return True

    def _start_stat(self) -> None:
        """开始按布阵文件筛选统计"""
        if self._loaded_layout is None:
            messagebox.showwarning("提示", "请先加载布阵文件", parent=self._top)
            return

        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        self.file_stat_btn.config(state=tk.DISABLED)
        self.export_replay_btn.config(state=tk.DISABLED)
        self.file_filter_match_var.set("统计中……")

        target_layout = self._loaded_layout
        progress = [0]
        total_ref = [0]

        def worker():
            try:
                matched = []

                # 搜索 .jgs 文件
                from src.layout_stats import find_jgs_files, load_total_info, _layout_is_empty
                jgs_files = find_jgs_files(folder)
                total_ref[0] += len(jgs_files)
                for jgs_file in jgs_files:
                    try:
                        total_info = load_total_info(jgs_file)
                        # 检查是否有非法棋子
                        skip = False
                        for pib in total_info.player_info_block:
                            err = self._layout_has_invalid_piece(pib.player_layout)
                            if err:
                                skip = True
                                break
                        if skip:
                            continue
                        for pib in total_info.player_info_block:
                            layout = pib.player_layout
                            if _layout_is_empty(layout):
                                continue
                            if self._match_layout_vs_target(layout, target_layout):
                                matched.append((jgs_file, "QQ", layout))
                    except Exception:
                        pass
                    progress[0] += 1

                # 搜索 .jqh 文件
                from src.lianzhong import decode_jqh_all_boards, get_char_to_itemtype
                c2i = get_char_to_itemtype()
                pattern = os.path.join(folder, "**", "*.jqh")
                jqh_files = sorted(glob.glob(pattern, recursive=True))
                total_ref[0] += len(jqh_files)
                for jqh_file in jqh_files:
                    try:
                        raw = open(jqh_file, "rb").read()
                        boards, _ = decode_jqh_all_boards(raw)
                        # 检查是否有非法棋子
                        skip = False
                        for color, board in boards.items():
                            if all(ch == "空" for row in board for ch in row):
                                continue
                            for row in range(6):
                                for col in range(5):
                                    if (row, col) in self.CAMP_POSITIONS:
                                        continue
                                    ch = board[row][col] if row < len(board) and col < len(board[row]) else "空"
                                    if ch == "空":
                                        skip = True
                                        break
                                if skip:
                                    break
                            if skip:
                                break
                        if skip:
                            continue
                        for color, board in boards.items():
                            if all(ch == "空" for row in board for ch in row):
                                continue
                            board_types = [[c2i.get(ch, ItemType.JQNone) for ch in row] for row in board]
                            if self._match_layout_vs_target_matrix(board_types, target_layout):
                                matched.append((jqh_file, color, board_types))
                    except Exception:
                        pass
                    progress[0] += 1

                self._file_filter_result = (matched, total_ref[0], None)
            except Exception as e:
                self._file_filter_result = ([], 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._progress_timer = self.after(200, self._update_progress, progress, total_ref)
        self.after(100, self._poll_worker)

    def _update_progress(self, progress, total_ref):
        """更新进度显示"""
        if hasattr(self, '_worker') and self._worker.is_alive():
            self.file_filter_match_var.set(f"统计中…… {progress[0]}/{total_ref[0]}")
            self._progress_timer = self.after(200, self._update_progress, progress, total_ref)

    def _poll_worker(self) -> None:
        try:
            if self._worker.is_alive():
                self.after(100, self._poll_worker)
                return

            matched, total, err = self._file_filter_result
            if hasattr(self, 'file_stat_btn') and self.file_stat_btn.winfo_exists():
                self.file_stat_btn.config(state=tk.NORMAL)

            if err:
                self.file_filter_match_var.set(f"统计失败: {err}")
                messagebox.showerror("错误", err, parent=self._top)
                return

            self._file_filter_matched = matched
            match_count = len(matched)
            unique_files = len(set(fp for fp, _, _ in matched)) if matched else 0
            self.file_filter_match_var.set(f"匹配布阵: {match_count}  去重复盘: {unique_files}/{total}")
            has_match = match_count > 0
            if hasattr(self, 'export_replay_btn') and self.export_replay_btn.winfo_exists():
                self.export_replay_btn.config(state=tk.NORMAL if has_match else tk.DISABLED)
        except Exception:
            pass

    def _export_replays(self) -> None:
        """导出匹配的复盘文件（按文件去重）"""
        if not self._file_filter_matched:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            seen_files = set()
            unique_files = []
            for file_path, source, layout_data in self._file_filter_matched:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)

            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            import shutil
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                shutil.copy2(src_path, output_path)
                exported += 1
                self.file_filter_match_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
            self.file_filter_match_var.set(f"已导出 {exported} 个复盘文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)

    def clear_stats(self) -> None:
        """清空统计结果"""
        self._file_filter_matched = []
        if hasattr(self, 'file_filter_match_var'):
            self.file_filter_match_var.set("")
        if hasattr(self, 'export_replay_btn') and self.export_replay_btn.winfo_exists():
            self.export_replay_btn.config(state=tk.DISABLED)

    def _refresh_grid(self) -> None:
        """方向变化时清空结果"""
        self.clear_stats()


class ConditionalProbabilityPanel(BasePanel):
    """条件概率筛选面板: 左侧拖拽面板 + 中间条件面板 + 右侧结果面板"""

    PIECE_OPTIONS = [
        ("司", ItemType.Silin),
        ("军", ItemType.Junzhang),
        ("师", ItemType.Shizhang),
        ("旅", ItemType.Lvzhang),
        ("团", ItemType.Tuanzhang),
        ("营", ItemType.Yinzhang),
        ("连", ItemType.Lianzhang),
        ("排", ItemType.Paizhang),
        ("兵", ItemType.Gongbing),
        ("弹", ItemType.Zhadan),
        ("雷", ItemType.Dilei),
        ("旗", ItemType.Junqi),
    ]

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar,
                 display_mode_getter=None, direction_getter=None, is_mainline_getter=None):
        super().__init__(parent, padding=2)
        self._folder_var = folder_var
        self._source_var = source_var
        self._display_mode_getter = display_mode_getter or (lambda: "百分比")
        self._direction_getter = direction_getter or (lambda: "逆时针")
        self._is_mainline_getter = is_mainline_getter or (lambda: False)
        self._selected_piece = None
        self._stored_result = None  # 存储统计结果用于刷新显示
        self._build_ui()

    @staticmethod
    def _rank_color(rank: int) -> str:
        """根据排名返回颜色: top1=红(255,0,0), top2=(127,0,0), top3=(63,0,0), 其它=黑"""
        colors = {1: "#FF0000", 2: "#7F0000", 3: "#3F0000"}
        return colors.get(rank, "#000000")

    def _can_place(self, row: int, piece_name: str) -> bool:
        """检查棋子是否可以放在指定行"""
        if piece_name == "弹" and row == 0:
            return False
        if piece_name == "雷" and row not in [4, 5]:
            return False
        if piece_name == "旗" and row not in [4, 5]:
            return False
        return True

    def _build_ui(self) -> None:
        # 按钮栏
        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, pady=(0, 5))

        self.stat_btn = ttk.Button(button_frame, text="统计", command=self._start_stat)
        self.stat_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_btn = ttk.Button(button_frame, text="清空所有", command=self._clear_all)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.export_replay_btn = ttk.Button(button_frame, text="导出复盘", command=self._export_replays, state=tk.DISABLED)
        self.export_replay_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.export_layout_btn = ttk.Button(button_frame, text="导出布阵", command=self._export_layouts, state=tk.DISABLED)
        self.export_layout_btn.pack(side=tk.LEFT, padx=(0, 5))

        # 导出格式选择
        self.export_format_var = tk.StringVar(value="联众(jqs)")
        for fmt in ("联众(jqs)", "QQ(jql)"):
            ttk.Radiobutton(button_frame, text=fmt, value=fmt,
                            variable=self.export_format_var).pack(side=tk.LEFT, padx=(4, 0))

        self.result_var = tk.StringVar(value="点击棋子后，左键单击放置在指定位置，右键单击取消")
        ttk.Label(button_frame, textvariable=self.result_var).pack(side=tk.LEFT, padx=(20, 0))

        # 子类可覆写的高度变量
        self._cond_cell_height = 90  # 条件面板单元格高度(像素)
        self._result_cell_height = 6  # 结果面板单元格高度(字符行数)

        # 主容器: 左侧(棋子+条件) + 右侧(结果)
        main_frame = ttk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        main_frame.columnconfigure(0, weight=0)  # 左侧固定宽度
        main_frame.columnconfigure(1, weight=0)  # 结果面板固定宽度
        main_frame.rowconfigure(0, weight=1)

        # ===== 左侧: 棋子 + 条件面板垂直排列 =====
        left_frame = ttk.Frame(main_frame)
        left_frame.grid(row=0, column=0, padx=(0, 5), sticky="nsew")
        left_frame.rowconfigure(0, weight=0)  # 棋子栏不拉伸
        left_frame.rowconfigure(1, weight=1)  # 条件面板拉伸
        left_frame.columnconfigure(0, weight=1)

        # ----- 棋子栏 (2x6) -----
        drag_frame = ttk.LabelFrame(left_frame, text="棋子")
        drag_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 5))

        self._drag_labels = {}  # {piece_name: tk.Label}
        for idx, (piece_name, piece_type) in enumerate(self.PIECE_OPTIONS):
            r, c = divmod(idx, 6)  # 2行6列
            lbl = tk.Label(drag_frame, text=piece_name, width=4, height=1,
                          font=("Consolas", 10, "bold"), relief=tk.RAISED, bd=1,
                          bg="#E8E8E8", cursor="hand2")
            lbl.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            lbl.bind("<Button-1>", lambda e, pn=piece_name: self._on_drag_click(pn))
            self._drag_labels[piece_name] = lbl

        for c in range(6):
            drag_frame.columnconfigure(c, weight=1)

        # ----- 条件面板 (6x5) -----
        cond_frame = ttk.LabelFrame(left_frame, text="条件面板(放置棋子=条件, 空格子=结果)")
        cond_frame.grid(row=1, column=0, sticky="nsew")

        self.cell_pieces = {}  # {(row,col): set(piece_name)}
        self.cell_labels = {}  # {(row,col): {piece_name: tk.Label}}
        self.cond_cells = {}   # {(row,col): tk.Frame}

        for row in range(6):
            for col in range(5):
                if self._is_camp(row, col):
                    self._create_camp_label(cond_frame, row, col)
                    continue

                cell = tk.Frame(cond_frame, relief=tk.GROOVE, bd=1, bg="#FFFFFF",
                               width=81, height=self._cond_cell_height)
                cell.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
                cell.grid_propagate(False)
                self.cond_cells[(row, col)] = cell
                self.cell_pieces[(row, col)] = set()
                self.cell_labels[(row, col)] = {}

                cell.bind("<Button-1>", lambda e, r=row, c=col: self._on_cell_click(r, c))
                cell.bind("<Button-3>", lambda e, r=row, c=col: self._on_cell_right_click(r, c))

                # 统一4行3列布局
                for pr in range(4):
                    for pc in range(3):
                        piece_idx = pr * 3 + pc
                        if piece_idx < len(self.PIECE_OPTIONS):
                            pn = self.PIECE_OPTIONS[piece_idx][0]
                            if not self._can_place(row, pn):
                                continue
                            plbl = tk.Label(cell, text="", width=3, height=1,
                                           font=("Consolas", 10), bg="#FFFFFF")
                            plbl.grid(row=pr, column=pc, padx=0, pady=0, sticky="nsew")
                            plbl.bind("<Button-1>",
                                     lambda e, r=row, c=col: self._on_cell_click(r, c))
                            plbl.bind("<Button-3>",
                                     lambda e, r=row, c=col, pn=pn: self._on_piece_remove(r, c, pn))
                            self.cell_labels[(row, col)][pn] = plbl
                        else:
                            tk.Label(cell, text="", width=3, height=1,
                                    bg="#FFFFFF").grid(row=pr, column=pc, padx=0, pady=0, sticky="nsew")

        # 使用 uniform 确保所有行高一致、所有列宽一致
        for row in range(6):
            cond_frame.rowconfigure(row, weight=1, uniform="cond_row")
        for col in range(5):
            cond_frame.columnconfigure(col, weight=1, uniform="cond_col")

        # ===== 右侧: 结果面板 (6x5) =====
        result_frame = ttk.LabelFrame(main_frame, text="结果面板")
        result_frame.grid(row=0, column=1, padx=(5, 0), sticky="nsew")

        self.cell_result_labels = {}

        # 结果面板: 固定宽度21字符
        for c in range(5):
            result_frame.columnconfigure(c, weight=0, uniform="result_col")

        for row in range(6):
            for col in range(5):
                if self._is_camp(row, col):
                    self._create_camp_label(result_frame, row, col, width=21, height=self._result_cell_height)
                    continue

                # 使用 Text 控件支持彩色文字
                txt = tk.Text(result_frame, bg="#FFFFFF", relief=tk.GROOVE, bd=1,
                              wrap=tk.WORD, font=("Consolas", 10),
                              height=self._result_cell_height, width=21,
                              state=tk.DISABLED)
                txt.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
                self.cell_result_labels[(row, col)] = txt

        for row in range(6):
            result_frame.rowconfigure(row, weight=1, uniform="result_row")

    def _on_drag_click(self, piece_name: str) -> None:
        """点击拖拽面板的棋子: RadioButton行为, 同时只能选中一个"""
        if self._selected_piece == piece_name:
            # 取消选中
            self._selected_piece = None
            self._drag_labels[piece_name].config(bg="#E8E8E8", relief=tk.RAISED)
            self.result_var.set("点击棋子后，左键单击放置在指定位置，右键单击取消")
        else:
            # 取消之前的选中
            if self._selected_piece:
                self._drag_labels[self._selected_piece].config(bg="#E8E8E8", relief=tk.RAISED)
            # 选中新的
            self._selected_piece = piece_name
            self._drag_labels[piece_name].config(bg="#90EE90", relief=tk.SUNKEN)
            self.result_var.set(f"已选中: {piece_name}，左键放置，右键取消")

    def _on_cell_click(self, row: int, col: int) -> None:
        """左键点击条件面板格子: 放置选中的棋子"""
        if not self._selected_piece:
            return
        piece_name = self._selected_piece

        if not self._can_place(row, piece_name):
            messagebox.showwarning("提示", f"{piece_name} 不能放在第 {row+1} 行", parent=self._top)
            return

        if piece_name in self.cell_pieces[(row, col)]:
            return

        self.cell_pieces[(row, col)].add(piece_name)
        if piece_name in self.cell_labels[(row, col)]:
            self.cell_labels[(row, col)][piece_name].config(text=piece_name, bg="#90EE90")

    def _on_cell_right_click(self, row: int, col: int) -> None:
        """右键点击条件面板格子: 移除最后放置的棋子"""
        if self.cell_pieces[(row, col)]:
            # 移除最后添加的棋子
            last_piece = list(self.cell_pieces[(row, col)])[-1]
            self.cell_pieces[(row, col)].discard(last_piece)
            if last_piece in self.cell_labels[(row, col)]:
                self.cell_labels[(row, col)][last_piece].config(text="", bg="#FFFFFF")

    def _on_piece_remove(self, row: int, col: int, piece_name: str) -> None:
        """点击条件面板格子里的棋子: 移除"""
        if piece_name in self.cell_pieces[(row, col)]:
            self.cell_pieces[(row, col)].discard(piece_name)
            if piece_name in self.cell_labels[(row, col)]:
                self.cell_labels[(row, col)][piece_name].config(text="", bg="#FFFFFF")

    def update_for_direction(self) -> None:
        """根据方向模式更新格子显示状态"""
        is_mainline = self._is_mainline_getter()
        target = (5, 1)

        if target not in self.cond_cells:
            return

        cell = self.cond_cells[target]
        if is_mainline:
            # 先销毁所有子控件
            for child in cell.winfo_children():
                child.destroy()
            self.cell_labels[target] = {}
            # 创建旗标签
            lbl = tk.Label(cell, text="旗", bg="#C0C0C0", font=("Consolas", 10, "bold"))
            lbl.pack(fill=tk.BOTH, expand=True)
            self.cell_pieces[target].clear()
        else:
            for child in cell.winfo_children():
                child.destroy()
            self.cell_pieces[target].clear()
            self.cell_labels[target] = {}
            grid_rows = 4
            for pr in range(grid_rows):
                for pc in range(3):
                    piece_idx = pr * 3 + pc
                    if piece_idx < len(self.PIECE_OPTIONS):
                        pn = self.PIECE_OPTIONS[piece_idx][0]
                        if not self._can_place(target[0], pn):
                            continue
                        plbl = tk.Label(cell, text="", width=3, height=1,
                                       font=("Consolas", 10), bg="#FFFFFF")
                        plbl.grid(row=pr, column=pc, padx=1, pady=1)
                        plbl.bind("<Button-1>",
                                 lambda e, r=target[0], c=target[1], pn=pn: self._on_piece_remove(r, c, pn))
                        self.cell_labels[target][pn] = plbl

    def _clear_all(self) -> None:
        """清空条件面板和结果面板"""
        for (row, col), pieces in self.cell_pieces.items():
            pieces.clear()
            for pn, lbl in self.cell_labels[(row, col)].items():
                lbl.config(text="", bg="#FFFFFF")
        for (row, col), txt in self.cell_result_labels.items():
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.config(state=tk.DISABLED)
        if self._selected_piece:
            self._drag_labels[self._selected_piece].config(bg="#E8E8E8", relief=tk.RAISED)
            self._selected_piece = None
        self.result_var.set("点击棋子后，左键单击放置在指定位置，右键单击取消")

    @staticmethod
    def _mirror_col(col: int) -> int:
        """镜像列: 0↔4, 1↔3, 2不变"""
        return 4 - col

    def _get_mirror_flag(self) -> bool:
        """根据方向返回是否需要镜像布局数据
        逆时针: 不镜像
        顺时针: 镜像
        旗侧视角: 不镜像(由调用方处理左右旗合并)"""
        direction = self._direction_getter()
        if self._is_mainline_getter():
            return False
        return direction == "顺时针"

    def _get_piece_from_layout(self, layout, row: int, col: int, mirror: bool):
        """从QQ布局获取棋子, 支持镜像"""
        c = self._mirror_col(col) if mirror else col
        return layout.item(row, c)

    def _get_piece_from_matrix(self, board, row: int, col: int, mirror: bool, c2i: dict):
        """从联众矩阵获取棋子, 支持镜像"""
        c = self._mirror_col(col) if mirror else col
        if row >= len(board) or c >= len(board[row]):
            return ItemType.JQNone
        return c2i.get(board[row][c], ItemType.JQNone)

    def _check_conditions_block(self, layout, conditions: dict, mirror: bool) -> bool:
        """检查QQ布局是否匹配条件"""
        for (row, col), allowed in conditions.items():
            piece = self._get_piece_from_layout(layout, row, col, mirror)
            if piece not in allowed:
                return False
        return True

    def _check_conditions_matrix(self, board, conditions: dict, mirror: bool, c2i: dict) -> bool:
        """检查联众布局是否匹配条件"""
        for (row, col), allowed in conditions.items():
            piece = self._get_piece_from_matrix(board, row, col, mirror, c2i)
            if piece not in allowed:
                return False
        return True

    def _start_stat(self) -> None:
        """开始条件概率统计"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return

        # 清空上次统计结果
        self._stored_result = None
        self._clear_result_labels()

        # 收集条件格子
        conditions = {}
        PIECE_NAME_TO_TYPE = {name: pt for name, pt in self.PIECE_OPTIONS}

        for (row, col), pieces in self.cell_pieces.items():
            if pieces:
                checked = set()
                for pn in pieces:
                    checked.add(PIECE_NAME_TO_TYPE[pn])
                conditions[(row, col)] = checked

        if not conditions:
            messagebox.showwarning("提示", "请在条件面板中放置至少一个棋子", parent=self._top)
            return

        # 结果格子 = 所有非条件、非行营格子
        result_cells = set()
        for row in range(6):
            for col in range(5):
                if (row, col) in CAMP_POSITIONS:
                    continue
                if (row, col) not in conditions:
                    result_cells.add((row, col))

        self.stat_btn.config(state=tk.DISABLED)
        self.result_var.set("统计中……")

        is_lianzhong = self._source_var.get() == "联众"
        is_mainline = self._is_mainline_getter()
        show_pct = self._display_mode_getter() == "百分比"
        # 逆时针不镜像, 顺时针镜像 (旗侧视角由特殊逻辑处理)
        normal_mirror = self._get_mirror_flag()

        def worker():
            try:
                result_counts = {(r, c): {} for r, c in result_cells}
                match_total = 0
                total = 0
                matched_files = []  # 收集匹配的文件路径
                matched_layouts = []  # 收集匹配的布阵数据

                if is_lianzhong:
                    from src.lianzhong import decode_jqh_all_boards, get_char_to_itemtype
                    c2i = get_char_to_itemtype()
                    pattern = os.path.join(folder, "**", "*.jqh")
                    files = sorted(glob.glob(pattern, recursive=True))
                    total = len(files)

                    for i, jqh_file in enumerate(files):
                        try:
                            raw = open(jqh_file, "rb").read()
                            boards, _ = decode_jqh_all_boards(raw)
                            # 检查是否有非法棋子
                            skip = False
                            for color, board in boards.items():
                                if all(ch == "空" for row in board for ch in row):
                                    continue
                                for row in range(6):
                                    for col in range(5):
                                        if (row, col) in self.CAMP_POSITIONS:
                                            continue
                                        ch = board[row][col] if row < len(board) and col < len(board[row]) else "空"
                                        if ch == "空":
                                            skip = True
                                            break
                                    if skip:
                                        break
                                if skip:
                                    break
                            if skip:
                                continue
                            for color, board in boards.items():
                                if all(ch == "空" for row in board for ch in row):
                                    continue

                                if is_mainline:
                                    flag_col = self._find_flag_col_matrix(board, c2i)
                                    mirror_this = (flag_col == 3)
                                else:
                                    mirror_this = normal_mirror

                                if self._check_conditions_matrix(board, conditions, mirror_this, c2i):
                                    match_total += 1
                                    matched_files.append(jqh_file)
                                    matched_layouts.append((jqh_file, color, board))
                                    for rc in result_cells:
                                        r, c = rc
                                        pt = self._get_piece_from_matrix(board, r, c, mirror_this, c2i)
                                        result_counts[rc][pt] = result_counts[rc].get(pt, 0) + 1
                        except Exception:
                            pass
                        self.result_var.set(f"统计中…… {i+1}/{total}")
                else:
                    from src.layout_stats import find_jgs_files, load_total_info
                    files = find_jgs_files(folder)
                    total = len(files)

                    for i, jgs_file in enumerate(files):
                        try:
                            total_info = load_total_info(jgs_file)
                            # 检查是否有非法棋子
                            skip = False
                            for pib in total_info.player_info_block:
                                err = self._layout_has_invalid_piece(pib.player_layout)
                                if err:
                                    skip = True
                                    break
                            if skip:
                                continue
                            for pib in total_info.player_info_block:
                                layout = pib.player_layout
                                if all(layout.item(r, c) == ItemType.JQNone
                                       for r in range(JGSLayoutBlock.row_num)
                                       for c in range(JGSLayoutBlock.col_num)):
                                    continue

                                if is_mainline:
                                    flag_col = self._find_flag_col_block(layout)
                                    mirror_this = (flag_col == 3)
                                else:
                                    mirror_this = normal_mirror

                                if self._check_conditions_block(layout, conditions, mirror_this):
                                    match_total += 1
                                    matched_files.append(jgs_file)
                                    matched_layouts.append((jgs_file, "QQ", layout))
                                    for rc in result_cells:
                                        r, c = rc
                                        pt = self._get_piece_from_layout(layout, r, c, mirror_this)
                                        result_counts[rc][pt] = result_counts[rc].get(pt, 0) + 1
                        except Exception:
                            pass
                        self.result_var.set(f"统计中…… {i+1}/{total}")

                self._stat_result = (result_counts, match_total, total, show_pct, None)
                self._matched_files = matched_files
                self._matched_layouts = matched_layouts
            except Exception as e:
                self._stat_result = ({}, 0, 0, show_pct, str(e))
                self._matched_files = []
                self._matched_layouts = []

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    @staticmethod
    def _find_flag_col_block(layout: JGSLayoutBlock) -> int:
        """查找QQ布局中军旗所在列, 默认返回1(左)"""
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                if layout.item(row, col) == ItemType.Junqi:
                    return col
        return 1

    @staticmethod
    def _find_flag_col_matrix(board, c2i: dict) -> int:
        """查找联众布局中军旗所在列, 默认返回1(左)"""
        for row in range(len(board)):
            for col in range(len(board[row])):
                if c2i.get(board[row][col]) == ItemType.Junqi:
                    return col
        return 1

    def _poll_worker(self) -> None:
        if self._worker.is_alive():
            self.after(100, self._poll_worker)
            return

        result_counts, match_total, total, show_pct, err = self._stat_result
        self.stat_btn.config(state=tk.NORMAL)

        if err:
            self.result_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err, parent=self._top)
            return
        if match_total == 0:
            self.result_var.set(f"无匹配布阵 (总文件: {total})")
            return

        # 存储统计结果，用于切换显示模式时刷新
        self._stored_result = (result_counts, match_total, total)

        # 渲染结果
        self._render_results(result_counts, match_total, total, show_pct)

        # 启用导出按钮
        self.export_replay_btn.config(state=tk.NORMAL)
        self.export_layout_btn.config(state=tk.NORMAL)

    def _render_results(self, result_counts, match_total, total, show_pct):
        """渲染统计结果到结果面板 (带颜色)"""
        import math
        from src.jgs_types import item_type2string
        for rc, counts in result_counts.items():
            total_count = sum(counts.values())
            if total_count == 0:
                continue

            # 按原始频率降序排序
            sorted_items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))

            # 计算数量格式宽度 (与 layout_stats._fmt_count 一致)
            count_width = 1 + int(math.log10(total_count)) if total_count > 0 else 1

            # 更新结果 Text 控件
            txt = self.cell_result_labels.get(rc)
            if not txt:
                continue
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", tk.END)

            # 每行两个棋子，带排名颜色
            for i in range(0, len(sorted_items), 2):
                if i > 0:
                    txt.insert(tk.END, "\n")
                for j in range(2):
                    if i + j < len(sorted_items):
                        pt, cnt = sorted_items[i + j]
                        rank = i + j + 1  # 排名从1开始
                        if show_pct:
                            ratio = cnt / total_count
                            pct_val = ratio * 100
                            val = " 100%" if pct_val == 100 else f"{pct_val:5.1f}%"
                        else:
                            val = f"{cnt:{count_width}d}"
                        seg_text = f"{item_type2string(pt)}:{val}"
                        color = self._rank_color(rank)
                        tag = f"r{i}_{j}"
                        txt.tag_configure(tag, foreground=color)
                        if j > 0:
                            txt.insert(tk.END, "  ")
                        start = txt.index(tk.INSERT)
                        txt.insert(tk.END, seg_text)
                        end = txt.index(tk.INSERT)
                        txt.tag_add(tag, start, end)
            txt.config(state=tk.DISABLED)

        self.result_var.set(f"匹配: {match_total}/{total} ({match_total/total*100:.1f}%)")

    def clear_stats(self) -> None:
        """清空统计结果"""
        self._stored_result = None
        self._matched_files = []
        self._matched_layouts = []
        self._clear_all()

    def _refresh_grid(self) -> None:
        """方向变化或显示模式变化时刷新结果"""
        if self._stored_result:
            # 有存储的统计结果，重新渲染
            result_counts, match_total, total = self._stored_result
            show_pct = self._display_mode_getter() == "百分比"
            self._render_results(result_counts, match_total, total, show_pct)
        else:
            self._clear_result_labels()

    def _clear_result_labels(self) -> None:
        """清空所有结果标签内容"""
        for rc, txt in self.cell_result_labels.items():
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.config(state=tk.DISABLED)
        self.result_var.set("点击棋子后，左键单击放置在指定位置，右键单击取消")

    def _export_replays(self) -> None:
        """导出匹配的复盘文件（按文件去重）"""
        if not hasattr(self, '_matched_files') or not self._matched_files:
            messagebox.showwarning("提示", "没有可导出的复盘", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            seen_files = set()
            unique_files = []
            for file_path in self._matched_files:
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    unique_files.append(file_path)

            total_count = len(unique_files)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1

            exported = 0
            used_names = set()
            import shutil
            for i, src_path in enumerate(unique_files):
                original_name = Path(src_path).name
                ext = Path(src_path).suffix
                new_filename = f"{i:0{prefix_digits}d}_{original_name}"
                counter = 1
                base = new_filename
                while new_filename in used_names:
                    new_filename = f"{Path(base).stem}_{counter}{ext}"
                    counter += 1
                used_names.add(new_filename)

                output_path = os.path.join(save_dir, new_filename)
                shutil.copy2(src_path, output_path)
                exported += 1
                self.result_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个复盘文件(去重后)到:\n{save_dir}", parent=self._top)
            self.result_var.set(f"已导出 {exported} 个复盘文件")
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)

    def _export_layouts(self) -> None:
        """导出匹配的布阵文件（按布阵去重，导出为jqs/jql格式）"""
        if not hasattr(self, '_matched_layouts') or not self._matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布阵", parent=self._top)
            return

        top = self.winfo_toplevel()
        geo = top.geometry()
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=top)
        top.geometry(geo)
        if not save_dir:
            return

        try:
            is_jqs = self.export_format_var.get() == "联众(jqs)"

            # JQS/JQL格式常量
            JQS_HEADER = bytes([
                0x47, 0x4C, 0xE9, 0x03, 0xCE, 0x07, 0xC4, 0x02, 0x53, 0x61, 0x74, 0x20,
                0x41, 0x75, 0x67, 0x20, 0x30, 0x31, 0x20, 0x32, 0x32, 0x3A, 0x34, 0x35,
                0x3A, 0x30, 0x33, 0x20, 0x32, 0x30, 0x32, 0x36, 0x0A, 0x00, 0x00, 0x00,
                0x62, 0x79, 0x20, 0x5B, 0xD1, 0xD0, 0xBE, 0xBF, 0x31, 0x5D, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00
            ])
            JQS_LAYOUT_OFFSET = 0x370
            JQS_ROW_STRIDE = 17
            JQS_FILE_SIZE = 1264
            PIECE_TO_JQS = {
                ItemType.JQNone: 0x00, ItemType.Junzhang: 0x01, ItemType.Shizhang: 0x02,
                ItemType.Lvzhang: 0x03, ItemType.Tuanzhang: 0x04, ItemType.Yinzhang: 0x05,
                ItemType.Lianzhang: 0x06, ItemType.Paizhang: 0x07, ItemType.Gongbing: 0x08,
                ItemType.Junqi: 0x09, ItemType.Silin: 0x0A, ItemType.Dilei: 0x0B,
                ItemType.Zhadan: 0x0C,
            }
            JQL_HEADER = bytes([0x51, 0x51, 0x47, 0x61, 0x6D, 0x65, 0x20, 0x4A,
                                0x51, 0x4C, 0x20, 0x46, 0x69, 0x6C, 0x65, 0x00,
                                0x57, 0x04, 0x00, 0x00])
            JQL_LAYOUT_OFFSET = 0x14
            JQL_FILE_SIZE = 50
            PIECE_TO_JQL = PIECE_TO_JQS

            # 按布阵内容去重
            seen_keys = set()
            unique_layouts = []
            for file_path, color, layout_data in self._matched_layouts:
                key = self._layout_to_key(layout_data)
                if key not in seen_keys:
                    seen_keys.add(key)
                    unique_layouts.append((file_path, color, layout_data))

            total_count = len(unique_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
            PIECE_TO_EXPORT = PIECE_TO_JQS if is_jqs else PIECE_TO_JQL

            exported = 0
            for i, (file_path, color, layout_data) in enumerate(unique_layouts):
                original_name = Path(file_path).stem
                ext = ".jqs" if is_jqs else ".jql"
                new_filename = f"{i:0{prefix_digits}d}_{original_name}{ext}"
                output_path = os.path.join(save_dir, new_filename)

                if is_jqs:
                    data = bytearray(JQS_FILE_SIZE)
                    data[0:len(JQS_HEADER)] = JQS_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQS_LAYOUT_OFFSET + (row * JQS_ROW_STRIDE + col) * 4
                            if isinstance(layout_data, JGSLayoutBlock):
                                pt = layout_data.item(row, col)
                            else:
                                from src.lianzhong import get_char_to_itemtype
                                c2i = get_char_to_itemtype()
                                pt = c2i.get(layout_data[row][col], ItemType.JQNone)
                            code = 0x00 if (row, col) in CAMP_POSITIONS else PIECE_TO_EXPORT.get(pt, 0x00)
                            struct.pack_into('<I', data, offset, code)
                else:
                    data = bytearray(JQL_FILE_SIZE)
                    data[0:20] = JQL_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQL_LAYOUT_OFFSET + row * 5 + col
                            if isinstance(layout_data, JGSLayoutBlock):
                                pt = layout_data.item(row, col)
                            else:
                                from src.lianzhong import get_char_to_itemtype
                                c2i = get_char_to_itemtype()
                                pt = c2i.get(layout_data[row][col], ItemType.JQNone)
                            data[offset] = int(pt)

                with open(output_path, 'wb') as f:
                    f.write(data)
                exported += 1
                self.result_var.set(f"导出中…… {exported}/{total_count}")

            messagebox.showinfo("完成", f"成功导出 {exported} 个布阵文件到:\n{save_dir}", parent=self._top)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self._top)


class QQReplayPanel(ttk.Frame):
    """QQ 复盘完整对局解析: 单文件解析、布阵/行棋/得失展示与导出"""

    def __init__(self, parent):
        super().__init__(parent)
        self.replay = None
        self.analysis = None
        self.file_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请选择 .jgs 复盘文件")
        self.stats_summary_var = tk.StringVar(value="")
        self._layout_labels: dict[tuple[str, int, int], tk.Label] = {}
        self._layout_groups: dict[str, ttk.LabelFrame] = {}
        self._layout_headers: dict[str, ttk.Label] = {}
        self._build_ui()

    @property
    def _top(self):
        return self.winfo_toplevel()

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(0, 0, 0, 6))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="选择复盘...", command=self._choose_file).pack(side=tk.LEFT)
        ttk.Label(bar, text="文件:").pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="清空", command=self._clear).pack(side=tk.RIGHT)
        self.export_text_btn = ttk.Button(
            bar, text="导出文本", command=self._export_text, state=tk.DISABLED)
        self.export_text_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self.export_json_btn = ttk.Button(
            bar, text="导出 JSON", command=self._export_json, state=tk.DISABLED)
        self.export_json_btn.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(bar, text="解析", command=self._parse_current).pack(side=tk.RIGHT)
        entry = ttk.Entry(bar, textvariable=self.file_var, width=62)
        entry.pack(side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._parse_current())

        ttk.Label(self, textvariable=self.status_var, padding=(2, 2, 2, 0)).pack(
            fill=tk.X, side=tk.BOTTOM)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self._build_overview_tab()
        self._build_layout_tab()
        self._build_moves_tab()
        self._build_stats_tab()

    def _build_overview_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="对局概览")
        self.overview_text = tk.Text(
            frame, wrap=tk.WORD, font=("Microsoft YaHei UI", 10),
            state=tk.DISABLED, padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.overview_text.yview)
        self.overview_text.configure(yscrollcommand=scroll.set)
        self.overview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.overview_text.tag_configure("heading", font=("Microsoft YaHei UI", 11, "bold"))
        self.overview_text.tag_configure("warn", foreground="#B00000")

    def _build_layout_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="四方布阵")
        for i, color in enumerate(COLOR_ORDER):
            group = ttk.LabelFrame(frame, text=color)
            group.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
            frame.grid_rowconfigure(i // 2, weight=1)
            frame.grid_columnconfigure(i % 2, weight=1)
            header = ttk.Label(group, text="", font=("Microsoft YaHei UI", 10, "bold"))
            header.pack(anchor=tk.W, padx=6, pady=(4, 2))
            self._layout_headers[color] = header
            grid = ttk.Frame(group)
            grid.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
            for row in range(6):
                grid.grid_rowconfigure(row, weight=1)
                for col in range(5):
                    grid.grid_columnconfigure(col, weight=1)
                    lbl = tk.Label(
                        grid, text="", width=3, height=1, relief=tk.SOLID, bd=1,
                        bg=CELL_BG, font=("Microsoft YaHei UI", 13, "bold"))
                    lbl.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
                    self._layout_labels[(color, row, col)] = lbl
            self._layout_groups[color] = group

    def _build_moves_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="行棋记录")
        columns = ("no", "color", "start", "end", "result", "event", "net")
        self.moves_tree = ttk.Treeview(frame, columns=columns, show="headings")
        titles = {
            "no": ("序号", 60), "color": ("颜色", 60), "start": ("起点", 90),
            "end": ("终点", 90), "result": ("结果", 160), "event": ("关键事件", 320),
            "net": ("子力变化", 180),
        }
        for key, (title, width) in titles.items():
            self.moves_tree.heading(key, text=title)
            self.moves_tree.column(key, width=width, anchor=tk.CENTER, stretch=(key == "event"))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.moves_tree.yview)
        self.moves_tree.configure(yscrollcommand=scroll.set)
        self.moves_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.moves_tree.tag_configure("event", background="#FFF6D8")
        self.moves_tree.tag_configure("error", background="#FFE0E0")

    def _build_stats_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="子力统计")
        ttk.Label(
            frame, textvariable=self.stats_summary_var,
            font=("Microsoft YaHei UI", 10, "bold"), padding=(8, 8, 8, 4),
        ).pack(fill=tk.X)
        columns = (
            "player", "seat", "steps", "eat", "killed", "both", "bomb_kill",
            "bomb_hit", "dig", "flag", "mine", "mistakes", "score", "loss", "net",
        )
        titles = {
            "player": ("玩家", 150), "seat": ("座位", 60), "steps": ("步数", 55),
            "eat": ("吃子", 55), "killed": ("被吃", 55), "both": ("同尽", 55),
            "bomb_kill": ("炸杀", 55), "bomb_hit": ("中炸", 55), "dig": ("挖雷", 55),
            "flag": ("扛旗", 55), "mine": ("触雷", 55), "mistakes": ("失着", 55),
            "score": ("得分", 65), "loss": ("损失", 65), "net": ("净值", 65),
        }
        self.stats_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, (title, width) in titles.items():
            self.stats_tree.heading(key, text=title)
            self.stats_tree.column(key, width=width, anchor=tk.CENTER,
                                   stretch=(key == "player"))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.stats_tree.yview)
        self.stats_tree.configure(yscrollcommand=scroll.set)
        self.stats_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _choose_file(self) -> None:
        top = self._top
        geo = top.geometry()
        path = filedialog.askopenfilename(
            title="选择 QQ 复盘文件", parent=top,
            filetypes=[("QQ 复盘", "*.jgs"), ("所有文件", "*.*")])
        top.geometry(geo)
        if path:
            self.file_var.set(path)
            self._parse_current()

    def _parse_current(self) -> None:
        path = self.file_var.get().strip()
        if not path:
            messagebox.showwarning("提示", "请先选择 .jgs 复盘文件", parent=self._top)
            return
        try:
            replay = parse_jgs(path)
            analysis = replay.analyze()
        except Exception as exc:
            self.replay = None
            self.analysis = None
            self.export_json_btn.config(state=tk.DISABLED)
            self.export_text_btn.config(state=tk.DISABLED)
            self._refresh_overview()
            self._refresh_layouts()
            self._refresh_moves()
            self._refresh_stats()
            self.status_var.set(f"解析失败: {exc}")
            messagebox.showerror("解析失败", str(exc), parent=self._top)
            return
        self._set_replay(replay, analysis)

    def _set_replay(self, replay, analysis) -> None:
        self.replay = replay
        self.analysis = analysis
        self.file_var.set(replay.path)
        self.export_json_btn.config(state=tk.NORMAL)
        self.export_text_btn.config(state=tk.NORMAL)
        self._refresh_overview()
        self._refresh_layouts()
        self._refresh_moves()
        self._refresh_stats()
        self.status_var.set(
            f"解析完成: {Path(replay.path).name}  "
            f"棋步 {replay.move_count} / 事件 {replay.event_count}  "
            f"方位自洽率 {replay.self_check_rate * 100:.1f}%  "
            f"异常步数 {analysis.error_count}")

    def _clear(self) -> None:
        self.replay = None
        self.analysis = None
        self.file_var.set("")
        self.export_json_btn.config(state=tk.DISABLED)
        self.export_text_btn.config(state=tk.DISABLED)
        self.status_var.set("请选择 .jgs 复盘文件")
        self._refresh_overview()
        self._refresh_layouts()
        self._refresh_moves()
        self._refresh_stats()

    def _refresh_overview(self) -> None:
        widget = self.overview_text
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if self.replay is None or self.analysis is None:
            widget.config(state=tk.DISABLED)
            return
        replay = self.replay
        analysis = self.analysis
        lines = [
            ("heading", "基础信息\n"),
            ("", f"文件: {replay.path}\n"),
            ("", f"大小: {replay.file_size} 字节\n"),
            ("", f"头部步数: {replay.header_steps}（含结束事件）   "
                 f"实际步数: {len(replay.seq)}（棋步 {replay.move_count} / "
                 f"事件 {replay.event_count}）\n"),
        ]
        my_name = replay.players.get(replay.my_color, "") if replay.my_color else ""
        lines.append(("", f"己方: {replay.my_color or '未知'}"
                          f"{'（' + my_name + '）' if my_name else ''}\n"))
        directions = "  ".join(
            f"{block.color}={DIR_CN.get(block.orient, block.orient)}" for block in replay.blocks)
        seats = "  ".join(f"{block.color}={block.seat}" for block in replay.blocks)
        lines.append(("", f"方位: {directions}\n"))
        lines.append(("", f"座位: {seats}\n"))
        lines.append(("", f"行棋顺序: {' → '.join(replay.turn_order)}\n"))
        lines.append(("", f"方位自洽率: {replay.self_check_rate * 100:.1f}%\n"))
        lines.append(("", f"结果: {analysis.result_text}\n"))
        audit_tag = "warn" if analysis.error_count else ""
        lines.append((audit_tag, f"数据自洽: {analysis.error_count} 步异常\n"))

        lines.append(("heading", "\n四方玩家\n"))
        for block in replay.blocks:
            qq = f"  QQ: {block.qq_number}" if block.qq_number else ""
            lines.append(("", f"{block.color} · {block.seat} · {block.name}{qq}\n"))

        key_events = [
            step for step in analysis.steps
            if step.resolved is not None
            and step.resolved.event_text
            and step.resolved.severity >= 2
        ]
        if key_events:
            lines.append(("heading", "\n关键事件\n"))
            for step in key_events[:30]:
                lines.append(("", f"第 {step.no} 步  {step.resolved.event_text}\n"))
            if len(key_events) > 30:
                lines.append(("", f"其余 {len(key_events) - 30} 条已省略，详见行棋记录\n"))

        if analysis.errors:
            lines.append(("heading", "\n数据异常\n"))
            for step_no, desc in analysis.errors[:10]:
                lines.append(("warn", f"第 {step_no} 步: {desc}\n"))
            if len(analysis.errors) > 10:
                lines.append(("warn", f"其余 {len(analysis.errors) - 10} 条已省略\n"))

        for tag, text in lines:
            widget.insert(tk.END, text, tag if tag else ())
        widget.config(state=tk.DISABLED)

    def _refresh_layouts(self) -> None:
        for color in COLOR_ORDER:
            group = self._layout_groups.get(color)
            header = self._layout_headers.get(color)
            block = next((b for b in self.replay.blocks if b.color == color), None) \
                if self.replay else None
            if block is not None:
                group.config(text=f"{color} · {block.seat}")
                header.config(text=block.name)
            else:
                group.config(text=color)
                header.config(text="")
            for row in range(6):
                for col in range(5):
                    lbl = self._layout_labels.get((color, row, col))
                    if lbl is None:
                        continue
                    if (row, col) in CAMP_POSITIONS:
                        lbl.config(text="", bg=CAMP_BG)
                    elif block is not None:
                        code = block.matrix[row * 5 + col]
                        lbl.config(
                            text=PIECE_SHORT.get(code, "?"), bg=CELL_BG,
                            fg="#B00000" if code == int(ItemType.Junqi) else "#000000")
                    else:
                        lbl.config(text="", bg=CELL_BG)

    def _refresh_moves(self) -> None:
        self._clear_tree(self.moves_tree)
        if self.analysis is None:
            return
        for step in self.analysis.steps:
            if step.kind == "event":
                values = (step.no, step.event.color or "", "", "", "事件",
                          step.event_text, "")
                self.moves_tree.insert("", tk.END, values=values, tags=("event",))
                continue
            resolved = step.resolved
            values = (
                step.no,
                step.move.color,
                f"{step.move.start[0]},{step.move.start[1]}",
                f"{step.move.end[0]},{step.move.end[1]}",
                resolved.desc,
                resolved.event_text or "",
                self._format_delta(resolved.delta),
            )
            tags = ("error",) if resolved.error else ()
            self.moves_tree.insert("", tk.END, values=values, tags=tags)
        self.moves_tree.yview_moveto(0)

    def _refresh_stats(self) -> None:
        self._clear_tree(self.stats_tree)
        if self.replay is None or self.analysis is None:
            self.stats_summary_var.set("")
            return
        analysis = self.analysis
        self.stats_summary_var.set(
            f"结果: {analysis.result_text}    方位自洽率: "
            f"{self.replay.self_check_rate * 100:.1f}%    数据异常: "
            f"{analysis.error_count} 步")
        for color in COLOR_ORDER:
            stats = analysis.stats.get(color)
            block = next((b for b in self.replay.blocks if b.color == color), None)
            if stats is None or block is None:
                continue
            values = (
                f"{color} {block.name}", block.seat, stats.steps, stats.eat,
                stats.killed, stats.both, stats.bomb_kill, stats.bomb_hit,
                stats.dig, stats.flag, stats.mine, stats.mistakes,
                stats.score, stats.loss, stats.net,
            )
            self.stats_tree.insert("", tk.END, values=values)

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    @staticmethod
    def _format_delta(delta: dict[str, int]) -> str:
        return "  ".join(f"{color}{value:+d}" for color, value in delta.items() if value)

    def _export_json(self) -> None:
        if self.replay is None:
            return
        top = self._top
        geo = top.geometry()
        path = filedialog.asksaveasfilename(
            title="导出解析 JSON", parent=top, defaultextension=".json",
            initialfile=f"{Path(self.replay.path).stem}_解析.json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        top.geometry(geo)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.replay.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=top)
            return
        messagebox.showinfo("完成", f"已导出: {path}", parent=top)

    def _export_text(self) -> None:
        if self.replay is None:
            return
        top = self._top
        geo = top.geometry()
        path = filedialog.asksaveasfilename(
            title="导出复盘文本", parent=top, defaultextension=".txt",
            initialfile=f"{Path(self.replay.path).stem}_复盘v2.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        top.geometry(geo)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.replay.to_text())
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=top)
            return
        messagebox.showinfo("完成", f"已导出: {path}", parent=top)


class QQBatchPanel(ttk.Frame):
    """QQ 复盘批量统计：四色×12 棋子条件 + 17×17 条件/结果棋盘"""

    CELL = 26
    MARGIN = 15

    def __init__(self, parent, folder_var: tk.StringVar, display_mode_getter=None):
        super().__init__(parent, padding=2)
        self._folder_var = folder_var
        self._display_mode_getter = display_mode_getter or (lambda: "百分比")
        self._selected: tuple[str, int] | None = None
        self._legal: set[tuple[int, int]] = set()
        self._conditions: dict[tuple[int, int], set[tuple[str, int]]] = {}
        self._active_cells: set[tuple[int, int]] = set()
        self._result_counts: dict[tuple[int, int], dict[tuple[str, int], int]] = {}
        self._match_total = 0
        self._snapshot_total = 0
        self._stored_result = None
        self._stored_mode = None
        self._worker = None
        self._progress_queue: queue.Queue = queue.Queue()
        self._drag = None
        self._piece_buttons: dict[tuple[str, int], tk.Label] = {}
        self.first_only_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(
            value="拖拽或点击左侧棋子；可配置全盘，旗/雷限初始位；结果格需左键点亮")
        self.cond_hover_var = tk.StringVar(
            value="旗/雷限初始位；方位：黄上 / 紫左 / 绿下 / 蓝右；悬停条件格查看格内“或”条件")
        self._build_ui()

    @property
    def _top(self):
        return self.winfo_toplevel()

    @property
    def _board_size(self) -> int:
        return self.MARGIN * 2 + 17 * self.CELL

    def _build_ui(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 4))
        controls = ttk.Frame(bar)
        controls.pack(fill=tk.X)
        self.stat_btn = ttk.Button(controls, text="统计", command=self._start_stat)
        self.stat_btn.pack(side=tk.LEFT)
        ttk.Button(controls, text="清空所有", command=self._clear_all).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            controls, text="仅统计首次满足快照", variable=self.first_only_var,
            command=self._on_first_only_change,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(bar, textvariable=self.status_var).pack(
            fill=tk.X, pady=(2, 0))

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True)
        main.grid_columnconfigure(0, weight=0)
        main.grid_columnconfigure(1, weight=1)
        main.grid_columnconfigure(2, weight=1)
        main.grid_rowconfigure(0, weight=1)

        left = ttk.LabelFrame(main, text="棋子条件（四色 × 12）")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.configure(width=205)
        left.grid_propagate(False)
        for color in SEAT_COLORS:
            group = ttk.LabelFrame(left, text=f"{color} · {CANONICAL_SEAT[color]}")
            group.pack(fill=tk.X, padx=4, pady=(2, 4))
            for idx, piece in enumerate(PIECE_ORDER):
                row, col = divmod(idx, 6)
                piece_code = int(piece)
                lbl = tk.Label(
                    group, text=PIECE_SHORT[piece_code], width=2, height=1,
                    bg=COLOR_FILL[color], fg=COLOR_TEXT_ON[color],
                    font=("Microsoft YaHei UI", 9, "bold"),
                    relief=tk.RAISED, bd=1, cursor="hand2")
                lbl.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
                lbl.bind("<ButtonPress-1>",
                         lambda e, c=color, p=piece_code: self._on_piece_press(c, p, e))
                lbl.bind("<B1-Motion>", self._on_piece_motion)
                lbl.bind("<ButtonRelease-1>", self._on_piece_release)
                self._piece_buttons[(color, piece_code)] = lbl
            for col in range(6):
                group.columnconfigure(col, weight=1)

        cond_frame = ttk.LabelFrame(
            main, text="条件棋盘（格内或 / 格间且；全盘可选）")
        cond_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
        self.cond_canvas = tk.Canvas(
            cond_frame, width=self._board_size, height=self._board_size,
            bg="#FFFFFF", highlightthickness=0)
        self.cond_canvas.pack(padx=4, pady=(4, 2))
        ttk.Label(cond_frame, textvariable=self.cond_hover_var, wraplength=460).pack(
            fill=tk.X, padx=6, pady=(0, 4))
        self.cond_canvas.bind("<Button-1>", self._on_cond_click)
        self.cond_canvas.bind("<Button-3>", self._on_cond_right_click)
        self.cond_canvas.bind("<Motion>", self._on_cond_motion)
        self.cond_canvas.bind("<Leave>", lambda _e: self.cond_hover_var.set(
            "旗/雷限初始位；方位：黄上 / 紫左 / 绿下 / 蓝右；悬停条件格查看格内“或”条件"))

        result_frame = ttk.LabelFrame(
            main, text=self._result_title())
        self.result_frame = result_frame
        result_frame.grid(row=0, column=2, sticky="nsew")
        self.result_canvas = tk.Canvas(
            result_frame, width=self._board_size, height=self._board_size,
            bg="#FFFFFF", highlightthickness=0)
        self.result_canvas.pack(padx=4, pady=(4, 2))
        self.hover_var = tk.StringVar(value="悬停已点亮格子查看前 3 名明细")
        ttk.Label(result_frame, textvariable=self.hover_var, wraplength=460).pack(
            fill=tk.X, padx=6, pady=(0, 4))
        self.result_canvas.bind("<Button-1>", self._on_result_click)
        self.result_canvas.bind("<Motion>", self._on_result_motion)
        self.result_canvas.bind("<Leave>", lambda _e: self.hover_var.set(
            "悬停已点亮格子查看前 3 名明细"))

        self._draw_board_base(self.cond_canvas)
        self._draw_board_base(self.result_canvas)
        self._redraw_condition_board()
        self._redraw_result_board()

    def _bbox(self, row: int, col: int) -> tuple[int, int, int, int]:
        x0 = self.MARGIN + col * self.CELL
        y0 = self.MARGIN + row * self.CELL
        return x0, y0, x0 + self.CELL, y0 + self.CELL

    def _draw_board_base(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        for row in range(17):
            for col in range(17):
                x0, y0, x1, y1 = self._bbox(row, col)
                kind = cell_kind(row, col)
                region = region_of(row, col)
                if kind == ".":
                    canvas.create_rectangle(x0, y0, x1, y1,
                                            fill="#F7F7F7", outline="#F7F7F7")
                    continue
                fill = REGION_FILL.get(region, "#FAFAFA")
                if kind == "C":
                    fill = "#D5D5D5"
                elif kind == "R":
                    fill = "#F1F1F1"
                canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="#D8D8D8")
                if kind == "C":
                    canvas.create_oval(x0 + 5, y0 + 5, x1 - 5, y1 - 5,
                                       fill="#BFBFBF", outline="#9E9E9E")
                elif kind == "H":
                    central = 6 <= row <= 10 and 6 <= col <= 10
                    canvas.create_rectangle(
                        x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                        fill="#FFF8E1" if central else "",
                        outline="#C62828" if not central else "#E0A800",
                        width=2 if not central else 1)
        for orientation, index, start, end in RAIL_LINES:
            if orientation == "h":
                y = self.MARGIN + index * self.CELL + self.CELL / 2
                x1 = self.MARGIN + start * self.CELL + self.CELL / 2
                x2 = self.MARGIN + end * self.CELL + self.CELL / 2
                canvas.create_line(x1, y, x2, y, fill="#9E7B5A", width=2,
                                   capstyle=tk.ROUND)
            else:
                x = self.MARGIN + index * self.CELL + self.CELL / 2
                y1 = self.MARGIN + start * self.CELL + self.CELL / 2
                y2 = self.MARGIN + end * self.CELL + self.CELL / 2
                canvas.create_line(x, y1, x, y2, fill="#9E7B5A", width=2,
                                   capstyle=tk.ROUND)
        border = self.MARGIN - 1
        canvas.create_rectangle(border, border, self._board_size - border,
                                self._board_size - border,
                                outline="#8D6E63", width=2)
        for col in range(17):
            x = self.MARGIN + col * self.CELL + self.CELL / 2
            canvas.create_text(x, self.MARGIN / 2, text=chr(65 + col),
                               font=("Consolas", 6), fill="#666666")
        for row in range(17):
            y = self.MARGIN + row * self.CELL + self.CELL / 2
            canvas.create_text(self.MARGIN / 2, y, text=str(row + 1),
                               font=("Consolas", 6), fill="#666666")

    def _on_piece_press(self, color: str, piece: int, event) -> None:
        self._select_piece(color, piece)
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        tk.Label(
            ghost, text=PIECE_SHORT[piece], bg=COLOR_FILL[color],
            fg=COLOR_TEXT_ON[color], font=("Microsoft YaHei UI", 12, "bold"),
            relief=tk.SOLID, bd=2, padx=4, pady=2,
        ).pack()
        ghost.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")
        ghost.withdraw()
        self._drag = {
            "color": color, "piece": piece, "ghost": ghost,
            "started": False, "sx": event.x_root, "sy": event.y_root,
        }

    def _on_piece_motion(self, event) -> None:
        if not self._drag:
            return
        if not self._drag["started"]:
            if abs(event.x_root - self._drag["sx"]) + abs(event.y_root - self._drag["sy"]) <= 3:
                return
            self._drag["started"] = True
            self._drag["ghost"].deiconify()
        self._drag["ghost"].geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

    def _on_piece_release(self, event) -> None:
        drag = self._drag
        self._drag = None
        if not drag:
            return
        ghost = drag["ghost"]
        started = drag["started"]
        if started:
            ghost.withdraw()
            widget = self.winfo_containing(event.x_root, event.y_root)
            if widget is self.cond_canvas:
                x = event.x_root - self.cond_canvas.winfo_rootx()
                y = event.y_root - self.cond_canvas.winfo_rooty()
                cell = self._cell_from_xy(x, y)
                if cell and self._selected == (drag["color"], drag["piece"]):
                    if cell in self._legal:
                        self._add_condition(cell)
                    else:
                        self.status_var.set("该位置不是该棋子的合法初始位置")
        ghost.destroy()

    def _select_piece(self, color: str, piece: int) -> None:
        for lbl in self._piece_buttons.values():
            lbl.config(relief=tk.RAISED, bd=1, highlightthickness=0)
        self._selected = (color, piece)
        self._piece_buttons[(color, piece)].config(
            relief=tk.SUNKEN, bd=2, highlightthickness=2,
            highlightbackground="#FF6F00")
        self._legal = legal_cells(color, piece)
        self._redraw_condition_board()
        self.status_var.set(
            f"已选中: {color}-{PIECE_SHORT[piece]}；左键添加/取消，右键清空该格")

    def _cell_from_xy(self, x: float, y: float) -> tuple[int, int] | None:
        col = int((x - self.MARGIN) // self.CELL)
        row = int((y - self.MARGIN) // self.CELL)
        if 0 <= row < 17 and 0 <= col < 17:
            return row, col
        return None

    def _on_cond_click(self, event) -> None:
        if self._selected is None:
            self.status_var.set("请先在左侧选择棋子")
            return
        cell = self._cell_from_xy(event.x, event.y)
        if cell is None:
            return
        if cell not in self._legal:
            self.status_var.set("该位置不是该棋子的合法初始位置")
            return
        self._toggle_condition(cell)

    def _on_cond_right_click(self, event) -> None:
        cell = self._cell_from_xy(event.x, event.y)
        if cell is not None and cell in self._conditions:
            del self._conditions[cell]
            self._redraw_condition_board()
            self._redraw_result_board()
            self.status_var.set("已清空该格的全部条件")

    def _toggle_condition(self, cell: tuple[int, int]) -> None:
        if self._selected is None:
            return
        color, piece = self._selected
        alternatives = self._conditions.setdefault(cell, set())
        if (color, piece) in alternatives:
            alternatives.discard((color, piece))
            action = "移除"
        else:
            alternatives.add((color, piece))
            action = "添加"
        if not alternatives:
            del self._conditions[cell]
        self._redraw_condition_board()
        self._redraw_result_board()
        self.status_var.set(
            f"{action}条件: {color}-{PIECE_SHORT[piece]} @ "
            f"{chr(65 + cell[1])}{cell[0] + 1}")

    def _add_condition(self, cell: tuple[int, int]) -> None:
        if self._selected is None:
            return
        color, piece = self._selected
        self._conditions.setdefault(cell, set()).add((color, piece))
        self._redraw_condition_board()
        self._redraw_result_board()
        self.status_var.set(
            f"条件: {color}-{PIECE_SHORT[piece]} @ "
            f"{chr(65 + cell[1])}{cell[0] + 1}")

    def _on_cond_motion(self, event) -> None:
        cell = self._cell_from_xy(event.x, event.y)
        if cell is None:
            self.cond_hover_var.set(
                "旗/雷限初始位；方位：黄上 / 紫左 / 绿下 / 蓝右；悬停条件格查看格内“或”条件")
            return
        row, col = cell
        coord = f"{chr(65 + col)}{row + 1}"
        alternatives = self._conditions.get(cell)
        if not alternatives:
            self.cond_hover_var.set(f"{coord}  空条件格")
            return
        ordered = sorted(
            alternatives,
            key=lambda item: (SEAT_COLORS.index(item[0]), item[1]))
        text = "  或  ".join(
            f"{color}-{PIECE_SHORT[piece]}" for color, piece in ordered)
        self.cond_hover_var.set(f"{coord}  条件: {text}")

    def _redraw_condition_board(self) -> None:
        canvas = self.cond_canvas
        canvas.delete("dyn")
        for row in range(17):
            for col in range(17):
                if not can_hold_piece(row, col):
                    continue
                x0, y0, x1, y1 = self._bbox(row, col)
                legal = self._selected is not None and (row, col) in self._legal
                if legal:
                    canvas.create_rectangle(
                        x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                        fill="#C8E6C9", stipple="gray25",
                        outline="#66BB6A", width=1, tags="dyn")
                else:
                    canvas.create_rectangle(
                        x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                        fill="#BDBDBD", stipple="gray50",
                        outline="", tags="dyn")
        for (row, col), alternatives in self._conditions.items():
            if not alternatives:
                continue
            ordered = sorted(
                alternatives,
                key=lambda item: (SEAT_COLORS.index(item[0]), item[1]))
            x0, y0, x1, y1 = self._bbox(row, col)
            canvas.create_rectangle(
                x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                outline=COLOR_LINE[ordered[0][0]], width=1, tags="dyn")
            if len(ordered) == 1:
                color, piece = ordered[0]
                canvas.create_oval(
                    x0 + 3, y0 + 3, x1 - 3, y1 - 3,
                    fill=COLOR_FILL[color], outline=COLOR_LINE[color], width=2,
                    tags="dyn")
                canvas.create_text(
                    (x0 + x1) / 2, (y0 + y1) / 2, text=PIECE_SHORT[piece],
                    font=("Microsoft YaHei UI", 8, "bold"),
                    fill=COLOR_TEXT_ON[color], tags="dyn")
                continue
            if len(ordered) == 2:
                cy = (y0 + y1) / 2
                for idx, (color, piece) in enumerate(ordered):
                    cx = x0 + 9 if idx == 0 else x1 - 9
                    canvas.create_oval(
                        cx - 7, cy - 7, cx + 7, cy + 7,
                        fill=COLOR_FILL[color], outline=COLOR_LINE[color],
                        width=1, tags="dyn")
                    canvas.create_text(
                        cx, cy, text=PIECE_SHORT[piece],
                        font=("Microsoft YaHei UI", 7, "bold"),
                        fill=COLOR_TEXT_ON[color], tags="dyn")
                continue
            centers = (
                (x0 + 7, y0 + 7), (x1 - 7, y0 + 7),
                (x0 + 7, y1 - 7), (x1 - 7, y1 - 7),
            )
            shown = ordered[:4]
            for idx, (cx, cy) in enumerate(centers):
                if idx >= len(shown):
                    break
                if idx == 3 and len(ordered) > 4:
                    canvas.create_text(
                        cx, cy, text=f"+{len(ordered) - 3}",
                        font=("Microsoft YaHei UI", 5, "bold"),
                        fill="#333333", tags="dyn")
                    continue
                color, piece = shown[idx]
                canvas.create_oval(
                    cx - 5, cy - 5, cx + 5, cy + 5,
                    fill=COLOR_FILL[color], outline=COLOR_LINE[color],
                    width=1, tags="dyn")
                canvas.create_text(
                    cx, cy, text=PIECE_SHORT[piece],
                    font=("Microsoft YaHei UI", 5, "bold"),
                    fill=COLOR_TEXT_ON[color], tags="dyn")

    def _redraw_result_board(self) -> None:
        canvas = self.result_canvas
        canvas.delete("dyn")
        for row in range(17):
            for col in range(17):
                if not can_hold_piece(row, col):
                    continue
                x0, y0, x1, y1 = self._bbox(row, col)
                if (row, col) not in self._active_cells:
                    canvas.create_rectangle(
                        x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                        fill="#FFFFFF", stipple="gray25",
                        outline="", tags="dyn")
        if not self._result_counts or self._match_total <= 0:
            for row, col in self._active_cells:
                if not can_hold_piece(row, col):
                    continue
                x0, y0, x1, y1 = self._bbox(row, col)
                canvas.create_rectangle(
                    x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                    fill="#FFF3C4", stipple="gray25",
                    outline="#FF6F00", width=2, tags="dyn")
            return
        for (row, col) in self._active_cells:
            if not can_hold_piece(row, col):
                continue
            x0, y0, x1, y1 = self._bbox(row, col)
            canvas.create_rectangle(
                x0 + 1, y0 + 1, x1 - 1, y1 - 1,
                fill="#FFF3C4", stipple="gray25",
                outline="#FF6F00", width=2, tags="dyn")
            counts = self._result_counts.get((row, col))
            if not counts:
                canvas.create_text(
                    (x0 + x1) / 2, (y0 + y1) / 2, text="—",
                    font=("Microsoft YaHei UI", 9), fill="#999999", tags="dyn")
                continue
            cell_total = sum(counts.values())
            (color, piece), count = max(
                counts.items(), key=lambda item: (item[1], item[0][0]))
            canvas.create_text(
                (x0 + x1) / 2, (y0 + y1) / 2 - 2,
                text=PIECE_SHORT[piece],
                font=("Microsoft YaHei UI", 10, "bold"),
                fill=COLOR_TEXT[color], tags="dyn")
            width = max(2, (self.CELL - 6) * count / cell_total)
            canvas.create_rectangle(
                x0 + 3, y1 - 6, x1 - 3, y1 - 2,
                fill="#EEEEEE", outline="", tags="dyn")
            canvas.create_rectangle(
                x0 + 3, y1 - 6, x0 + 3 + width, y1 - 2,
                fill=COLOR_FILL[color], outline="", tags="dyn")

    def _on_result_click(self, event) -> None:
        cell = self._cell_from_xy(event.x, event.y)
        if cell is None or not can_hold_piece(*cell):
            return
        if cell in self._active_cells:
            self._active_cells.discard(cell)
            self.status_var.set(
                f"已关闭结果格 {chr(65 + cell[1])}{cell[0] + 1}")
        else:
            self._active_cells.add(cell)
            self.status_var.set(
                f"已点亮结果格 {chr(65 + cell[1])}{cell[0] + 1}，"
                f"共 {len(self._active_cells)} 格")
        if self._stored_result is not None and self._stored_mode == "all":
            self._result_counts = {}
            self._match_total = 0
            self._snapshot_total = 0
            self._stored_result = None
            self._stored_mode = None
            self.status_var.set(
                "点亮格已变化，请重新统计（全部快照模式按点亮区域去重）")
        self._redraw_result_board()

    def _on_result_motion(self, event) -> None:
        cell = self._cell_from_xy(event.x, event.y)
        if cell is None or not can_hold_piece(*cell):
            self.hover_var.set("悬停已点亮格子查看前 3 名明细")
            return
        row, col = cell
        coord = f"{chr(65 + col)}{row + 1}"
        if cell not in self._active_cells:
            self.hover_var.set(f"{coord}  未点亮（左键点亮）")
            return
        counts = self._result_counts.get(cell)
        if not counts:
            self.hover_var.set(f"{coord}  无样本")
            return
        cell_total = sum(counts.values())
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0][0]))[:3]
        show_pct = self._display_mode_getter() == "百分比"
        parts = []
        for (color, piece), count in top:
            if show_pct:
                pct = count / cell_total * 100 if cell_total else 0
                parts.append(f"{color}-{PIECE_SHORT[piece]} {pct:.1f}%")
            else:
                parts.append(f"{color}-{PIECE_SHORT[piece]} {count}")
        self.hover_var.set(f"{coord}  " + "   ".join(parts))

    def _result_title(self) -> str:
        if self.first_only_var.get():
            return "结果棋盘（首次满足时；左键点亮）"
        return "结果棋盘（全部满足快照；连续同态去重）"

    def _on_first_only_change(self) -> None:
        self.result_frame.config(text=self._result_title())
        self._stored_mode = None
        if self._result_counts or self._stored_result:
            self._result_counts = {}
            self._match_total = 0
            self._snapshot_total = 0
            self._stored_result = None
            self._stored_mode = None
            self._redraw_result_board()
            self.hover_var.set("悬停已点亮格子查看前 3 名明细")
            self.status_var.set("统计模式已改变，请重新统计")

    @staticmethod
    def _conditions_match(board, conditions) -> bool:
        for cell, alternatives in conditions.items():
            placed = board.get(cell)
            if placed is None:
                return False
            if (placed.color, placed.piece) not in alternatives:
                return False
        return True

    def _start_stat(self) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return
        if not self._conditions:
            messagebox.showwarning("提示", "请在条件棋盘上放置至少一个条件", parent=self._top)
            return
        if not self._active_cells:
            messagebox.showwarning("提示", "请先在结果棋盘上左键点亮至少一个格子", parent=self._top)
            return

        self.stat_btn.config(state=tk.DISABLED)
        self._result_counts = {}
        self._match_total = 0
        self._snapshot_total = 0
        self._stored_result = None
        self._stored_mode = None
        self.result_frame.config(text=self._result_title())
        self._redraw_result_board()
        self.hover_var.set("悬停已点亮格子查看前 3 名明细")
        self.status_var.set("统计中……")
        self._progress_queue = queue.Queue()
        conditions = {
            cell: set(alternatives)
            for cell, alternatives in self._conditions.items()
        }
        active_cells = tuple(sorted(self._active_cells))
        active_set = set(active_cells)
        first_only = bool(self.first_only_var.get())
        mode = "first" if first_only else "all"

        def worker():
            from src.layout_stats import find_jgs_files
            files = find_jgs_files(folder)
            total = len(files)
            counts: dict[tuple[int, int], dict[tuple[str, int], int]] = {}
            match_total = 0
            snapshot_total = 0
            failed = 0
            invalid = 0
            orient_skipped = 0

            def state_key(state) -> tuple:
                values = []
                for cell in active_cells:
                    placed = state.get(cell)
                    values.append(
                        (placed.color, placed.piece) if placed else None)
                return tuple(values)

            try:
                for index, jgs_file in enumerate(files, 1):
                    try:
                        replay = parse_jgs(jgs_file)
                        if (len(replay.blocks) != 4
                                or not all(layout_is_valid(b.matrix)
                                           for b in replay.blocks)):
                            invalid += 1
                        else:
                            times = rotation_times(replay.orient)
                            board = normalize_board(replay.board, replay.orient)
                            if times is None or board is None:
                                orient_skipped += 1
                            else:
                                counted_here = False
                                last_key = None
                                for _step_no, state in iter_states(
                                        replay, board, times):
                                    if not self._conditions_match(state, conditions):
                                        last_key = None
                                        continue
                                    if not first_only:
                                        key = state_key(state)
                                        if key == last_key:
                                            continue
                                        last_key = key
                                    counted_here = True
                                    snapshot_total += 1
                                    for cell, placed in state.items():
                                        if not can_hold_piece(*cell):
                                            continue
                                        if not first_only and cell not in active_set:
                                            continue
                                        bucket = counts.setdefault(cell, {})
                                        key_piece = (placed.color, placed.piece)
                                        bucket[key_piece] = bucket.get(key_piece, 0) + 1
                                    if first_only:
                                        break
                                if counted_here:
                                    match_total += 1
                    except Exception:
                        failed += 1
                    self._progress_queue.put((index, total))
                valid_total = total - invalid - failed - orient_skipped
                missed = max(0, valid_total - match_total)
                self._stat_result = (
                    counts, match_total, snapshot_total, total, failed, invalid,
                    orient_skipped, missed, mode)
            except Exception as exc:
                self._stat_result = (
                    counts, match_total, snapshot_total, total, failed, invalid,
                    orient_skipped, str(exc), mode)

        self._stat_result = None
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _poll_worker(self) -> None:
        while True:
            try:
                done, total = self._progress_queue.get_nowait()
            except queue.Empty:
                break
            self.status_var.set(f"统计中…… {done}/{total}")

        if self._worker is not None and self._worker.is_alive():
            self.after(100, self._poll_worker)
            return

        self.stat_btn.config(state=tk.NORMAL)
        result = self._stat_result
        if result is None:
            self.status_var.set("统计失败")
            return
        counts, match_total, snapshot_total, total, failed, invalid, orient_skipped, missed, mode = result
        if isinstance(failed, str):
            self.status_var.set(f"统计失败: {failed}")
            messagebox.showerror("统计失败", failed, parent=self._top)
            return
        self._result_counts = counts
        self._match_total = match_total
        self._snapshot_total = snapshot_total
        self._stored_mode = mode
        if match_total == 0:
            self._stored_result = None
            self._redraw_result_board()
            self.status_var.set(
                f"没有复盘满足条件 (总数 {total}，未命中 {missed}，布局异常 {invalid}，"
                f"解析失败 {failed}，方位无法归一 {orient_skipped})")
            return
        self._stored_result = (counts, match_total, total)
        self._redraw_result_board()
        valid_total = total - invalid - failed - orient_skipped
        ratio = match_total / valid_total * 100 if valid_total else 0
        if mode == "first":
            self.status_var.set(
                f"命中复盘: {match_total}/{valid_total} ({ratio:.1f}%)  "
                f"未命中 {missed}  布局异常 {invalid}  解析失败 {failed}  "
                f"方位无法归一 {orient_skipped}")
        else:
            self.status_var.set(
                f"快照: {snapshot_total}（来自 {match_total}/{valid_total} 个复盘，"
                f"连续同态已去重）  未命中 {missed}  布局异常 {invalid}  "
                f"解析失败 {failed}  方位无法归一 {orient_skipped}")

    def _clear_all(self) -> None:
        self._conditions.clear()
        self._active_cells.clear()
        self._selected = None
        self._legal = set()
        self._result_counts = {}
        self._match_total = 0
        self._snapshot_total = 0
        self._stored_result = None
        self._stored_mode = None
        for lbl in self._piece_buttons.values():
            lbl.config(relief=tk.RAISED, bd=1, highlightthickness=0)
        self._redraw_condition_board()
        self._redraw_result_board()
        self.cond_hover_var.set(
            "旗/雷限初始位；方位：黄上 / 紫左 / 绿下 / 蓝右；悬停条件格查看格内“或”条件")
        self.hover_var.set("悬停已点亮格子查看前 3 名明细")
        self.status_var.set(
            "拖拽或点击左侧棋子；可配置全盘，旗/雷限初始位；结果格需左键点亮")

    def clear_stats(self) -> None:
        self._clear_all()

    def _refresh_grid(self) -> None:
        if self._stored_result:
            self._redraw_result_board()


class QQQuickFilterPanel(ttk.Frame):
    """QQ 复盘常用模式一键筛选（当前第一套：司令/军长被空炸复盘筛选）"""

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar):
        super().__init__(parent, padding=2)
        self._folder_var = folder_var
        self._source_var = source_var
        self._worker = None
        self._scan_result = None
        self._row_paths: dict[str, str] = {}
        self.mode_var = tk.StringVar(value="none")
        self.status_var = tk.StringVar(
            value="选择文件夹后点击筛选按钮；四家中任一家命中即计入该局")
        self._build_ui()

    @property
    def _top(self):
        return self.winfo_toplevel()

    def _build_ui(self) -> None:
        top = ttk.LabelFrame(self, text="第一套 · 司令/军长被空炸复盘筛选")
        top.pack(fill=tk.X, padx=2, pady=(0, 4))

        row = ttk.Frame(top)
        row.pack(fill=tk.X, padx=6, pady=(4, 2))
        ttk.Label(row, text="命中条件:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            row, text="什么都没吃", value="none",
            variable=self.mode_var).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(
            row, text="最大仅吃到连长", value="max_lian",
            variable=self.mode_var).pack(side=tk.LEFT, padx=(12, 0))
        self.scan_btn = ttk.Button(
            row, text="司令/军长被空炸复盘筛选", command=self._start_scan)
        self.scan_btn.pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(row, text="清空结果", command=self.clear_stats).pack(
            side=tk.LEFT, padx=(6, 0))

        ttk.Label(
            top,
            text="追踪四家司令/军长的全局吃子记录（含反吃），凡被对手炸弹换掉"
                 "且满足所选条件即命中；“最大仅吃到连长”指击杀仅限连长/排长/工兵"
                 "（含什么都没吃）。复盘范围随全局平台选择（QQ .jgs / 联众 .jqh）。"
                 "双击结果行打开复盘（等同资源管理器双击），右键复制完整地址。",
            wraplength=760).pack(fill=tk.X, padx=6, pady=(0, 4))

        result = ttk.LabelFrame(
            self, text="命中结果（每行一局；一局多条事件时格内多行显示）")
        result.pack(fill=tk.BOTH, expand=True, padx=2)

        cols = ("file", "seat", "player", "piece", "kills", "step", "kind",
                "verdict", "path")
        heads = ("文件", "方位", "玩家", "棋子", "击杀记录", "阵亡步",
                 "空炸类型", "判定", "详细地址")
        widths = (170, 42, 112, 46, 118, 60, 130, 190, 240)
        style = ttk.Style(self)
        style.configure("Quick.Treeview", font=("Microsoft YaHei UI", 8),
                        rowheight=22)
        style.configure("Quick.Treeview.Heading",
                        font=("Microsoft YaHei UI", 9, "bold"))
        self.tree = ttk.Treeview(
            result, columns=cols, show="headings", height=14,
            style="Quick.Treeview")
        for col, head, width in zip(cols, heads, widths):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, anchor="w",
                             stretch=(col == "path"))
        vsb = ttk.Scrollbar(result, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        vsb.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 4), pady=4)
        self.tree.bind("<Double-Button-1>", self._on_tree_open)
        self.tree.bind("<Button-3>", self._on_tree_context)

        ttk.Label(self, textvariable=self.status_var).pack(
            fill=tk.X, pady=(2, 0))

    def _start_scan(self) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return
        self.scan_btn.config(state=tk.DISABLED)
        self.status_var.set("统计中……")
        mode = self.mode_var.get()
        is_lianzhong = self._source_var.get() == "联众"
        progress = [0]
        total_ref = [0]

        def worker():
            try:
                rows = []
                failed = 0
                if is_lianzhong:
                    from src.lianzhong import find_jqh_empty_bomb_victims
                    files = sorted(glob.glob(
                        os.path.join(folder, "**", "*.jqh"), recursive=True))
                    total_ref[0] = len(files)
                    for jqh_file in files:
                        try:
                            raw = open(jqh_file, "rb").read()
                            for rec in find_jqh_empty_bomb_victims(raw, mode):
                                rows.append((jqh_file, rec))
                        except Exception:
                            failed += 1
                        progress[0] += 1
                else:
                    from src.layout_stats import find_jgs_files
                    from src.qq_replay import find_empty_bomb_victims
                    files = find_jgs_files(folder)
                    total_ref[0] = len(files)
                    for jgs_file in files:
                        try:
                            rep = parse_jgs(jgs_file)
                            for rec in find_empty_bomb_victims(rep, mode):
                                rows.append((jgs_file, rec))
                        except Exception:
                            failed += 1
                        progress[0] += 1
                self._scan_result = (rows, total_ref[0], failed, mode, None)
            except Exception as e:
                self._scan_result = ([], 0, 0, mode, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._progress_timer = self.after(
            200, self._update_progress, progress, total_ref)
        self.after(100, self._poll_worker)

    def _update_progress(self, progress, total_ref) -> None:
        if self._worker is not None and self._worker.is_alive():
            self.status_var.set(f"统计中…… {progress[0]}/{total_ref[0]}")
            self._progress_timer = self.after(
                200, self._update_progress, progress, total_ref)

    def _poll_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self.after(100, self._poll_worker)
            return
        if not hasattr(self, "scan_btn") or not self.scan_btn.winfo_exists():
            return
        self.scan_btn.config(state=tk.NORMAL)
        if self._scan_result is None:
            return
        rows, total, failed, mode, err = self._scan_result
        if err:
            self.status_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err, parent=self._top)
            return

        self.tree.delete(*self.tree.get_children())
        self._row_paths.clear()
        grouped: dict[str, list] = {}
        order: list[str] = []
        for full_path, rec in rows:
            if full_path not in grouped:
                grouped[full_path] = []
                order.append(full_path)
            grouped[full_path].append(rec)
        max_lines = 1
        for full_path in order:
            recs = grouped[full_path]
            max_lines = max(max_lines, len(recs))
            kills_lines = "\n".join(
                "、".join(r["kills"]) if r["kills"] else "（无）" for r in recs)
            kind_lines = "\n".join(r.get("kind") or "—" for r in recs)
            verdict_lines = "\n".join(
                ("空炸司令/军长（什么都没吃）" if r["empty"]
                 else "空炸司令/军长（最大仅吃到连长）") for r in recs)
            iid = self.tree.insert("", tk.END, values=(
                os.path.basename(full_path),
                "\n".join(r["seat"] for r in recs),
                "\n".join(r["player"] for r in recs),
                "\n".join(r["piece"] for r in recs),
                kills_lines,
                "\n".join(str(r["death_step"]) for r in recs),
                kind_lines,
                verdict_lines,
                full_path))
            self._row_paths[iid] = full_path
        # 行高随本批最大事件数适配(每行内容纵向居中, 各列逐行对齐)
        ttk.Style(self).configure(
            "Quick.Treeview", rowheight=max(22, max_lines * 17 + 6))
        hit_files = {os.path.basename(p) for p in grouped}
        n_empty = sum(1 for recs in grouped.values() for r in recs if r["empty"])

        mode_text = "什么都没吃" if mode == "none" else "最大仅吃到连长"
        platform_text = "联众" if self._source_var.get() == "联众" else "QQ"
        status = (f"范围[{platform_text}] 模式[{mode_text}] 扫描 {total} 局，"
                  f"命中 {len(hit_files)} 局"
                  f" / {len(rows)} 条记录（什么都没吃 {n_empty} 条 ·"
                  f"最大仅吃到连长 {len(rows) - n_empty} 条）")
        if failed:
            status += f"；解析失败 {failed} 个"
        self.status_var.set(status)

    def clear_stats(self) -> None:
        if hasattr(self, "tree") and self.tree.winfo_exists():
            self.tree.delete(*self.tree.get_children())
        self._row_paths.clear()
        self.status_var.set("选择文件夹后点击筛选按钮；四家中任一家命中即计入该局")
        self._scan_result = None

    def _on_tree_open(self, event) -> None:
        """双击结果行打开复盘(等同资源管理器下双击打开/运行)。"""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        path = self._row_paths.get(iid)
        if not path:
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("错误", f"无法打开复盘: {e}", parent=self._top)

    def _on_tree_context(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制完整地址",
                         command=lambda: self._copy_path(iid))
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_path(self, iid: str) -> None:
        path = self._row_paths.get(iid, "")
        if not path:
            return
        self.clipboard_clear()
        self.clipboard_append(path)
        self.status_var.set(f"已复制完整地址: {path}")


class GeneralFilterPanel(QQBatchPanel):
    """通用筛选条件统计: 条件棋盘/结果棋盘统计, QQ(.jgs) 与联众(.jqh) 双平台。
    思路参考 QQ复盘批量统计(QQBatchPanel) + 联众复盘解析(lianzhong):
    条件与交互完全复用 QQ 面板, 复盘范围由全局 平台 radio 切换。
    条件色为 QQ 规范色(黄上/紫左/绿下/蓝右), 联众复盘按同座位映射
    (蓝上/绿左/红下/黑右) 套用同一条件。
    """

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar,
                 display_mode_getter=None):
        super().__init__(parent, folder_var, display_mode_getter)
        self._source_var = source_var
        self.cond_hover_var.set(
            "旗/雷限初始位；方位：黄上 / 紫左 / 绿下 / 蓝右"
            "（联众同座位：蓝上/绿左/红下/黑右）；悬停条件格查看格内“或”条件")
        self.status_var.set(
            "拖拽或点击左侧棋子；可配置全盘，旗/雷限初始位；结果格需左键点亮")

    def _start_stat(self) -> None:
        if self._source_var.get() != "联众":
            super()._start_stat()
            return
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹", parent=self._top)
            return
        if not self._conditions:
            messagebox.showwarning("提示", "请在条件棋盘上放置至少一个条件", parent=self._top)
            return
        if not self._active_cells:
            messagebox.showwarning("提示", "请先在结果棋盘上左键点亮至少一个格子", parent=self._top)
            return

        self.stat_btn.config(state=tk.DISABLED)
        self._result_counts = {}
        self._match_total = 0
        self._snapshot_total = 0
        self._stored_result = None
        self._stored_mode = None
        self.result_frame.config(text=self._result_title())
        self._redraw_result_board()
        self.hover_var.set("悬停已点亮格子查看前 3 名明细")
        self.status_var.set("统计中……")
        self._progress_queue = queue.Queue()
        conditions = {
            cell: set(alternatives)
            for cell, alternatives in self._conditions.items()
        }
        active_cells = tuple(sorted(self._active_cells))
        active_set = set(active_cells)
        first_only = bool(self.first_only_var.get())
        mode = "first" if first_only else "all"

        def worker():
            from src.lianzhong import iter_jqh_states
            files = sorted(glob.glob(
                os.path.join(folder, "**", "*.jqh"), recursive=True))
            total = len(files)
            counts: dict[tuple[int, int], dict[tuple[str, int], int]] = {}
            match_total = 0
            snapshot_total = 0
            failed = 0
            invalid = 0

            def state_key(state) -> tuple:
                values = []
                for cell in active_cells:
                    placed = state.get(cell)
                    values.append((placed[0], placed[1]) if placed else None)
                return tuple(values)

            def conditions_match(state) -> bool:
                for cell, alternatives in conditions.items():
                    placed = state.get(cell)
                    if placed is None or (placed[0], placed[1]) not in alternatives:
                        return False
                return True

            try:
                for index, jqh_file in enumerate(files, 1):
                    try:
                        raw = open(jqh_file, "rb").read()
                        counted_here = False
                        last_key = None
                        is_initial = True
                        for _step_no, state in iter_jqh_states(raw):
                            if is_initial:
                                is_initial = False
                                if len(state) != 100:
                                    invalid += 1
                                    break
                            if not conditions_match(state):
                                last_key = None
                                continue
                            if not first_only:
                                key = state_key(state)
                                if key == last_key:
                                    continue
                                last_key = key
                            counted_here = True
                            snapshot_total += 1
                            for cell, placed in state.items():
                                if not can_hold_piece(*cell):
                                    continue
                                if not first_only and cell not in active_set:
                                    continue
                                bucket = counts.setdefault(cell, {})
                                key_piece = (placed[0], placed[1])
                                bucket[key_piece] = bucket.get(key_piece, 0) + 1
                            if first_only:
                                break
                        if counted_here:
                            match_total += 1
                    except Exception:
                        failed += 1
                    self._progress_queue.put((index, total))
                valid_total = total - invalid - failed
                missed = max(0, valid_total - match_total)
                self._stat_result = (
                    counts, match_total, snapshot_total, total, failed, invalid,
                    0, missed, mode)
            except Exception as exc:
                self._stat_result = (
                    counts, match_total, snapshot_total, total, str(exc),
                    invalid, 0, 0, mode)

        self._stat_result = None
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)


class GLReplayPanel(ttk.Frame):
    """联众 .JQH.TXT 文本复盘完整解析：玩家、棋盘、行棋与子力统计"""

    CELL = 28
    MARGIN = 18
    PLAYER_FILL = {
        "blue": "#4F86C6", "red": "#D9534F",
        "green": "#5FA85F", "gray": "#8A8A8A",
    }
    PLAYER_TEXT = {
        "blue": "#FFFFFF", "red": "#FFFFFF",
        "green": "#FFFFFF", "gray": "#FFFFFF",
    }
    REGION_FILL = {
        "top": "#E3F0FA", "bottom": "#FFE6E6",
        "left": "#E6F4E3", "right": "#EFEFEF",
    }

    def __init__(self, parent):
        super().__init__(parent)
        self.replay = None
        self.analysis = None
        self.file_var = tk.StringVar()
        self.status_var = tk.StringVar(value="请选择联众 .JQH.TXT 文本复盘")
        self.stats_summary_var = tk.StringVar(value="")
        self._build_ui()

    @property
    def _top(self):
        return self.winfo_toplevel()

    @property
    def _board_size(self) -> int:
        return self.MARGIN * 2 + 17 * self.CELL

    def _build_ui(self) -> None:
        bar = ttk.Frame(self, padding=(0, 0, 0, 6))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="选择复盘...", command=self._choose_file).pack(side=tk.LEFT)
        ttk.Label(bar, text="文件:").pack(side=tk.LEFT, padx=(6, 0))
        entry = ttk.Entry(bar, textvariable=self.file_var, width=62)
        entry.pack(side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._parse_current())
        ttk.Button(bar, text="清空", command=self._clear).pack(side=tk.RIGHT)
        self.export_btn = ttk.Button(
            bar, text="导出 JSON", command=self._export_json, state=tk.DISABLED)
        self.export_btn.pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(bar, text="解析", command=self._parse_current).pack(side=tk.RIGHT)

        ttk.Label(self, textvariable=self.status_var, padding=(2, 2, 2, 0)).pack(
            fill=tk.X, side=tk.BOTTOM)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self._build_overview_tab()
        self._build_board_tab()
        self._build_moves_tab()
        self._build_stats_tab()

    def _build_overview_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="对局概览")
        self.overview_text = tk.Text(
            frame, wrap=tk.WORD, font=("Microsoft YaHei UI", 10),
            state=tk.DISABLED, padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.overview_text.yview)
        self.overview_text.configure(yscrollcommand=scroll.set)
        self.overview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.overview_text.tag_configure("heading", font=("Microsoft YaHei UI", 11, "bold"))
        self.overview_text.tag_configure("warn", foreground="#B00000")

    def _build_board_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="初始棋盘")
        self.board_canvas = tk.Canvas(
            frame, width=self._board_size, height=self._board_size,
            bg="#FFFFFF", highlightthickness=0)
        self.board_canvas.pack(padx=8, pady=8)
        ttk.Label(
            frame, text="颜色：蓝=上家  红=下家  绿=左家  灰=右家",
            padding=(8, 0, 8, 8)).pack(anchor=tk.W)

    def _build_moves_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="行棋记录")
        columns = ("no", "type", "start", "end", "turn", "player", "result", "detail", "time")
        self.moves_tree = ttk.Treeview(frame, columns=columns, show="headings")
        titles = {
            "no": ("序号", 55), "type": ("类型", 70), "start": ("起点", 80),
            "end": ("终点", 80), "turn": ("回合方", 70), "player": ("实际方", 70),
            "result": ("结果", 100), "detail": ("交手", 300), "time": ("时间", 90),
        }
        for key, (title, width) in titles.items():
            self.moves_tree.heading(key, text=title)
            self.moves_tree.column(key, width=width, anchor=tk.CENTER,
                                   stretch=(key == "detail"))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.moves_tree.yview)
        self.moves_tree.configure(yscrollcommand=scroll.set)
        self.moves_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.moves_tree.tag_configure("event", background="#FFF6D8")
        self.moves_tree.tag_configure("error", background="#FFE0E0")

    def _build_stats_tab(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="子力统计")
        ttk.Label(
            frame, textvariable=self.stats_summary_var,
            font=("Microsoft YaHei UI", 10, "bold"), padding=(8, 8, 8, 4),
        ).pack(fill=tk.X)
        columns = (
            "player", "seat", "moves", "attacks", "captures", "losses",
            "mutual", "bomb_kills", "bomb_losses", "mine_kills", "mine_losses",
            "gained", "lost", "net", "rating",
        )
        titles = {
            "player": ("玩家", 150), "seat": ("座位", 50), "moves": ("步数", 50),
            "attacks": ("交手", 50), "captures": ("吃子", 50), "losses": ("被吃", 50),
            "mutual": ("同尽", 50), "bomb_kills": ("炸杀", 50),
            "bomb_losses": ("被炸", 50), "mine_kills": ("挖雷", 50),
            "mine_losses": ("触雷", 50), "gained": ("得分", 60),
            "lost": ("损失", 60), "net": ("净值", 60), "rating": ("评级", 50),
        }
        self.stats_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, (title, width) in titles.items():
            self.stats_tree.heading(key, text=title)
            self.stats_tree.column(key, width=width, anchor=tk.CENTER,
                                   stretch=(key == "player"))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.stats_tree.yview)
        self.stats_tree.configure(yscrollcommand=scroll.set)
        self.stats_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _choose_file(self) -> None:
        top = self._top
        geo = top.geometry()
        path = filedialog.askopenfilename(
            title="选择联众文本复盘", parent=top,
            filetypes=[("联众文本复盘", "*.TXT"), ("文本文件", "*.txt"),
                       ("所有文件", "*.*")])
        top.geometry(geo)
        if path:
            self.file_var.set(path)
            self._parse_current()

    def _parse_current(self) -> None:
        path = self.file_var.get().strip()
        if not path:
            messagebox.showwarning("提示", "请先选择联众文本复盘", parent=self._top)
            return
        try:
            replay = parse_glworld_text(path)
            analysis = replay.analyze()
        except Exception as exc:
            self.replay = None
            self.analysis = None
            self.export_btn.config(state=tk.DISABLED)
            self._refresh_overview()
            self._refresh_board()
            self._refresh_moves()
            self._refresh_stats()
            self.status_var.set(f"解析失败: {exc}")
            messagebox.showerror("解析失败", str(exc), parent=self._top)
            return
        self._set_replay(replay, analysis)

    def _set_replay(self, replay, analysis) -> None:
        self.replay = replay
        self.analysis = analysis
        self.file_var.set(replay.path)
        self.export_btn.config(state=tk.NORMAL)
        self._refresh_overview()
        self._refresh_board()
        self._refresh_moves()
        self._refresh_stats()
        self.status_var.set(
            f"解析完成: {Path(replay.path).name}  行棋 {len(replay.moves)}  "
            f"棋子 {replay.piece_count}  胜方 "
            f"{'、'.join(GL_COLOR_CN[c] for c in analysis.winner) or '和局'}")

    def _clear(self) -> None:
        self.replay = None
        self.analysis = None
        self.file_var.set("")
        self.export_btn.config(state=tk.DISABLED)
        self.status_var.set("请选择联众 .JQH.TXT 文本复盘")
        self._refresh_overview()
        self._refresh_board()
        self._refresh_moves()
        self._refresh_stats()

    def _refresh_overview(self) -> None:
        widget = self.overview_text
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if self.replay is None or self.analysis is None:
            widget.config(state=tk.DISABLED)
            return
        replay = self.replay
        analysis = self.analysis
        lines = [
            ("heading", "基础信息\n"),
            ("", f"文件: {replay.path}\n"),
            ("", f"行棋数: {len(replay.moves)}    棋子数: {replay.piece_count}\n"),
            ("", f"先手方: {GL_COLOR_CN.get(replay.first_move_color, '未知')}\n"),
        ]
        for key, value in replay.game_info.items():
            lines.append(("", f"{key}: {value}\n"))
        lines.append(("heading", "\n四位玩家\n"))
        for color in GL_COLOR_ORDER:
            player = replay.players.get(color)
            stats = analysis.stats.get(color)
            if not player or not stats:
                continue
            lines.append((
                "", f"{GL_COLOR_CN[color]} · {player.seat}家 · {player.name}  "
                    f"步数 {stats.moves}  净值 {stats.net_value:+d}  评级 {stats.rating}\n"))
        lines.append(("", f"\n结果: {'、'.join(GL_COLOR_CN[c] for c in analysis.winner) or '和局'}\n"))
        if analysis.result.losers:
            lines.append(("", "负方: " + "、".join(
                GL_COLOR_CN.get(c, c) for c in analysis.result.losers) + "\n"))
        if analysis.key_events:
            lines.append(("heading", "\n关键事件\n"))
            for event in analysis.key_events[:30]:
                lines.append(("", f"第 {event['step']} 步  "
                                  f"{event.get('category', '')}  {event.get('text', '')}\n"))
            if len(analysis.key_events) > 30:
                lines.append(("", f"其余 {len(analysis.key_events) - 30} 条已省略\n"))
        for tag, text in lines:
            widget.insert(tk.END, text, tag if tag else ())
        widget.config(state=tk.DISABLED)

    def _refresh_board(self) -> None:
        canvas = self.board_canvas
        canvas.delete("all")
        if self.replay is None:
            return
        cell = self.CELL
        margin = self.MARGIN
        for row in range(17):
            for col in range(17):
                x0 = margin + col * cell
                y0 = margin + row * cell
                data = self.replay.board[row][col]
                if data is None:
                    canvas.create_rectangle(
                        x0, y0, x0 + cell, y0 + cell,
                        fill="#F7F7F7", outline="#E0E0E0")
                    continue
                region = self._quadrant(row, col)
                fill = self.REGION_FILL.get(region, "#FAFAFA")
                if data.type == "camp":
                    fill = "#D5D5D5"
                canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                        fill=fill, outline="#D0D0D0")
                if data.type == "camp":
                    canvas.create_oval(
                        x0 + 5, y0 + 5, x0 + cell - 5, y0 + cell - 5,
                        fill="#BFBFBF", outline="#9E9E9E")
                elif data.type == "hq":
                    canvas.create_rectangle(
                        x0 + 1, y0 + 1, x0 + cell - 1, y0 + cell - 1,
                        outline="#C62828", width=2)
                if data.piece is not None:
                    color = data.piece.player if data.piece.player in self.PLAYER_FILL else "gray"
                    canvas.create_oval(
                        x0 + 4, y0 + 4, x0 + cell - 4, y0 + cell - 4,
                        fill=self.PLAYER_FILL[color],
                        outline=self.PLAYER_FILL[color])
                    canvas.create_text(
                        x0 + cell / 2, y0 + cell / 2,
                        text=data.piece.char,
                        font=("Microsoft YaHei UI", 10, "bold"),
                        fill=self.PLAYER_TEXT[color])
        border = margin - 1
        canvas.create_rectangle(
            border, border, self._board_size - border, self._board_size - border,
            outline="#8D6E63", width=2)
        for col in range(17):
            x = margin + col * cell + cell / 2
            canvas.create_text(x, margin / 2, text=str(col + 1),
                               font=("Consolas", 6), fill="#666666")
        for row in range(17):
            y = margin + row * cell + cell / 2
            canvas.create_text(margin / 2, y, text=GL_ROW_LETTERS[row],
                               font=("Consolas", 7), fill="#666666")

    @staticmethod
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

    def _refresh_moves(self) -> None:
        self._clear_tree(self.moves_tree)
        if self.replay is None:
            return
        result_text = {
            "move": "移动", "invalid": "无效", "pass": "Pass", "giveup": "GiveUp",
            "attacker_wins": "吃子", "defender_wins": "被吃",
            "both_die": "同尽", "flag_captured": "扛旗",
        }
        for move in self.replay.moves:
            if move.kind in ("pass", "giveup"):
                values = (move.num, move.display_type, "", "", move.turn_player or "",
                          move.player or "", result_text.get(move.result, move.result),
                          "", f"{move.time1}-{move.time2}")
                self.moves_tree.insert("", tk.END, values=values, tags=("event",))
                continue
            detail = ""
            if move.combat:
                attacker = move.combat["attacker"]
                defender = move.combat["defender"]
                detail = (f"攻 {attacker['char']}({GL_COLOR_CN.get(attacker['player'], '?')})"
                          f" vs 守 {defender['char']}({GL_COLOR_CN.get(defender['player'], '?')})")
            values = (
                move.num, move.display_type,
                f"{move.start[0]},{move.start[1]}" if move.start else "",
                f"{move.end[0]},{move.end[1]}" if move.end else "",
                move.turn_player or "", move.player or "",
                result_text.get(move.result, move.result), detail,
                f"{move.time1}-{move.time2}",
            )
            tags = ("event",) if move.combat else ()
            if move.result == "invalid":
                tags = ("error",)
            self.moves_tree.insert("", tk.END, values=values, tags=tags)
        self.moves_tree.yview_moveto(0)

    def _refresh_stats(self) -> None:
        self._clear_tree(self.stats_tree)
        if self.replay is None or self.analysis is None:
            self.stats_summary_var.set("")
            return
        winners = "、".join(GL_COLOR_CN[c] for c in self.analysis.winner) or "和局"
        self.stats_summary_var.set(f"结果: {winners}    行棋: {len(self.replay.moves)}")
        for color in GL_COLOR_ORDER:
            player = self.replay.players.get(color)
            st = self.analysis.stats.get(color)
            if not player or not st:
                continue
            values = (
                f"{GL_COLOR_CN[color]} {player.name}", player.seat, st.moves,
                st.attacks, st.captures, st.losses, st.mutual_destruct,
                st.bomb_kills, st.bomb_losses, st.mine_kills, st.mine_losses,
                st.value_gained, st.value_lost, st.net_value, st.rating,
            )
            self.stats_tree.insert("", tk.END, values=values)

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _export_json(self) -> None:
        if self.replay is None:
            return
        top = self._top
        geo = top.geometry()
        path = filedialog.asksaveasfilename(
            title="导出联众复盘解析", parent=top, defaultextension=".json",
            initialfile=f"{Path(self.replay.path).stem}_解析.json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        top.geometry(geo)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.replay.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc), parent=top)
            return
        messagebox.showinfo("完成", f"已导出: {path}", parent=top)


class StatsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("军棋复盘布阵统计")

        # 设置窗口默认大小 (宽度994, 高度812 = 933 - 121灰边)
        self.root.geometry("1230x812")

        self._build_folder_bar()
        self._build_tabs()

        # 文件夹变化时清空所有统计结果
        self.folder_var.trace_add("write", lambda *_: self._clear_all_stats())

    def _build_folder_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill=tk.X)

        # 第一行: 文件夹
        row1 = ttk.Frame(bar)
        row1.pack(fill=tk.X)
        ttk.Button(row1, text="浏览...", command=self._choose_folder).pack(side=tk.LEFT)
        ttk.Label(row1, text="文件夹:").pack(side=tk.LEFT, padx=(4, 0))
        self.folder_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.folder_var, width=52).pack(
            side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)

        # 第二行: 平台 + 方向
        row2 = ttk.Frame(bar)
        row2.pack(fill=tk.X, pady=(4, 0))

        # 平台: 联众/QQ 二选一
        ttk.Label(row2, text="平台:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="QQ")
        for name in ("QQ", "联众"):
            ttk.Radiobutton(row2, text=name, value=name, variable=self.source_var,
                            command=self._on_source_change).pack(side=tk.LEFT, padx=(4, 0))

        # 行棋习惯: 三选一
        ttk.Label(row2, text="  方向:").pack(side=tk.LEFT)
        self.direction_var = tk.StringVar(value="逆时针")
        for name in ("逆时针", "顺时针", "主线（旗侧）视角"):
            ttk.Radiobutton(row2, text=name, value=name, variable=self.direction_var,
                            command=self._on_direction_change).pack(side=tk.LEFT, padx=(4, 0))

        # 频率显示模式: 百分比/数量
        ttk.Label(row2, text="  显示:").pack(side=tk.LEFT)
        self.display_mode_var = tk.StringVar(value="百分比")
        for name in ("百分比", "数量"):
            ttk.Radiobutton(row2, text=name, value=name, variable=self.display_mode_var,
                            command=self._on_display_mode_change).pack(side=tk.LEFT, padx=(4, 0))

        # 去重勾选框 (默认开启)
        self.dedup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="去除重复布阵", variable=self.dedup_var,
                        command=self._on_dedup_change).pack(side=tk.LEFT, padx=(16, 0))

    def _is_clockwise(self) -> bool:
        """QQ模式: 顺时针=True(镜像), 逆时针=False(不镜像)"""
        return self.direction_var.get() == "顺时针"

    def _is_mainline(self) -> bool:
        return self.direction_var.get() == "主线（旗侧）视角"

    def _should_mirror(self) -> bool:
        """根据来源模式和方向决定是否镜像
        主线模式永远不镜像(内部已处理镜像合并)"""
        if self._is_mainline():
            return False
        is_lianzhong = self.source_var.get() == "联众"
        if is_lianzhong:
            return self.direction_var.get() == "逆时针"
        return self.direction_var.get() == "顺时针"

    def _on_source_change(self) -> None:
        """切换联众/QQ模式"""
        is_lianzhong = self.source_var.get() == "联众"

        # 记住当前选中的tab索引
        current_idx = self.notebook.index(self.notebook.select())

        # 切换默认方向: 如果当前是主线模式则保留, 否则设默认
        if not self._is_mainline():
            if is_lianzhong:
                self.direction_var.set("顺时针")
            else:
                self.direction_var.set("逆时针")

        # 显示/隐藏平台专属tabs
        # QQ专属 tabs (index 1, 3, 9, 10)
        for i in (1, 3, 9, 10):
            self.notebook.tab(i, state="hidden" if is_lianzhong else "normal")
        # 主视角 tab (index 2) 始终隐藏
        self.notebook.tab(2, state="hidden")
        # 联众专属 tabs (index 4, 5, 6)
        for i in (4, 5, 6):
            self.notebook.tab(i, state="normal" if is_lianzhong else "hidden")
        # 公共 tabs 始终显示 (0=棋子维度, 7=按布阵筛选, 8=条件概率, 11=一键筛选, 12=通用条件统计)
        for i in (0, 7, 8, 11, 12):
            self.notebook.tab(i, state="normal")

        # 选项卡跳转逻辑:
        # 1.1 QQ->联众，且当前是"QQ颜色(兼座次)统计"(index=1) -> 切换到"颜色统计"(index=4)
        # 1.2 联众->QQ，且当前是"颜色统计"(index=4)或"先手方视角"(index=5) -> 切换到"QQ颜色(兼座次)统计"(index=1)
        # 1.3 QQ->联众，且当前是"QQ号码统计"(index=3) -> 切换到"联众ID统计"(index=6)
        # 1.4 联众->QQ，且当前是"联众ID统计"(index=6) -> 切换到"QQ号码统计"(index=3)
        # 1.5 QQ->联众，且当前是QQ复盘解析/批量统计(9/10) -> 切换到"通用筛选条件统计"(index=12)
        # 其它情况: 如果当前tab被隐藏，跳到棋子维度统计(0)
        if is_lianzhong and current_idx == 1:
            target_idx = 4
        elif is_lianzhong and current_idx == 3:
            target_idx = 6
        elif is_lianzhong and current_idx in (9, 10):
            target_idx = 12
        elif not is_lianzhong and current_idx in (4, 5):
            target_idx = 1
        elif not is_lianzhong and current_idx == 6:
            target_idx = 3
        else:
            # 检查当前tab是否仍然可见
            try:
                tab_state = self.notebook.tab(current_idx, "state")
                if tab_state == "hidden":
                    target_idx = 0  # 跳到棋子维度统计
                else:
                    target_idx = current_idx
            except Exception:
                target_idx = 0

        # 确保目标tab可见
        try:
            self.notebook.select(target_idx)
        except Exception:
            self.notebook.select(0)

        # 清空所有统计结果
        self._clear_all_stats()

    def _clear_all_stats(self) -> None:
        """清空所有面板的统计结果"""
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel,
                     self.lz_id_panel, self.lz_first_panel, self.piece_panel,
                     self.layout_filter_panel, self.conditional_panel,
                     self.qq_batch_panel, self.qq_quick_panel, self.general_panel):
            panel.clear_stats()

    def _on_direction_change(self) -> None:
        # 重绘所有已有统计结果的面板
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel,
                     self.lz_id_panel, self.lz_first_panel, self.piece_panel,
                     self.layout_filter_panel, self.conditional_panel):
            panel._refresh_grid()
        # 条件概率面板: 更新旗侧视角下(5,1)位置的显示
        self.conditional_panel.update_for_direction()

    def _on_display_mode_change(self) -> None:
        """切换百分比/数量显示模式"""
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel,
                     self.lz_id_panel, self.lz_first_panel, self.piece_panel,
                     self.layout_filter_panel, self.conditional_panel,
                     self.qq_batch_panel, self.general_panel):
            panel._refresh_grid()

    def _on_dedup_change(self) -> None:
        """切换去除重复布阵选项"""
        self._clear_all_stats()

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 2))

        dedup_getter = lambda: self.dedup_var.get()
        display_mode_getter = lambda: self.display_mode_var.get()

        # 棋子维度统计: 始终显示 (index=0)
        self.piece_panel = PieceFilterPanel(
            self.notebook, self.folder_var, export_name="layout_stats_棋子维度.xlsx",
            mirror_getter=self._should_mirror, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter,
            source_var=self.source_var)
        self.notebook.add(self.piece_panel, text="棋子维度统计")

        # QQ颜色统计 (index=1)
        self.color_panel = ColorPanel(
            self.notebook, self.folder_var, export_name="layout_stats_颜色.xlsx",
            mirror_getter=self._is_clockwise, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter)
        self.notebook.add(self.color_panel, text="QQ颜色(兼座次)统计")

        # 主视角统计 (index=2, 仅QQ, 默认隐藏)
        self.view_panel = PerspectivePanel(
            self.notebook, self.folder_var, export_name="layout_stats_主视角.xlsx",
            mirror_getter=self._is_clockwise, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter)
        self.notebook.add(self.view_panel, text="主视角统计")
        self.notebook.tab(2, state="hidden")

        # QQ号码统计 (index=3, 仅QQ)
        self.qq_panel = QQPanel(
            self.notebook, self.folder_var, export_name="layout_stats_QQ.xlsx",
            mirror_getter=self._is_clockwise, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter,
            source_var=self.source_var)
        self.notebook.add(self.qq_panel, text="QQ号码统计")

        # 联众颜色统计 (index=4, 初始隐藏)
        self.lz_panel = LianzhongPanel(
            self.notebook, self.folder_var, export_name="layout_stats_联众.xlsx",
            mirror_getter=self._should_mirror, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter)
        self.notebook.add(self.lz_panel, text="颜色统计")
        self.notebook.tab(4, state="hidden")

        # 联众先手方视角 (index=5, 初始隐藏)
        self.lz_first_panel = LianzhongFirstPanel(
            self.notebook, self.folder_var, export_name="layout_stats_先手方.xlsx",
            mirror_getter=self._should_mirror, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter)
        self.notebook.add(self.lz_first_panel, text="先手方视角")
        self.notebook.tab(5, state="hidden")

        # 联众ID统计 (index=6, 初始隐藏)
        self.lz_id_panel = LianzhongIDPanel(
            self.notebook, self.folder_var, export_name="layout_stats_联众ID.xlsx",
            mirror_getter=self._should_mirror, mainline_getter=self._is_mainline,
            dedup_getter=dedup_getter, display_mode_getter=display_mode_getter)
        self.notebook.add(self.lz_id_panel, text="联众ID统计")
        self.notebook.tab(6, state="hidden")

        # 按布阵筛选 (index=7, 始终显示)
        self.layout_filter_panel = LayoutFilterPanel(
            self.notebook, self.folder_var, self.source_var)
        self.notebook.add(self.layout_filter_panel, text="按布阵筛选")

        # 条件概率筛选 (index=8, 始终显示)
        self.conditional_panel = ConditionalProbabilityPanel(
            self.notebook, self.folder_var, self.source_var,
            display_mode_getter=display_mode_getter,
            direction_getter=lambda: self.direction_var.get(),
            is_mainline_getter=self._is_mainline)
        self.notebook.add(self.conditional_panel, text="条件概率筛选")

        # QQ复盘完整解析 (index=9, 仅QQ)
        self.qq_replay_panel = QQReplayPanel(self.notebook)
        self.notebook.add(self.qq_replay_panel, text="QQ复盘解析")

        # QQ复盘批量统计 (index=10, 仅QQ)
        self.qq_batch_panel = QQBatchPanel(
            self.notebook, self.folder_var, display_mode_getter=display_mode_getter)
        self.notebook.add(self.qq_batch_panel, text="QQ复盘批量统计")

        # 常用模式一键筛选 (index=11, 双平台, 复用全局平台 radio)
        self.qq_quick_panel = QQQuickFilterPanel(
            self.notebook, self.folder_var, self.source_var)
        self.notebook.add(self.qq_quick_panel, text="常用模式一键筛选")

        # 通用筛选条件统计 (index=12, 双平台, 复用全局平台 radio)
        self.general_panel = GeneralFilterPanel(
            self.notebook, self.folder_var, self.source_var,
            display_mode_getter=display_mode_getter)
        self.notebook.add(self.general_panel, text="通用筛选条件统计")

        # 联众复盘完整解析: GLReplayPanel 模块保留, 主入口不再挂载该选项卡

        # QQ/联众选项卡对应关系 (用于切换平台时保持位置)
        self._qq_to_lz_tab = {1: 4, 3: 6}  # QQ index -> 联众 index
        self._lz_to_qq_tab = {4: 1, 5: 1, 6: 3}  # 联众 index -> QQ index

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含 .jgs/.jqh 复盘的文件夹", parent=self.root)
        if folder:
            self.folder_var.set(folder)


def main() -> None:
    root = tk.Tk()
    StatsGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()