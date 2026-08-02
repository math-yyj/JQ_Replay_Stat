# 布阵统计可视化界面 (tkinter)
# 功能: 选择文件夹递归统计 .jgs 布阵, 6x5 网格展示 top5 频率, 可导出 Excel
# 三个互相独立的统计页:
#   1. 主视角统计: 勾选 自家/上家/对家/下家, 取并集
#   2. 颜色统计:   勾选 黄/紫/绿/蓝, 取并集
#   3. QQ号码统计: 输入QQ号, 只统计该玩家的布阵
# 用法: python stats_gui.py

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import LayoutStats, ItemColor
from src.block import JGSLayoutBlock
from src.layout_stats import next_mover, partner, prev_mover

ROW_TITLES = ["第1行", "第2行", "第3行", "第4行", "第5行", "第6行"]

HEADER_BG = "#D9E1F2"
CELL_BG = "#FFFFFF"
EMPTY_TEXT = "(无样本)"


class StatPanel(ttk.Frame):
    """独立统计面板基类: 自带统计/导出按钮/汇总栏/6x5 网格与统计结果,
    筛选控件由子类实现 (_build_filter/_get_filter), 多个面板之间互不干扰"""

    def __init__(self, parent, folder_var: tk.StringVar, export_name: str,
                 mirror_getter=None):
        super().__init__(parent, padding=4)
        self._folder_var = folder_var  # 与其它面板共享文件夹输入, 只读
        self._export_name = export_name
        self._mirror_getter = mirror_getter or (lambda: False)  # 行棋习惯: 顺时针时镜像显示
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

        # 行标题 + 数据单元格
        self.cell_labels: list[list[tk.Label]] = []
        for row in range(JGSLayoutBlock.row_num):
            tk.Label(grid, text=ROW_TITLES[row], bg=HEADER_BG, relief=tk.RIDGE, bd=1,
                     font=("", 10, "bold")).grid(row=row + 1, column=0, sticky="nsew")
            row_labels = []
            for col in range(JGSLayoutBlock.col_num):
                label = tk.Label(grid, text=EMPTY_TEXT, bg=CELL_BG, relief=tk.RIDGE, bd=1,
                                 justify=tk.LEFT, anchor=tk.NW, padx=6, pady=4,
                                 font=("Consolas", 10), height=6, width=26)
                label.grid(row=row + 1, column=col + 1, sticky="nsew")
                row_labels.append(label)
            self.cell_labels.append(row_labels)

        for col in range(JGSLayoutBlock.col_num + 1):
            grid.columnconfigure(col, weight=0 if col == 0 else 1)
        for row in range(JGSLayoutBlock.row_num + 1):
            grid.rowconfigure(row, weight=0 if row == 0 else 1)

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

        # 后台线程统计, 避免大量文件时界面卡死
        def worker():
            stats = LayoutStats()
            try:
                files = stats.add_folder(folder, selector, predicate)
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
        if skipped:
            detail = "\n".join(stats.skipped_1v1_files[:20])
            if skipped > 20:
                detail += f"\n……共 {skipped} 个"
            messagebox.showinfo(
                "已跳过 1v1(单挑) 复盘",
                f"以下 {skipped} 个复盘只有两家布阵(1v1), 未计入统计:\n\n{detail}")
        if abnormal:
            detail = "\n".join(f"{f}\n  {r}" for f, r in stats.skipped_abnormal_files[:20])
            if abnormal > 20:
                detail += f"\n……共 {abnormal} 个"
            messagebox.showwarning(
                "已跳过异常复盘",
                f"以下 {abnormal} 个复盘布阵校验不通过, 未计入统计:\n\n{detail}")
        if failed:
            detail = "\n".join(f"{f}: {r}" for f, r in stats.failed_files[:20])
            messagebox.showwarning("部分文件解析失败", detail)

    def _refresh_grid(self) -> None:
        """刷新网格; 顺时针行棋习惯时以中间列为轴镜像显示 (显示列 col 取数据列 4-col)"""
        if self.stats is None:
            return
        mirror = self._mirror_getter()
        for row in range(JGSLayoutBlock.row_num):
            for col in range(JGSLayoutBlock.col_num):
                data_col = JGSLayoutBlock.col_num - 1 - col if mirror else col
                text = self.stats._cell_text(row, data_col)
                self.cell_labels[row][col].config(text=text)

    def _export_excel(self) -> None:
        if self.stats is None:
            return
        xlsx_file = filedialog.asksaveasfilename(
            title="导出 Excel", defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx")], initialfile=self._export_name)
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
    """颜色统计: 勾选 黄/紫/绿/蓝, 并集筛选"""

    COLORS = [
        ("黄", ItemColor.YELLOW),
        ("紫", ItemColor.PURPLE),
        ("绿", ItemColor.GREEN),
        ("蓝", ItemColor.BLUE),
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
    """QQ号码统计: 输入QQ号, 只统计该玩家的布阵 (QQ未知的复盘记录为0, 无法匹配)"""

    def _build_filter(self, bar: ttk.Frame) -> None:
        ttk.Label(bar, text="QQ号码:").pack(side=tk.LEFT)
        self.qq_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.qq_var, width=16).pack(side=tk.LEFT, padx=(4, 8))

    def _get_filter(self):
        text = self.qq_var.get().strip()
        if not text.isdigit() or int(text) == 0:
            messagebox.showwarning("提示", "请输入有效的QQ号码(纯数字)")
            return None
        qq = int(text)
        predicate = lambda pib, self_color: pib.player_qqnumber == qq
        return None, predicate, f"QQ[{qq}]"


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

        def worker():
            stats = LayoutStats()
            try:
                files = stats.add_jqh_folder(folder, chosen_colors, c2i)
                self._result = (stats, len(files), None)
            except Exception as e:
                self._result = (None, 0, str(e))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_worker)


class StatsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("军棋复盘布阵统计")

        self._build_folder_bar()
        self._build_tabs()

    def _build_folder_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill=tk.X)

        ttk.Label(bar, text="文件夹:").pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.folder_var, width=52).pack(
            side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        ttk.Button(bar, text="浏览...", command=self._choose_folder).pack(side=tk.LEFT)

        # 复盘来源: 联众/QQ 二选一
        ttk.Label(bar, text="  来源:").pack(side=tk.LEFT)
        self.source_var = tk.StringVar(value="QQ")
        for name in ("QQ", "联众"):
            ttk.Radiobutton(bar, text=name, value=name, variable=self.source_var,
                            command=self._on_source_change).pack(side=tk.LEFT, padx=(4, 0))

        # 行棋习惯: 二选一, 独立于各页筛选条件; 切换时立即重绘已有结果
        ttk.Label(bar, text="  方向:").pack(side=tk.LEFT)
        self.direction_var = tk.StringVar(value="逆时针")
        for name in ("逆时针", "顺时针"):
            ttk.Radiobutton(bar, text=name, value=name, variable=self.direction_var,
                            command=self._on_direction_change).pack(side=tk.LEFT, padx=(4, 0))

    def _is_clockwise(self) -> bool:
        return self.direction_var.get() == "顺时针"

    def _on_source_change(self) -> None:
        """切换联众/QQ模式: 联众模式下只显示颜色统计, QQ模式下显示三个tab"""
        is_lianzhong = self.source_var.get() == "联众"

        # 切换默认方向: 联众=顺时针, QQ=逆时针
        if is_lianzhong:
            self.direction_var.set("顺时针")
        else:
            self.direction_var.set("逆时针")

        if is_lianzhong:
            # 隐藏 QQ 三个 tab
            for i in range(3):
                self.notebook.tab(i, state="hidden")
            # 添加并显示联众 tab
            if not self._lianzhong_tab_added:
                self.notebook.add(self.lz_panel, text="颜色统计")
                self._lianzhong_tab_added = True
            else:
                self.notebook.tab(3, state="normal")
            self.notebook.select(3)
        else:
            # 隐藏联众 tab
            if self._lianzhong_tab_added:
                self.notebook.tab(3, state="hidden")
            # 恢复 QQ 三个 tab
            for i in range(3):
                self.notebook.tab(i, state="normal")
            self.notebook.select(1)

        # 重绘已有结果
        self._on_direction_change()

    def _on_direction_change(self) -> None:
        # 重绘所有已有统计结果的面板
        for panel in (self.view_panel, self.color_panel, self.qq_panel, self.lz_panel):
            panel._refresh_grid()

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # 三个独立统计页: 各自的筛选/按钮/汇总/网格/结果互不干扰
        self.view_panel = PerspectivePanel(
            self.notebook, self.folder_var, export_name="layout_stats_主视角.xlsx",
            mirror_getter=self._is_clockwise)
        self.notebook.add(self.view_panel, text="主视角统计")

        self.color_panel = ColorPanel(
            self.notebook, self.folder_var, export_name="layout_stats_颜色.xlsx",
            mirror_getter=self._is_clockwise)
        self.notebook.add(self.color_panel, text="颜色统计")

        self.qq_panel = QQPanel(
            self.notebook, self.folder_var, export_name="layout_stats_QQ.xlsx",
            mirror_getter=self._is_clockwise)
        self.notebook.add(self.qq_panel, text="QQ号码统计")

        # 联众复盘面板: 初始不添加到 notebook, 联众模式下动态添加
        # 联众布阵已做旋转校正(摆正视角), 不需要顺/逆时针镜像
        self.lz_panel = LianzhongPanel(
            self.notebook, self.folder_var, export_name="layout_stats_联众.xlsx",
            mirror_getter=lambda: False)
        self._lianzhong_tab_added = False

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含 .jgs/.jqh 复盘的文件夹")
        if folder:
            self.folder_var.set(folder)


def main() -> None:
    root = tk.Tk()
    StatsGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
