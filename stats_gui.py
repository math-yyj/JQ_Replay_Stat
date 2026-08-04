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
import math
import os
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

ROW_TITLES = ["第1行", "第2行", "第3行", "第4行", "第5行", "第6行"]

HEADER_BG = "#D9E1F2"
CELL_BG = "#FFFFFF"
CAMP_BG = "#D0D0D0"  # 行营灰色背景
EMPTY_TEXT = "(无样本)"

# 行营位置 (0-indexed)
CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}


class StatPanel(ttk.Frame):
    """独立统计面板基类: 自带统计/导出按钮/汇总栏/6x5 网格与统计结果,
    筛选控件由子类实现 (_build_filter/_get_filter), 多个面板之间互不干扰"""

    def __init__(self, parent, folder_var: tk.StringVar, export_name: str,
                 mirror_getter=None, mainline_getter=None, dedup_getter=None,
                 display_mode_getter=None):
        super().__init__(parent, padding=4)
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
            messagebox.showwarning("提示", "请先选择有效的文件夹")
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
            messagebox.showerror("错误", err)
            return
        if total_files == 0:
            self.summary_var.set("未找到 .jgs 文件")
            messagebox.showinfo("提示", "该文件夹下未找到 .jgs 文件")
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
            messagebox.showwarning("部分复盘未计入统计", "\n\n".join(parts))

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
            messagebox.showerror("导出失败", str(e))
            return
        messagebox.showinfo("完成", f"已导出: {xlsx_file}")


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
            messagebox.showwarning("提示", "请至少勾选一个筛选条件")
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
            messagebox.showwarning("提示", "请至少勾选一个颜色")
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
        self.export_btn = ttk.Button(
            bar, text="导出布阵", command=self._export_layouts, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=(8, 0))

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
            messagebox.showwarning("提示", "请至少填写白名单或黑名单中的一个QQ号码")
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
            messagebox.showwarning("提示", "请先选择有效的文件夹")
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
            messagebox.showerror("错误", err)
            return
        if total_files == 0:
            self.summary_var.set("未找到 .jgs 文件")
            messagebox.showinfo("提示", "该文件夹下未找到 .jgs 文件")
            return

        self.stats = stats
        self.matched_layouts = matched
        self._refresh_grid()
        failed = len(stats.failed_files)
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"统计布阵数: {stats.layout_count}")
        self.summary_var.set(summary)
        self.export_btn.config(state=tk.NORMAL if matched else tk.DISABLED)

        if failed:
            paths = [str(Path(f).name) for f, _ in stats.failed_files[:10]]
            detail = "\n".join(paths)
            if failed > 10:
                detail += f"\n……共 {failed} 个"
            messagebox.showwarning("部分文件解析失败", detail)

    def _export_layouts(self) -> None:
        """导出筛选后的布阵文件"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布阵")
            return

        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
        if not save_dir:
            return

        try:
            total_count = len(self.matched_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
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

            exported_count = 0
            for i, (file_path, color, layout_data) in enumerate(self.matched_layouts):
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

            messagebox.showinfo("完成", f"成功导出 {exported_count} 个布局文件到:\n{save_dir}")
            self.summary_var.set(f"已导出 {exported_count} 个布局文件")

        except Exception as e:
            messagebox.showerror("导出失败", str(e))
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
            messagebox.showwarning("提示", "请至少勾选一个颜色")
            return None
        desc = "颜色[" + "+".join(chosen) + "]"
        return None, None, desc  # selector/predicate 不用, desc 用于显示

    def _start_stat(self) -> None:
        """联众模式重写: 用 add_jqh_folder 替代 add_folder"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹")
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
            messagebox.showwarning("提示", "请至少勾选一个棋子")
            return None
        piece_set = {it for _, it in chosen}
        desc = "棋子[" + "+".join(name for name, _ in chosen) + "]"
        return piece_set, None, desc

    def _start_stat(self) -> None:
        """根据来源模式(QQ/联众)选择不同的文件加载方式"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹")
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
            messagebox.showerror("导出失败", str(e))
            return
        messagebox.showinfo("完成", f"已导出: {xlsx_file}")


class AdvancedFilterPanel(ttk.Frame):
    """高级筛选面板: 支持多种筛选方式"""

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar):
        super().__init__(parent, padding=4)
        self._folder_var = folder_var
        self._source_var = source_var
        self._build_ui()

    def _build_ui(self) -> None:
        # 子选项单选按钮
        self.sub_option_var = tk.StringVar(value="按组合条件筛选")
        sub_options = ["按组合条件筛选", "按布阵文件筛选", "按手工布阵筛选"]
        
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
            self._show_file_filter_placeholder()
        elif option == "按手工布阵筛选":
            self._show_manual_filter_placeholder()

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
        
        self.export_btn = ttk.Button(button_frame, text="导出", command=self._export_combination_results, state=tk.DISABLED)
        self.export_btn.pack(side=tk.LEFT, padx=(0, 10))
        
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
            messagebox.showwarning("提示", "请先选择有效的文件夹")
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
            messagebox.showwarning("提示", "请至少选择一个筛选条件")
            return
        
        # 禁用按钮，显示进度
        self.stat_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.result_var.set("统计中……")
        
        # 后台线程统计
        def worker():
            try:
                is_lianzhong = self._source_var.get() == "联众"
                matched = []
                total = 0
                
                if is_lianzhong:
                    # 联众模式：解析 .jqh 文件
                    from src.lianzhong import decode_jqh_all_boards, get_char_to_itemtype
                    c2i = get_char_to_itemtype()
                    pattern = os.path.join(folder, "**", "*.jqh")
                    files = sorted(glob.glob(pattern, recursive=True))
                    total = len(files)
                    
                    for i, jqh_file in enumerate(files):
                        try:
                            raw = open(jqh_file, "rb").read()
                            boards, _ = decode_jqh_all_boards(raw)
                            
                            # 检查每个颜色的布阵
                            for color, board in boards.items():
                                if all(ch == "空" for row in board for ch in row):
                                    continue
                                
                                # 检查布阵是否匹配条件
                                if self._match_layout_matrix(board, conditions, c2i):
                                    matched.append((jqh_file, color, board))
                        except Exception as e:
                            pass  # 跳过解析失败的文件
                        
                        # 更新进度
                        self.result_var.set(f"统计中…… {i+1}/{total}")
                else:
                    # QQ模式：解析 .jgs 文件
                    from src.layout_stats import find_jgs_files, load_total_info
                    files = find_jgs_files(folder)
                    total = len(files)
                    
                    for i, jgs_file in enumerate(files):
                        try:
                            total_info = load_total_info(jgs_file)
                            for pib in total_info.player_info_block:
                                layout = pib.player_layout
                                # 检查布阵是否为空
                                if all(layout.item(r, c) == ItemType.JQNone 
                                       for r in range(JGSLayoutBlock.row_num) 
                                       for c in range(JGSLayoutBlock.col_num)):
                                    continue
                                
                                # 检查布阵是否匹配条件
                                if self._match_layout_block(layout, conditions):
                                    matched.append((jgs_file, pib.player_color, layout))
                        except Exception as e:
                            pass  # 跳过解析失败的文件
                        
                        # 更新进度
                        self.result_var.set(f"统计中…… {i+1}/{total}")
                
                self._stat_result = (matched, total, None)
            except Exception as e:
                self._stat_result = ([], 0, str(e))
        
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_stat_worker)

    def _match_layout_block(self, layout: JGSLayoutBlock, conditions: dict) -> bool:
        """检查QQ布阵是否匹配所有条件"""
        for (row, col), allowed_types in conditions.items():
            piece = layout.item(row, col)
            if piece not in allowed_types:
                return False
        return True

    def _match_layout_matrix(self, board: list[list[str]], conditions: dict, c2i: dict) -> bool:
        """检查联众布阵是否匹配所有条件"""
        for (row, col), allowed_types in conditions.items():
            if row >= len(board) or col >= len(board[row]):
                return False
            piece_name = board[row][col]
            piece_type = c2i.get(piece_name, ItemType.JQNone)
            if piece_type not in allowed_types:
                return False
        return True

    def _poll_stat_worker(self) -> None:
        """轮询统计线程结果"""
        if self._worker.is_alive():
            self.after(100, self._poll_stat_worker)
            return
        
        matched, total, err = self._stat_result
        self.stat_btn.config(state=tk.NORMAL)
        
        if err is not None:
            self.result_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err)
            return
        
        self.matched_layouts = matched
        self.total_layouts = total
        
        if total == 0:
            self.result_var.set("未找到布局文件")
            messagebox.showinfo("提示", "该文件夹下未找到布局文件")
            return
        
        # 计算匹配百分比
        match_count = len(matched)
        percentage = (match_count / total * 100) if total > 0 else 0
        
        self.result_var.set(f"匹配布局: {match_count}/{total} ({percentage:.1f}%)")
        self.export_btn.config(state=tk.NORMAL if match_count > 0 else tk.DISABLED)

    def _clear_all_checkboxes(self) -> None:
        """清空所有复选框"""
        for var in self.checkbox_vars.values():
            var.set(False)
        self.result_var.set("请选择条件并点击统计")
        self.export_btn.config(state=tk.DISABLED)

    def _export_combination_results(self) -> None:
        """导出匹配的布局为.jqs文件"""
        if not self.matched_layouts:
            messagebox.showwarning("提示", "没有可导出的布局")
            return
        
        # 选择保存目录
        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
        if not save_dir:
            return
        
        try:
            total_count = len(self.matched_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
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
            JQL_FILE_SIZE = 50  # 20 header + 30 layout
            
            exported_count = 0
            for i, (file_path, color, layout_data) in enumerate(self.matched_layouts):
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
                    # 联众jqs格式
                    data = bytearray(JQS_FILE_SIZE)
                    data[0:len(JQS_HEADER)] = JQS_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQS_LAYOUT_OFFSET + (row * JQS_ROW_STRIDE + col) * 4
                            pt = get_piece_type(row, col)
                            code = 0x00 if (row, col) in CAMP_POSITIONS else PIECE_TO_JQS.get(pt, 0x00)
                            struct.pack_into('<I', data, offset, code)
                else:
                    # QQ jql格式
                    data = bytearray(JQL_FILE_SIZE)
                    data[0:20] = JQL_HEADER
                    for row in range(6):
                        for col in range(5):
                            offset = JQL_LAYOUT_OFFSET + row * 5 + col
                            pt = get_piece_type(row, col)
                            data[offset] = int(pt)  # ItemType值即QQ编码
                
                with open(output_path, 'wb') as f:
                    f.write(data)
                
                exported_count += 1
                self.result_var.set(f"导出中…… {exported_count}/{total_count}")
            
            messagebox.showinfo("完成", f"成功导出 {exported_count} 个布局文件到:\n{save_dir}")
            self.result_var.set(f"已导出 {exported_count} 个布局文件")
            
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
            self.result_var.set(f"导出失败: {e}")

    def _show_file_filter_placeholder(self) -> None:
        """显示按布阵文件筛选占位符"""
        ttk.Label(self.content_frame, text="按布阵文件筛选功能开发中...").pack(expand=True)

    def _show_manual_filter_placeholder(self) -> None:
        """显示按手工布阵筛选占位符"""
        ttk.Label(self.content_frame, text="按手工布阵筛选功能开发中...").pack(expand=True)

    def clear_stats(self) -> None:
        """清空统计结果"""
        pass

    def _refresh_grid(self) -> None:
        """刷新网格（高级筛选面板没有网格）"""
        pass


class ConditionalProbabilityPanel(ttk.Frame):
    """条件概率筛选面板: 条件格子筛选 + 结果格子显示频率分布"""

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

    # 单元格角色: 条件=红色, 结果=绿色, 忽略=黑色
    CELL_ROLES = {"条件": "#CC0000", "结果": "#00AA00", "忽略": "#000000"}

    def __init__(self, parent, folder_var: tk.StringVar, source_var: tk.StringVar,
                 display_mode_getter=None):
        super().__init__(parent, padding=4)
        self._folder_var = folder_var
        self._source_var = source_var
        self._display_mode_getter = display_mode_getter or (lambda: "百分比")
        self._build_ui()

    def _build_ui(self) -> None:
        # 按钮栏
        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, pady=(0, 5))

        self.stat_btn = ttk.Button(button_frame, text="统计", command=self._start_stat)
        self.stat_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.clear_btn = ttk.Button(button_frame, text="清空所有勾选", command=self._clear_all)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.result_var = tk.StringVar(value="选择条件/结果格子后点击统计")
        ttk.Label(button_frame, textvariable=self.result_var).pack(side=tk.LEFT, padx=(20, 0))

        # 网格区域
        grid_frame = ttk.Frame(self)
        grid_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 存储
        self.cell_role_vars = {}     # {(row, col): tk.StringVar}
        self.checkbox_vars = {}      # {(row, col, piece_name): tk.BooleanVar}
        self.cell_cb_frames = {}     # {(row, col): ttk.Frame} 棋子复选框容器
        self.cell_result_labels = {} # {(row, col): ttk.Label} 结果显示标签

        for row in range(6):
            for col in range(5):
                if (row, col) in CAMP_POSITIONS:
                    ttk.Label(grid_frame, text="(行营)", anchor=tk.CENTER).grid(
                        row=row, column=col, padx=2, pady=2, sticky="nsew")
                    continue

                cell_frame = ttk.LabelFrame(grid_frame, text=f"({row+1},{col+1})")
                cell_frame.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")

                # 角色三选一: 条件/结果/忽略
                role_var = tk.StringVar(value="忽略")
                self.cell_role_vars[(row, col)] = role_var
                role_frame = ttk.Frame(cell_frame)
                role_frame.pack(fill=tk.X, padx=2, pady=(2, 0))
                for role_name in ("条件", "结果", "忽略"):
                    ttk.Radiobutton(role_frame, text=role_name, value=role_name,
                                    variable=role_var,
                                    command=lambda r=row, c=col: self._on_role_change(r, c)
                                    ).pack(side=tk.LEFT, padx=(0, 4))

                # 棋子复选框 4行3列 (默认隐藏)
                cb_frame = ttk.Frame(cell_frame)
                self.cell_cb_frames[(row, col)] = cb_frame
                for idx, (piece_name, piece_type) in enumerate(self.PIECE_OPTIONS):
                    r, c = divmod(idx, 3)
                    var = tk.BooleanVar(value=False)
                    self.checkbox_vars[(row, col, piece_name)] = var

                    should_disable = False
                    if piece_name == "弹" and row == 0:
                        should_disable = True
                    if piece_name == "旗" and (row, col) not in [(5, 1), (5, 3)]:
                        should_disable = True
                    if piece_name == "雷" and row not in [4, 5]:
                        should_disable = True

                    state = tk.DISABLED if should_disable else tk.NORMAL
                    ttk.Checkbutton(cb_frame, text=piece_name, variable=var,
                                    state=state).grid(row=r, column=c, padx=2, pady=1, sticky=tk.W)

                # 结果显示标签 (默认隐藏)
                result_lbl = ttk.Label(cell_frame, text="", foreground="#00AA00",
                                       font=("Consolas", 9), anchor=tk.NW, justify=tk.LEFT)
                self.cell_result_labels[(row, col)] = result_lbl

                # 默认忽略: 隐藏棋子框和结果标签
                # (pack_forget 不需要调用，因为还没 pack)

        for row in range(6):
            grid_frame.rowconfigure(row, weight=1)
        for col in range(5):
            grid_frame.columnconfigure(col, weight=1)

    def _on_role_change(self, row: int, col: int) -> None:
        """格子角色切换时，显示/隐藏对应内容"""
        role = self.cell_role_vars[(row, col)].get()
        cb_frame = self.cell_cb_frames[(row, col)]
        result_lbl = self.cell_result_labels[(row, col)]

        # 先全部隐藏
        cb_frame.pack_forget()
        result_lbl.pack_forget()

        if role == "条件":
            cb_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))
        elif role == "结果":
            result_lbl.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

    def _clear_all(self) -> None:
        """清空所有角色和棋子勾选，复位为忽略"""
        for (row, col), role_var in self.cell_role_vars.items():
            role_var.set("忽略")
            # 隐藏棋子框和结果标签
            self.cell_cb_frames[(row, col)].pack_forget()
            self.cell_result_labels[(row, col)].pack_forget()
            self.cell_result_labels[(row, col)].config(text="")
        for var in self.checkbox_vars.values():
            var.set(False)
        self.result_var.set("选择条件/结果格子后点击统计")

    def _start_stat(self) -> None:
        """开始条件概率统计"""
        folder = self._folder_var.get().strip()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择有效的文件夹")
            return

        # 收集条件格子 (有棋子勾选的"条件"格子)
        conditions = {}  # {(row, col): set(piece_type)}
        result_cells = set()  # {(row, col)}
        PIECE_NAME_TO_TYPE = {name: pt for name, pt in self.PIECE_OPTIONS}

        for (row, col), role_var in self.cell_role_vars.items():
            role = role_var.get()
            if role == "条件":
                checked = set()
                for (r, c, pname), var in self.checkbox_vars.items():
                    if r == row and c == col and var.get():
                        checked.add(PIECE_NAME_TO_TYPE[pname])
                if checked:
                    conditions[(row, col)] = checked
            elif role == "结果":
                result_cells.add((row, col))

        if not conditions:
            messagebox.showwarning("提示", "请至少将一个格子设为'条件'并勾选棋子")
            return
        if not result_cells:
            messagebox.showwarning("提示", "请至少将一个格子设为'结果'")
            return

        self.stat_btn.config(state=tk.DISABLED)
        self.result_var.set("统计中……")

        is_lianzhong = self._source_var.get() == "联众"
        show_pct = self._display_mode_getter() == "百分比"

        def worker():
            try:
                result_counts = {(r, c): {} for r, c in result_cells}
                match_total = 0
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
                                if self._match_matrix(board, conditions, c2i):
                                    match_total += 1
                                    for rc in result_cells:
                                        r, c = rc
                                        piece_name = board[r][c]
                                        pt = c2i.get(piece_name, ItemType.JQNone)
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
                            for pib in total_info.player_info_block:
                                layout = pib.player_layout
                                if all(layout.item(r, c) == ItemType.JQNone
                                       for r in range(JGSLayoutBlock.row_num)
                                       for c in range(JGSLayoutBlock.col_num)):
                                    continue
                                if self._match_block(layout, conditions):
                                    match_total += 1
                                    for rc in result_cells:
                                        r, c = rc
                                        pt = layout.item(r, c)
                                        result_counts[rc][pt] = result_counts[rc].get(pt, 0) + 1
                        except Exception:
                            pass
                        self.result_var.set(f"统计中…… {i+1}/{total}")

                self._stat_result = (result_counts, match_total, total, show_pct, None)
            except Exception as e:
                self._stat_result = ({}, 0, 0, show_pct, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)

    def _match_block(self, layout: JGSLayoutBlock, conditions: dict) -> bool:
        for (row, col), allowed in conditions.items():
            if layout.item(row, col) not in allowed:
                return False
        return True

    def _match_matrix(self, board, conditions, c2i) -> bool:
        for (row, col), allowed in conditions.items():
            if row >= len(board) or col >= len(board[row]):
                return False
            pt = c2i.get(board[row][col], ItemType.JQNone)
            if pt not in allowed:
                return False
        return True

    def _poll_worker(self) -> None:
        if self._worker.is_alive():
            self.after(100, self._poll_worker)
            return

        result_counts, match_total, total, show_pct, err = self._stat_result
        self.stat_btn.config(state=tk.NORMAL)

        if err:
            self.result_var.set(f"统计失败: {err}")
            messagebox.showerror("错误", err)
            return
        if match_total == 0:
            self.result_var.set(f"无匹配布阵 (总文件: {total})")
            return

        # 在结果格子里显示频率分布
        from src.jgs_types import item_type2string
        for rc, counts in result_counts.items():
            total_count = sum(counts.values())
            if total_count == 0:
                continue
            sorted_items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            lines = []
            for pt, cnt in sorted_items:
                ratio = cnt / total_count
                val = f"{ratio*100:5.1f}%" if show_pct else str(cnt)
                lines.append(f"{item_type2string(pt)}:{val}")
            # 更新结果标签
            result_lbl = self.cell_result_labels.get(rc)
            if result_lbl:
                result_lbl.config(text="\n".join(lines))

        self.result_var.set(f"匹配: {match_total}/{total} ({match_total/total*100:.1f}%)")

    def clear_stats(self) -> None:
        """清空统计结果"""
        self._clear_all()

    def _refresh_grid(self) -> None:
        pass


class StatsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("军棋复盘布阵统计")

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
        ttk.Label(row1, text="文件夹:").pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.folder_var, width=52).pack(
            side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        ttk.Button(row1, text="浏览...", command=self._choose_folder).pack(side=tk.LEFT)

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

        # 去重勾选框
        self.dedup_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="去除重复布阵", variable=self.dedup_var).pack(side=tk.LEFT, padx=(16, 0))

        # 频率显示模式: 百分比/数量
        ttk.Label(row2, text="  显示:").pack(side=tk.LEFT)
        self.display_mode_var = tk.StringVar(value="百分比")
        for name in ("百分比", "数量"):
            ttk.Radiobutton(row2, text=name, value=name, variable=self.display_mode_var,
                            command=self._on_display_mode_change).pack(side=tk.LEFT, padx=(4, 0))

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
        """切换联众/QQ模式: 联众=棋子维度+联众颜色, QQ=全部四个tab"""
        is_lianzhong = self.source_var.get() == "联众"

        # 切换默认方向: 如果当前是主线模式则保留, 否则设默认
        if not self._is_mainline():
            if is_lianzhong:
                self.direction_var.set("顺时针")
            else:
                self.direction_var.set("逆时针")

        # QQ tabs (index 1, 3): 颜色、QQ号码 (主视角index=2始终隐藏)
        for i in (1, 3):
            self.notebook.tab(i, state="hidden" if is_lianzhong else "normal")
        # 主视角 tab (index 2) 始终隐藏
        self.notebook.tab(2, state="hidden")
        # 联众颜色 tab (index 4)
        self.notebook.tab(4, state="normal" if is_lianzhong else "hidden")
        # 棋子维度 tab (index 0) 始终显示
        self.notebook.tab(0, state="normal")
        # 高级筛选 tab (index 5) 始终显示
        self.notebook.tab(5, state="normal")
        # 条件概率筛选 tab (index 6) 始终显示
        self.notebook.tab(6, state="normal")
        self.notebook.select(0)

        # 清空所有统计结果
        self._clear_all_stats()

    def _clear_all_stats(self) -> None:
        """清空所有面板的统计结果"""
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel, 
                     self.piece_panel, self.advanced_filter_panel, self.conditional_panel):
            panel.clear_stats()

    def _on_direction_change(self) -> None:
        # 重绘所有已有统计结果的面板
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel, 
                     self.piece_panel, self.advanced_filter_panel, self.conditional_panel):
            panel._refresh_grid()

    def _on_display_mode_change(self) -> None:
        """切换百分比/数量显示模式"""
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel, 
                     self.piece_panel, self.advanced_filter_panel, self.conditional_panel):
            panel._refresh_grid()

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

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
        self.notebook.add(self.color_panel, text="颜色(兼座次)统计")

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

        # 高级筛选 (index=5, 始终显示)
        self.advanced_filter_panel = AdvancedFilterPanel(
            self.notebook, self.folder_var, self.source_var)
        self.notebook.add(self.advanced_filter_panel, text="高级筛选")

        # 条件概率筛选 (index=6, 始终显示)
        self.conditional_panel = ConditionalProbabilityPanel(
            self.notebook, self.folder_var, self.source_var,
            display_mode_getter=display_mode_getter)
        self.notebook.add(self.conditional_panel, text="条件概率筛选")

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