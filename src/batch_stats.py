# 批处理入口：统计指定文件夹下所有 .jgs 复盘的布阵子力频率
# 用法: python src\batch_stats.py <文件夹> [--excel 输出.xlsx]

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from . import LayoutStats


def main() -> int:
    parser = argparse.ArgumentParser(description="统计 .jgs 复盘布阵中各位置子力的绝对/相对频率")
    parser.add_argument("folder", help="待递归遍历的文件夹")
    parser.add_argument("--excel", metavar="FILE", help="将统计结果另存为 Excel 文件 (.xlsx)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"文件夹不存在: {folder}", file=sys.stderr)
        return 1

    stats = LayoutStats()
    files = stats.add_folder(folder)
    if not files:
        print(f"未找到 .jgs 文件: {folder}", file=sys.stderr)
        return 1

    print(f"找到 {len(files)} 个 .jgs 文件\n")
    print(stats.report())
    print("\n各位置最高频子力 (6x5 初始布局网格):")
    print(stats.summary_grid())

    if args.excel:
        stats.save_excel(args.excel)
        print(f"\n统计结果已保存: {args.excel}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
