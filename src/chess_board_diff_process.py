"""JQH 布阵区棋盘可视化：按字节组差异判断棋子/空格，输出 17×17 方阵。"""

import sys
from pathlib import Path
import pdb

# 读取区域: 0x1070 to 0x14F7 (含两端) = 1160 字节 = 290 组
READ_START = 0x1070
READ_END = 0x14F7  # inclusive
GROUP_SIZE = 4
TOTAL_BYTES = READ_END - READ_START + 1  # 1160
NUM_GROUPS = TOTAL_BYTES // GROUP_SIZE    # 290
GRID_SIZE = 17  # 289 = 17 * 17
out_board_ranges = [
        # 棋盘上方
        (1, 6), (12, 17), 
        (18, 23), (29, 34),
        (35, 40), (46, 51),
        (52, 57), (63, 68),
        (69, 74), (80, 85),
        (86, 91), (97, 102),

        # 棋盘中部
        (110, 110), (112, 112),
        (126, 130),
        (144, 144), (146, 146),
        (160, 164),
        (178, 178), (180, 180),

        # 棋盘下方
        (1+187, 6+187), (12+187, 17+187), 
        (18+187, 23+187), (29+187, 34+187),
        (35+187, 40+187), (46+187, 51+187),
        (52+187, 57+187), (63+187, 68+187),
        (69+187, 74+187), (80+187, 85+187),
        (86+187, 91+187), (97+187, 102+187)
    ]

def load_groups(raw: bytes) -> list[bytes]:
    """从指定区域读取 290 组 4 字节数据。"""
    data = raw[READ_START:READ_END + 1]
    groups = []
    for i in range(0, len(data), GROUP_SIZE):
        groups.append(data[i:i + GROUP_SIZE])
    return groups

def classify_groups(groups: list[bytes]) -> list[bool]:
    """判断每组是棋子(True)还是空格(False)。

    规则:
      - groups[0] 是基准标记（空格参考值）
      - groups[1]: 如果与 groups[0] 相同则为空格，否则为棋子
      - groups[i] (i>1): 如果与前一组相同，或仅 byte[0] 相差1（byte[1]相同），
        则类型与前一组相同；否则类型翻转
    """
    marker = groups[0]
    is_piece = [False] * len(groups)  # groups[0] = marker = 空格
    
    for i in range(1, len(groups)):
        prev = [int(hex(item_prev), 16) for item_prev in list(groups[i - 1])]
        curr = [int(hex(item_curr), 16) for item_curr in list(groups[i])]

        if any(start <= i <= end for start, end in out_board_ranges):
            is_piece[i] = False
            continue

        if curr == prev:
            if is_piece[i-1]:
              is_piece[i] = not is_piece[i - 1]
            else:
              pass
              #print("warning")            
        elif (curr[1] == prev[1] and curr[2] == prev[2] and curr[3] == prev[3] and abs(curr[0] - prev[0]) == 1):
            if is_piece[i-1]:
              is_piece[i] = not is_piece[i - 1]
            else:
              pass
              #print("warning")
        else:
            if is_piece[i-1]:
              is_piece[i] = is_piece[i - 1]
            else:
              is_piece[i] = not is_piece[i - 1]

            
    return is_piece

def print_grid(is_piece: list[bool], start_idx: int = 1):
    """将 289 个分类结果排成 17×17 方阵并输出。"""
    grid = []
    for row in range(GRID_SIZE):
        line = []
        for col in range(GRID_SIZE):
            idx = start_idx + row * GRID_SIZE + col
            if idx < len(is_piece):
                line.append('X' if is_piece[idx] else '-' \
                    if any(start <= idx <= end for start, end in out_board_ranges) \
                    else "O")
            else:
                line.append('?')
        grid.append(line)

    print('    ' + ' '.join(f'{c:2d}' for c in range(GRID_SIZE)))
    print('   ' + '-' * (GRID_SIZE * 3))
    for r in range(GRID_SIZE):
        print(f'{r:2d} | ' + ' '.join(f'{cell}' for cell in grid[r]))

    # 统计
    total_x = sum(1 for r in grid for cell in r if cell == 'X')
    total_o = sum(1 for r in grid for cell in r if cell == 'O')
    print(f'\n棋子(X): {total_x}  空格(O): {total_o}  总计: {total_x + total_o}')

def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path(r'e:\Program Files\JunqiReplayDIY\jgs_python\test\test_lianzhong_jqh\test13.JQH')

    raw = path.read_bytes()
    print(f'文件: {path.name} ({len(raw)} 字节)')
    print(f'读取区域: 0x{READ_START:04X} - 0x{READ_END:04X} ({TOTAL_BYTES} 字节, {NUM_GROUPS} 组)')

    groups = load_groups(raw)
    print(f'基准标记(groups[0]): {groups[0].hex()}')

    # 展示前10组
    print('\n前10组数据:')
    for i in range(min(51, len(groups))):
        g = groups[i]
        xor_val = g[0] ^ g[1]
        print(f'  [{i:3d}] {g[0]:02X} {g[1]:02X} {g[2]:02X} {g[3]:02X}  xor=0x{xor_val:02X}')

    is_piece = classify_groups(groups)
    print(f'\n分类结果(前20组): {["X" if p else "O" for p in is_piece[:20]]}')

    # 17×17 网格 (从 groups[1] 开始取289个)
    print('\n17×17 棋盘:')
    print_grid(is_piece, start_idx=1)

if __name__ == '__main__':
    main()
