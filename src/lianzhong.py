# 联众四国军棋复盘 (.JQH) 解析 —— 初版（布阵读取，进行中）
#
# 已确认的容器结构（基于 test1.JQH=20步 与 test2.JQH=1步 双文件差分逆向）:
#   0x00–0x63   文本头(明文GBK): "GL"+时间戳+"研究过程记录 … step N"(总步数N)
#   0x64–0xC7   固定头(100字节): 两文件完全一致(含4个20字节玩家记录@0x64/78/8C/A0)
#   0xC8–gap    行棋数据区: 长度随步数增长(20步→到0x11B, 1步→到0xCF)
#   gap–0x106C  纯0填充: 编码后呈"周期2常量重复段"(如B0A3/3427), 恒填充到固定偏移0x106C
#   0x106C–EOF  尾部数据区(两文件等长1180字节, 随布阵不同而变化): 疑似存放四家布阵/终局
#
# 已确认的编码方案: 周期2累积异或  dec[i] = raw[i] ^ raw[i-2]  (i<2 时取0)
#   —— 该变换把"周期2常量重复段"解码为全0, 与"布阵/行棋间隔一大段纯0"吻合。
#
# 待攻克(卡点): 每家 6x5 布阵的逐格编码尚未完全破解。差分发现:
#   1) 布阵不在固定头0x64-0xC7(两不同布阵文件此区完全相同);
#   2) 全文件在raw/异或/加减delta/固定异或 各读法下均搜不到标准25子数量指纹
#      (定长逐格同码不成立), 疑为变长/位打包/多层编码;
#   3) 0x106C+尾部为固定大小且随布阵变化, 是布阵最可能的存放处, 但解码后仍为高熵数据。
#   最有效的进一步输入: 一个"0步/双方立即结束"的录像, 可干净隔离布阵字节。

from __future__ import annotations

import os
import struct


# ============================================================
# 联众 .jqs 布阵文件解析（已完全破解并验证）
# ============================================================
#
# 与加密的 .JQH 不同, .jqs 布阵块是明文、无编码。
#   - 布阵数据从固定偏移 0x370 开始;
#   - 每格 = 1 个小端 uint32 (4 字节), 取值即棋子编码(高 3 字节恒为 0);
#   - 棋盘 6 行 × 5 列, 行主序; 行间步长为 17 个 uint32(即每行 5 格 + 12 个 uint32 空档),
#     故 cell(row, col) 位于 uint32 索引 (row*17 + col), 字节偏移 0x370 + (row*17+col)*4;
#   - 单个布阵块共 90 个 uint32 = 360 字节(最后一行不含尾部空档)。
#   - 行营(空格)以 0x00 表示。
# 编码表(联众 .jqs 专用, 与 .JQH 内码及 myjgs 均不同):

JQS_LAYOUT_OFFSET = 0x370
JQS_ROW_STRIDE = 17          # 每行的 uint32 步长
JQS_ROW_NUM = 6
JQS_COL_NUM = 5

JQS_CODE_MAP = {
    0x00: "空",   # 行营
    0x01: "军",   # 军长
    0x02: "师",   # 师长
    0x03: "旅",   # 旅长
    0x04: "团",   # 团长
    0x05: "营",   # 营长
    0x06: "连",   # 连长
    0x07: "排",   # 排长
    0x08: "兵",   # 工兵
    0x09: "旗",   # 军旗
    0x0A: "司",   # 司令
    0x0B: "雷",   # 地雷
    0x0C: "弹",   # 炸弹
}

# 联众单字棋名 -> ItemType 枚举 (用于统计引擎)
# 延迟导入避免循环依赖, 由外部在需要时调用 get_char_to_itemtype()
def get_char_to_itemtype():
    from .jgs_types import ItemType
    return {
        "空": ItemType.JQNone,
        "军": ItemType.Junzhang,
        "师": ItemType.Shizhang,
        "旅": ItemType.Lvzhang,
        "团": ItemType.Tuanzhang,
        "营": ItemType.Yinzhang,
        "连": ItemType.Lianzhang,
        "排": ItemType.Paizhang,
        "兵": ItemType.Gongbing,
        "旗": ItemType.Junqi,
        "司": ItemType.Silin,
        "雷": ItemType.Dilei,
        "弹": ItemType.Zhadan,
    }


def parse_jqs_layout(data: bytes, offset: int = JQS_LAYOUT_OFFSET) -> list[list[str]]:
    """解析联众 .jqs 单家布阵, 返回 6x5 的棋子字符矩阵(行主序, '空'=行营)。

    每格为小端 uint32, cell(row,col) 位于 uint32 索引 (row*JQS_ROW_STRIDE + col)。
    """
    need = offset + ((JQS_ROW_NUM - 1) * JQS_ROW_STRIDE + JQS_COL_NUM) * 4
    if len(data) < need:
        raise ValueError("data too short for jqs layout: need %d bytes" % need)

    board = []
    for row in range(JQS_ROW_NUM):
        cells = []
        for col in range(JQS_COL_NUM):
            idx = row * JQS_ROW_STRIDE + col
            (v,) = struct.unpack_from("<I", data, offset + idx * 4)
            if v not in JQS_CODE_MAP:
                raise ValueError("unknown jqs piece code 0x%X at (%d,%d)" % (v, row, col))
            cells.append(JQS_CODE_MAP[v])
        board.append(cells)
    return board


def parse_jqs_file(jqs_file: str | os.PathLike, offset: int = JQS_LAYOUT_OFFSET) -> list[list[str]]:
    """读取 .jqs 文件并解析单家布阵。"""
    with open(jqs_file, "rb") as f:
        return parse_jqs_layout(f.read(), offset)


# ============================================================
# 联众 .JQH 布阵解码（四色已完全破解并验证）
# ============================================================
#
# 已验证的编码格式:
#   - 基准值(基准seed): 文件中 0x1374-0x1377 共4字节, 第二字节(seed_b1)为累积 delta 初始值
#     不同文件基准值不同, 必须从文件动态读取
#   - 每家25个非行营格离散分布在固定地址区间, 每格4字节, 行营格(5个)被跳过
#   - 行营固定位置(0索引): (1,1), (1,3), (2,2), (3,1), (3,3)
#   - 每格4字节格式(红/绿/蓝): [byte0, byte1, byte0, byte1] (byte[0]^byte[1]=0x13)
#   - 每格4字节格式(黑):       [byte0, byte1, byte0+1, byte1] (byte[0]+1=byte[2])
#   - 累积 delta: raw_delta = (byte1[i] - byte1[i-1]) & 0xFF, 初始 prev = seed_b1
#   - 颜色偏移: piece_code = (raw_delta - K_color) & 0xFF
#   - K_color: 红=0x00, 绿=0x64, 蓝=0xC8
#   - 黑方 K 动态由 XOR(byte0, byte1) 决定:
#       XOR∈{0x10, 0x12} → K=0x2B
#       XOR∈{0x14, 0x1C} → K=0x33
#   - delta=0 时回溯前一格棋子类型（同类型连排/行营均适用）
#   - 棋子编码表与 .jqs 完全一致: 0x00空 0x01军 0x02师 ... 0x0C弹

# 红方25个非行营格的离散地址区间（闭区间，每区间含若干4字节块）
_RED_BOARD_RANGES = [
    (0x1378, 0x138B),  # 20B = 5pos  row0
    (0x13BC, 0x13BF),  #  4B = 1pos  row1(跳过行营(1,1))
    (0x13C4, 0x13C7),  #  4B = 1pos
    (0x13CC, 0x13CF),  #  4B = 1pos  row1(跳过行营(1,3))
    (0x1400, 0x1407),  #  8B = 2pos  row2
    (0x140C, 0x1413),  #  8B = 2pos  row2(跳过行营(2,2))
    (0x1444, 0x1447),  #  4B = 1pos  row3
    (0x144C, 0x144F),  #  4B = 1pos  row3(跳过行营(3,1))
    (0x1454, 0x1457),  #  4B = 1pos  row3(跳过行营(3,3))
    (0x1488, 0x149B),  # 20B = 5pos  row4
    (0x14CC, 0x14DF),  # 20B = 5pos  row5
]
_RED_SEED_OFFSET = 0x1374  # 基准值4字节起始偏移
_CAMP_POSITIONS = {(1, 1), (1, 3), (2, 2), (3, 1), (3, 3)}  # 行营(0索引)

# 颜色偏移常量 (红/绿/蓝: K 固定; 黑: K 动态, 单独处理)
JQH_COLOR_K: dict[str, int] = {
    "红": 0x00,
    "绿": 0x64,
    "蓝": 0xC8,
}

# 黑方动态 K 查找表: XOR(byte0, byte1) → K 值
_BLACK_XOR_K: dict[int, int] = {
    0x10: 0x2B,
    0x12: 0x2B,
    0x14: 0x33,
    0x1C: 0x33,
}


def _get_black_k(xor_val: int) -> int:
    """根据黑方格子的 XOR(byte0, byte1) 值返回对应的 K。"""
    return _BLACK_XOR_K.get(xor_val, 0x2B)  # 默认 0x2B


def parse_jqh_color_board(raw: bytes, color: str) -> list[list[str]]:
    """解析红/绿/蓝/黑方 6x5 布阵，行营位置也参与解码。

    所有颜色共用同一组地址区间(0x1378-0x14DF), seed 从 0x1374 动态读取。
    区别仅在于颜色偏移 K_color:
      红=0x00(直接取delta), 绿=0x64, 蓝=0xC8, 黑=动态(XOR查表)

    行营位置也参与解码:
      - delta=0 时回溯前一格棋子类型（而非直接判空）
      - 某些复盘文件初始布局中行营内可能有棋子

    Args:
        raw: JQH 文件原始字节
        color: 颜色名("红"/"绿"/"蓝"/"黑")

    Returns:
        6x5 棋子字符矩阵
    """
    is_black = (color == "黑")
    if is_black:
        k = 0x00  # 占位, 黑方每格动态计算
    elif color in JQH_COLOR_K:
        k = JQH_COLOR_K[color]
    else:
        raise ValueError(f"不支持的颜色: {color!r}, 可选: {list(JQH_COLOR_K) + ['黑']}")

    seed_b1 = raw[_RED_SEED_OFFSET + 1]

    # 提取每格的 byte0 和 byte1 (黑方需要 byte0 计算 XOR)
    groups: list[tuple[int, int]] = []  # (byte0, byte1)
    for start, end_incl in _RED_BOARD_RANGES:
        for off in range(start, end_incl + 1, 4):
            groups.append((raw[off], raw[off + 1]))

    prev_b1 = seed_b1
    prev_piece = "空"
    pieces: list[str] = []
    for b0, b1 in groups:
        raw_delta = (b1 - prev_b1) & 0xFF
        if raw_delta == 0:
            # delta=0: 回溯前一格棋子类型
            pieces.append(prev_piece)
        else:
            if is_black:
                k = _get_black_k(b0 ^ b1)
            code = (raw_delta - k) & 0xFF
            if code > 0x0C:
                code = 0x00
            piece = JQS_CODE_MAP.get(code, "空")
            pieces.append(piece)
            prev_piece = piece
        prev_b1 = b1

    board: list[list[str]] = [["空"] * JQS_COL_NUM for _ in range(JQS_ROW_NUM)]
    idx = 0
    for r in range(JQS_ROW_NUM):
        for c in range(JQS_COL_NUM):
            if (r, c) not in _CAMP_POSITIONS:
                if idx < len(pieces):
                    board[r][c] = pieces[idx]
                idx += 1
    return board


def parse_jqh_red_board(raw: bytes) -> list[list[str]]:
    """解析红方 6x5 布阵 (K=0x00, 等价于 parse_jqh_color_board(raw, '红'))。"""
    return parse_jqh_color_board(raw, "红")

JQH_CODE_MAP = dict(JQS_CODE_MAP)

def parse_jqh_board_from_block(block: bytes) -> list[list[str]]:
    """从 ucDataBlock 中解码单家布阵(6x5)。

    编码格式(已用红顺序阵存储.jqs vs 红顺序排列.JQH 逐格验证):
      - 每4字节为一组: [byte0, byte1, byte0, byte1] (byte[0]^byte[1]=0x13, 信息全在byte[1])
      - 第1组(4字节)为种子, 第2组起每组编码1个棋子
      - 累积 delta: piece_code = (byte1[i] - byte1[i-1]) & 0xFF
      - byte1[-1] = 0x39 (seed的byte1值)
      - 空格: delta = 0 (byte1不变)
      - 与 .jqs 的棋子编码表(0x00空/0x01军/.../0x0C弹)完全一致
    """
    if len(block) < 8:
        return [["空"] * JQS_COL_NUM for _ in range(JQS_ROW_NUM)]

    board = []
    row = []
    prev_b1 = 0x39  # seed byte1
    idx = 4  # 跳过种子(4字节)
    while idx + 1 < len(block) and len(board) < JQS_ROW_NUM:
        b1 = block[idx + 1]
        piece = (b1 - prev_b1) & 0xFF
        if piece > 0x0C:
            piece = 0x00  # 超出范围当空格
        row.append(JQS_CODE_MAP.get(piece, "空"))
        if len(row) == JQS_COL_NUM:
            board.append(row)
            row = []
        if piece != 0x00:
            prev_b1 = b1  # 空格不更新prev, 同类型连排也不更新(与jqs行为一致)
        idx += 4  # 每4字节一组

    while len(board) < JQS_ROW_NUM:
        while len(row) < JQS_COL_NUM:
            row.append("空")
        board.append(row)
        row = []

    return board



def find_jqh_seed_blocks(raw: bytes, seed: tuple[int, int] = (0x2A, 0x39)) -> list[int]:
    """在 JQH 文件中搜索种子对并过滤掉填充区域的误匹配。

    真正的布阵起始位置: 种子对后紧接着有与种子不同的编码对(即第一个非空棋子)。
    填充区域的种子对后会一直重复种子本身(空格不改变编码状态)。
    """
    needle = bytes([seed[0], seed[1], seed[0], seed[1]])
    seed_pair = bytes([seed[0], seed[1]])
    result = []
    start = 0
    while True:
        pos = raw.find(needle, start)
        if pos == -1:
            break
        # 检查种子对后面是否紧跟着与种子不同的字节对(即非填充/非空格区域)
        next_pair_off = pos + 4
        if next_pair_off + 1 < len(raw):
            next_pair = raw[next_pair_off:next_pair_off + 2]
            if next_pair != seed_pair:
                result.append(pos)
        start = pos + 1
    return result


def parse_jqh_layout_from_ucblock(raw: bytes, offset: int) -> list[list[str]]:
    """从 JQH 文件的指定偏移开始, 读取 ucDataBlock 并解码单家布阵。

    布阵数据前4字节为种子(0x2A,0x39), 后续每4字节为一组编码一个棋子。
    解码使用累积 delta: piece = (byte1[i] - byte1[i-1]) & 0xFF, 初始 prev=0x39。
    """
    block_size = min(200, len(raw) - offset)
    block = raw[offset:offset + block_size]
    return parse_jqh_board_from_block(block)


def period2_delta_decode(raw: bytes) -> bytes:
    """周期2累积异或解码: dec[i] = raw[i] ^ raw[i-2]。

    联众 .JQH 用此方式编码，全0明文会呈现为"周期2常量重复段"。
    """
    out = bytearray(len(raw))
    for i in range(len(raw)):
        out[i] = raw[i] ^ (raw[i - 2] if i >= 2 else 0)
    return bytes(out)


def find_zero_gap(raw: bytes, start: int = 0x64) -> tuple[int, int]:
    """定位布阵区与行棋区之间的"纯0填充"段(编码后为周期2常量重复)。

    返回 (gap_start, gap_end)；gap_start 即布阵区的结束偏移。
    """
    i = start
    best = None
    while i < len(raw) - 1:
        a, b = raw[i], raw[i + 1]
        j = i
        while j + 1 < len(raw) and raw[j] == a and raw[j + 1] == b:
            j += 2
        if j - i >= 64:  # 足够长的重复段
            best = (i, j)
            break
        i += 2
    return best if best else (len(raw), len(raw))


class LianzhongReplay:
    """联众四国军棋复盘解析（布阵读取初版）。"""

    HEADER_TEXT_END = 0x64
    BOARD_REGION_START = 0x64

    def __init__(self, jqh_file: str | os.PathLike):
        self.path = str(jqh_file)
        with open(self.path, "rb") as f:
            self.raw = f.read()

        # 头部文本（GBK）
        self.header_text = self._decode_header_text()
        # 布阵与行棋之间的纯0填充段
        self.gap_start, self.gap_end = find_zero_gap(self.raw, self.BOARD_REGION_START)
        # 布阵候选区
        self.board_region = self.raw[self.BOARD_REGION_START:self.gap_start]
        # 周期2累积异或解码后的全文
        self.decoded = period2_delta_decode(self.raw)

    def _decode_header_text(self) -> str:
        seg = self.raw[0x24:self.HEADER_TEXT_END]
        seg = seg.split(b"\x00", 1)[0]
        try:
            return seg.decode("gbk", errors="replace")
        except Exception:
            return seg.decode("latin1", errors="replace")

    def hex_dump(self, start: int, end: int, decoded: bool = False) -> str:
        """十六进制查看指定区间(可选查看 period2 解码后的字节)。"""
        buf = self.decoded if decoded else self.raw
        lines = []
        for o in range(start, min(end, len(buf)), 16):
            lines.append(f"{o:04X}: " + " ".join(f"{b:02X}" for b in buf[o:o + 16]))
        return "\n".join(lines)

    def parse_layouts(self, seed_offsets: list[int] | None = None) -> list[list[list[str]]] | None:
        """解析所有可识别的布阵(多家)。

        遍历文件中所有种子对, 尝试解码每家布阵;
        无法解码的跳过, 返回解码成功的列表。
        """
        if seed_offsets is None:
            seed_offsets = find_jqh_seed_blocks(self.raw)
        if not seed_offsets:
            return None
        boards = []
        for off in seed_offsets:
            block = self.raw[off:off + 128]  # 最多读128字节(30格*4=120+种子4)
            board = parse_jqh_board_from_block(block)
            boards.append(board)
        return boards if boards else None


def decode_jqh_all_boards(raw: bytes) -> dict[str, list[list[str]]]:
    """用完整解码逻辑(棋子位置识别+四色累积delta)解析 JQH 文件, 返回四家 6x5 布阵。

    与 parse_jqh_color_board 不同, 本函数使用 chess_board_diff_process 的 17×17 棋盘
    位置识别, 正确处理绿/蓝/黑方在棋盘不同区域的数据。
    """
    import sys
    _parent = os.path.join(os.path.dirname(__file__), "..")
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    from .chess_board_diff_process import load_groups, classify_groups, GRID_SIZE

    groups = load_groups(raw)
    is_piece = classify_groups(groups)
    seed_b1 = groups[0][1]

    # 收集所有棋子位置
    piece_positions = []
    for i in range(1, len(is_piece)):
        if is_piece[i]:
            gi = i - 1
            row = gi // GRID_SIZE
            col = gi % GRID_SIZE
            piece_positions.append((i, row, col))

    # 累积 delta 解码 (zip 方式)
    COLORS = ['红', '绿', '蓝']
    _BLACK_XOR_K_LIST = {
        0x0C: [0x2B, 0x13], 0x10: [0x2B], 0x12: [0x2B],
        0x14: [0x33], 0x1C: [0x33],
        0x2C: [0x2B, 0x53], 0x6C: [0x53], 0xEC: [0x53],
    }

    def _decode_piece(prev_b1_val, gi, row, col):
        g = groups[gi]
        b0, b1 = g[0], g[1]
        delta = (b1 - prev_b1_val) & 0xFF
        for color in COLORS:
            k = JQH_COLOR_K[color]
            code = (delta - k) & 0xFF
            if code <= 0x0C:
                return b1, (row, col), (color, JQS_CODE_MAP.get(code, '?'))
        xor_val = b0 ^ b1
        k_list = _BLACK_XOR_K_LIST.get(xor_val)
        if k_list:
            for k in k_list:
                code = (delta - k) & 0xFF
                if code <= 0x0C:
                    return b1, (row, col), ('黑', JQS_CODE_MAP.get(code, '?'))
        return b1, (row, col), ('?', '?')

    # 构建 (prev_b1, gi, row, col) 序列, 用 zip 取相邻对
    prev_list = [seed_b1] + [groups[gi][1] for gi, _, _ in piece_positions[:-1]]
    gi_list = [gi for gi, _, _ in piece_positions]
    row_list = [r for _, r, _ in piece_positions]
    col_list = [c for _, _, c in piece_positions]

    results = [_decode_piece(p, g, r, c)
               for p, g, r, c in zip(prev_list, gi_list, row_list, col_list)]

    decoded = {}
    for _, (row, col), (color, piece) in results:
        if piece == '空' or piece == '?':
            continue
        decoded[(row, col)] = (color, piece)

    # 提取四家子方阵(同时提取棋子名和颜色)
    def extract_subgrid(row_range, col_range):
        piece_grid = []
        color_grid = []
        for r in range(*row_range):
            piece_row = []
            color_row = []
            for c in range(*col_range):
                if (r, c) in decoded:
                    piece_row.append(decoded[(r, c)][1])
                    color_row.append(decoded[(r, c)][0])
                else:
                    piece_row.append('空')
                    color_row.append('空')
            piece_grid.append(piece_row)
            color_grid.append(color_row)
        return piece_grid, color_grid

    def rotate_ccw_90(grid):
        R, C = len(grid), len(grid[0])
        return [[grid[r][c] for r in range(R)] for c in range(C - 1, -1, -1)]

    def rotate_cw_90(grid):
        R, C = len(grid), len(grid[0])
        return [[grid[R - 1 - r][c] for r in range(R)] for c in range(C)]

    def rotate_180(grid):
        R, C = len(grid), len(grid[0])
        return [[grid[R - 1 - r][C - 1 - c] for c in range(C)] for r in range(R)]

    boards = {}
    boards_with_color = {}
    # 红方: range(11,17) x range(6,11), 6行5列, 不旋转
    pg, cg = extract_subgrid((11, 17), (6, 11))
    boards['红'] = pg; boards_with_color['红'] = cg
    # 绿方: range(6,11) x range(0,6), 5行6列, 逆时针旋转90度
    pg, cg = extract_subgrid((6, 11), (0, 6))
    boards['绿'] = rotate_ccw_90(pg); boards_with_color['绿'] = rotate_ccw_90(cg)
    # 蓝方: range(0,6) x range(6,11), 6行5列, 旋转180度
    pg, cg = extract_subgrid((0, 6), (6, 11))
    boards['蓝'] = rotate_180(pg); boards_with_color['蓝'] = rotate_180(cg)
    # 黑方: range(6,11) x range(11,17), 5行6列, 顺时针旋转90度
    pg, cg = extract_subgrid((6, 11), (11, 17))
    boards['黑'] = rotate_cw_90(pg); boards_with_color['黑'] = rotate_cw_90(cg)

    return boards, boards_with_color


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else (
        os.path.join(os.path.dirname(__file__), "..", "test",
                     "test_lianzhong_jqh", "test1.JQH"))
    r = LianzhongReplay(path)
    print("文件:", r.path, "大小:", len(r.raw))
    print("头部文本:", r.header_text)
    print("纯0填充段: 0x%X – 0x%X" % (r.gap_start, r.gap_end))
    print("布阵候选区: 0x%X – 0x%X (%d 字节)" % (
        r.BOARD_REGION_START, r.gap_start, len(r.board_region)))
    print("--- 布阵区 RAW ---")
    print(r.hex_dump(r.BOARD_REGION_START, r.gap_start))
