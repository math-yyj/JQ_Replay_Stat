"""基于 chess_board_diff_process 的棋子位置识别 + 累积delta解码子力类型，输出17×17棋盘"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from .chess_board_diff_process import (
    load_groups, classify_groups, out_board_ranges, GRID_SIZE, READ_START, GROUP_SIZE
)
from .lianzhong import JQS_CODE_MAP, JQH_COLOR_K

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r'e:\Program Files\JunqiReplayDIY\jgs_python\test\test_lianzhong_jqh\test1.JQH')
raw = path.read_bytes()
groups = load_groups(raw)
is_piece = classify_groups(groups)

seed_b1 = groups[0][1]
print(f'文件: {path.name}  seed_b1=0x{seed_b1:02X}')

piece_positions = []
for i in range(1, len(is_piece)):
    if is_piece[i]:
        gi = i - 1
        row = gi // GRID_SIZE
        col = gi % GRID_SIZE
        piece_positions.append((i, row, col))

print(f'棋子位置: {len(piece_positions)} 个\n')

prev_b1 = seed_b1
COLORS = ['红', '绿', '蓝']
decoded = {}

for idx, (gi, row, col) in enumerate(piece_positions):
    g = groups[gi]
    b0, b1 = g[0], g[1]
    delta = (b1 - prev_b1) & 0xFF

    best_piece = '?'
    best_color = '?'

    # 红/绿/蓝: piece = (delta - K_color) & 0xFF
    for color in COLORS:
        k = JQH_COLOR_K[color]
        code = (delta - k) & 0xFF
        if code <= 0x0C:
            best_piece = JQS_CODE_MAP.get(code, '?')
            best_color = color
            break

    # 黑方: (delta - K_black) & 0xFF, K_black 由 XOR(byte0, byte1) 查表
    # 每个 XOR 对应多个候选 K 值, 逐个尝试直到 code <= 0x0C
    if best_piece == '?':
        xor_val = b0 ^ b1
        BLACK_XOR_K = {
            0x0C: [0x2B, 0x13],
            0x10: [0x2B],
            0x12: [0x2B],
            0x14: [0x33],
            0x1C: [0x33],
            0x2C: [0x2B, 0x53],
            0x6C: [0x53],
            0xEC: [0x53],
        }
        k_list = BLACK_XOR_K.get(xor_val)
        if k_list is not None:
            for k in k_list:
                code = (delta - k) & 0xFF
                if code <= 0x0C:
                    best_piece = JQS_CODE_MAP.get(code, '?')
                    best_color = '黑'
                    break
        else:
            print(f'  WARNING: 未知黑方XOR=0x{xor_val:02X} at ({row},{col})')

    if best_piece == '?':
        best_piece = f'?({delta:02X})'
        best_color = '?'
        print(f'  WARNING: 无法解码 ({row},{col}) delta=0x{delta:02X} b0=0x{b0:02X} b1=0x{b1:02X}')

    # 空格(行营)不记为棋子, prev_b1 也不更新
    if best_piece == '空':
        continue

    label = f'{best_color}{best_piece}'
    decoded[(row, col)] = label
    prev_b1 = b1

# 输出 17×17 方阵
print('\n    ' + ' '.join(f'{c:2d}' for c in range(GRID_SIZE)))
print('   ' + '-' * (GRID_SIZE * 4))
for r in range(GRID_SIZE):
    cells = []
    for c in range(GRID_SIZE):
        gi = r * GRID_SIZE + c
        if any(s <= gi + 1 <= e for s, e in out_board_ranges):
            cells.append('  ---')
        elif (r, c) in decoded:
            cells.append(f'{decoded[(r, c)]:>5}')
        else:
            cells.append('    O')
    print(f'{r:2d} | ' + ' '.join(cells))

total_x = len(decoded)
total_o = sum(1 for i in range(1, len(is_piece)) if not is_piece[i] and not any(s <= i <= e for s, e in out_board_ranges))
print(f'\n棋子: {total_x}  空格: {total_o}')

# ─── 输出四家布阵 ───
import numpy as np

def extract_subgrid(row_range, col_range):
    """从 decoded 字典提取子方阵, 无棋子位置记为'空', 超界记为'  -'"""
    rows = list(range(*row_range))
    cols = list(range(*col_range))
    grid = []
    for r in rows:
        row_data = []
        for c in cols:
            if (r, c) in decoded:
                piece = decoded[(r, c)]
                # 去掉颜色前缀, 只留子力名
                row_data.append(piece[1:] if len(piece) > 1 else piece)
            else:
                row_data.append('空')
        grid.append(row_data)
    return grid

def rotate_ccw_90(grid):
    """逆时针旋转90度: grid[r][c] -> new_grid[C-1-c][r]"""
    R, C = len(grid), len(grid[0])
    return [[grid[r][c] for r in range(R)] for c in range(C - 1, -1, -1)]

def rotate_cw_90(grid):
    """顺时针旋转90度: grid[r][c] -> new_grid[c][R-1-r]"""
    R, C = len(grid), len(grid[0])
    return [[grid[R - 1 - r][c] for r in range(R)] for c in range(C)]

def rotate_180(grid):
    """旋转180度: grid[r][c] -> new_grid[R-1-r][C-1-c]"""
    R, C = len(grid), len(grid[0])
    return [[grid[R - 1 - r][C - 1 - c] for c in range(C)] for r in range(R)]

def print_layout(title, grid):
    """打印一个6行5列的布阵方阵"""
    print(f'\n{title}')
    for row in grid:
        print('  ' + ' '.join(f'{cell:>3}' for cell in row))

# 红方: range(11,17) x range(6,11), 6行5列, 不旋转
red_grid = extract_subgrid((11, 17), (6, 11))
print_layout('【红方布阵】', red_grid)

# 绿方: range(6,11) x range(0,6), 5行6列, 逆时针旋转90度 → 6行5列
green_raw = extract_subgrid((6, 11), (0, 6))
green_grid = rotate_ccw_90(green_raw)
print_layout('【绿方布阵】(逆时针90°)', green_grid)

# 蓝方: range(0,6) x range(6,11), 6行5列, 旋转180度
blue_raw = extract_subgrid((0, 6), (6, 11))
blue_grid = rotate_180(blue_raw)
print_layout('【蓝方布阵】(旋转180°)', blue_grid)

# 黑方: range(6,11) x range(11,17), 5行6列, 顺时针旋转90度 → 6行5列
black_raw = extract_subgrid((6, 11), (11, 17))
black_grid = rotate_cw_90(black_raw)
print_layout('【黑方布阵】(顺时针90°)', black_grid)
