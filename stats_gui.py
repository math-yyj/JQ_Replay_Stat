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
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"统计布阵数: {stats.layout_count}")
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
        summary = (f"筛选: {self._filter_desc}    找到文件: {total_files}    "
                   f"解析成功: {stats.file_count}    失败: {failed}    "
                   f"统计布阵数: {stats.layout_count}")
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
            total_count = len(self.matched_layouts)
            prefix_digits = int(math.log10(total_count)) + 1 if total_count > 0 else 1
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

        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
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

        save_dir = filedialog.askdirectory(title="选择导出目录", parent=self.winfo_toplevel())
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

    # 归一化因子: 一副完整棋子里每种棋子的个数
    NORMALIZE_FACTOR = {
        ItemType.Silin: 1.0,      # 司
        ItemType.Junzhang: 1.0,   # 军
        ItemType.Shizhang: 2.0,   # 师
        ItemType.Lvzhang: 2.0,    # 旅
        ItemType.Tuanzhang: 2.0,  # 团
        ItemType.Yinzhang: 2.0,   # 营
        ItemType.Lianzhang: 3.0,  # 连
        ItemType.Paizhang: 3.0,   # 排
        ItemType.Gongbing: 3.0,   # 兵
        ItemType.Zhadan: 2.0,     # 弹
        ItemType.Dilei: 3.0,      # 雷
        ItemType.Junqi: 1.0,      # 旗
    }

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
            for child in cell.winfo_children():
                child.grid_forget()
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

            # 归一化: 仅用于排序和百分比计算
            normalized = {}
            for pt, cnt in counts.items():
                factor = self.NORMALIZE_FACTOR.get(pt, 1.0)
                normalized[pt] = cnt / factor
            norm_total = sum(normalized.values())

            # 按归一化后的频率降序排序
            sorted_items = sorted(normalized.items(), key=lambda x: (-x[1], x[0]))

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
                        pt, norm_cnt = sorted_items[i + j]
                        rank = i + j + 1  # 排名从1开始
                        original_cnt = counts[pt]
                        if show_pct:
                            ratio = norm_cnt / norm_total
                            pct_val = ratio * 100
                            val = " 100%" if pct_val == 100 else f"{pct_val:5.1f}%"
                        else:
                            val = f"{original_cnt:{count_width}d}"
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

            # 按布阵去重
            seen_keys = set()
            unique_layouts = []
            for file_path, color, layout_data in self._matched_layouts:
                key = (id(layout_data) if isinstance(layout_data, JGSLayoutBlock) else str(layout_data))
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
        # QQ专属 tabs (index 1, 3)
        for i in (1, 3):
            self.notebook.tab(i, state="hidden" if is_lianzhong else "normal")
        # 主视角 tab (index 2) 始终隐藏
        self.notebook.tab(2, state="hidden")
        # 联众专属 tabs (index 4, 5, 6)
        for i in (4, 5, 6):
            self.notebook.tab(i, state="normal" if is_lianzhong else "hidden")
        # 高级筛选 tab (index 7) 始终隐藏
        self.notebook.tab(7, state="hidden")
        # 公共 tabs 始终显示
        for i in (0, 8):
            self.notebook.tab(i, state="normal")

        # 选项卡跳转逻辑:
        # 1.1 QQ->联众，且当前是"QQ颜色(兼座次)统计"(index=1) -> 切换到"颜色统计"(index=4)
        # 1.2 联众->QQ，且当前是"颜色统计"(index=4)或"先手方视角"(index=5) -> 切换到"QQ颜色(兼座次)统计"(index=1)
        # 1.3 QQ->联众，且当前是"QQ号码统计"(index=3) -> 切换到"联众ID统计"(index=6)
        # 1.4 联众->QQ，且当前是"联众ID统计"(index=6) -> 切换到"QQ号码统计"(index=3)
        # 其它情况: 如果当前tab被隐藏，跳到棋子维度统计(0)
        if is_lianzhong and current_idx == 1:
            target_idx = 4
        elif is_lianzhong and current_idx == 3:
            target_idx = 6
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
                     self.advanced_filter_panel, self.conditional_panel):
            panel.clear_stats()

    def _on_direction_change(self) -> None:
        # 重绘所有已有统计结果的面板
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel,
                     self.lz_id_panel, self.lz_first_panel, self.piece_panel,
                     self.advanced_filter_panel, self.conditional_panel):
            panel._refresh_grid()
        # 条件概率面板: 更新旗侧视角下(5,1)位置的显示
        self.conditional_panel.update_for_direction()

    def _on_display_mode_change(self) -> None:
        """切换百分比/数量显示模式"""
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel,
                     self.lz_id_panel, self.lz_first_panel, self.piece_panel,
                     self.advanced_filter_panel, self.conditional_panel):
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

        # 高级筛选 (index=7, 始终隐藏)
        self.advanced_filter_panel = AdvancedFilterPanel(
            self.notebook, self.folder_var, self.source_var,
            direction_getter=lambda: self.direction_var.get(),
            is_mainline_getter=self._is_mainline)
        self.notebook.add(self.advanced_filter_panel, text="高级筛选")
        self.notebook.tab(7, state="hidden")

        # 条件概率筛选 (index=8, 始终显示)
        self.conditional_panel = ConditionalProbabilityPanel(
            self.notebook, self.folder_var, self.source_var,
            display_mode_getter=display_mode_getter,
            direction_getter=lambda: self.direction_var.get(),
            is_mainline_getter=self._is_mainline)
        self.notebook.add(self.conditional_panel, text="条件概率筛选")

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