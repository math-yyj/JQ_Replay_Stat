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


# ============================================================
# 联众 .JQH 用户名解码
# ============================================================
# 编码方案: encoded[i] = ((encoded[i-1] + plaintext[i]) % 0x100) ^ 0x13
#   - 第一个字节不加密, 直接是明文
#   - 后续字节依赖前一个 encoded 字节
#   - 用户名最长20字节, 不足时用 0x00 填充

JQH_USERNAME_OFFSET = 0x64   # 四家用户名起始偏移
JQH_USERNAME_LEN = 20        # 每家用户名最大长度
JQH_USERNAME_COUNT = 4       # 四家
JQH_USERNAME_XOR_KEY = 0x13  # XOR密钥


def decode_jqh_username(raw: bytes) -> str:
    """解码单个联众用户名 (20字节编码数据 -> 字符串)

    编码公式: encoded[i] = ((encoded[i-1] + plaintext[i]) % 0x100) ^ 0x13
    解码公式: plaintext[i] = ((encoded[i] ^ 0x13) - encoded[i-1]) % 0x100
    第一个字节不加密, 直接是明文。
    """
    if len(raw) < JQH_USERNAME_LEN:
        return ""
    decoded = bytearray(JQH_USERNAME_LEN)
    prev_encoded = 0
    for i in range(JQH_USERNAME_LEN):
        encoded_byte = raw[i]
        if i == 0:
            decoded[i] = encoded_byte  # 第一个字节不加密
        else:
            decoded[i] = ((encoded_byte ^ JQH_USERNAME_XOR_KEY) - prev_encoded) % 0x100
        prev_encoded = encoded_byte
    # 截断到第一个 \x00
    null_pos = decoded.find(0)
    if null_pos >= 0:
        decoded = decoded[:null_pos]
    try:
        return decoded.decode("gbk")
    except UnicodeDecodeError:
        return decoded.decode("utf-8", errors="replace")


def decode_jqh_all_usernames(raw: bytes) -> list[str]:
    """解码联众复盘文件中四家用户名 [红, 绿, 蓝, 黑]

    注意: 整个80字节是连续编码链，每20字节为一家。
    仅红方首字母不加密，后续所有字节(含绿蓝黑首字母)都根据前一个编码字节加密。
    """
    block = raw[JQH_USERNAME_OFFSET:JQH_USERNAME_OFFSET + JQH_USERNAME_COUNT * JQH_USERNAME_LEN]
    if len(block) < JQH_USERNAME_COUNT * JQH_USERNAME_LEN:
        return [""] * JQH_USERNAME_COUNT

    decoded_all = bytearray(JQH_USERNAME_COUNT * JQH_USERNAME_LEN)
    prev_encoded = 0
    for i in range(JQH_USERNAME_COUNT * JQH_USERNAME_LEN):
        encoded_byte = block[i]
        if i == 0:
            decoded_all[i] = encoded_byte
        else:
            decoded_all[i] = ((encoded_byte ^ JQH_USERNAME_XOR_KEY) - prev_encoded) % 0x100
        prev_encoded = encoded_byte

    # 按20字节分割为四家
    names = []
    for player_idx in range(JQH_USERNAME_COUNT):
        start = player_idx * JQH_USERNAME_LEN
        chunk = decoded_all[start:start + JQH_USERNAME_LEN]
        null_pos = chunk.find(0)
        if null_pos >= 0:
            chunk = chunk[:null_pos]
        try:
            names.append(chunk.decode("gbk"))
        except UnicodeDecodeError:
            names.append(chunk.decode("utf-8", errors="replace"))
    return names


# ============================================================
# 联众 .JQH 主视角颜色解码
# ============================================================
# 编码公式: ((data[0xB7] + 主视角颜色) % 0x100) ^ 0x13 = data[0xB8]
# 解码公式: 主视角颜色 = ((data[0xB8] ^ 0x13) - data[0xB7]) % 0x100
# 颜色映射: 红=0, 绿=1, 蓝=2, 黑=3

JQH_MAIN_COLOR_OFFSET_PREV = 0xB7   # 前一个编码字节
JQH_MAIN_COLOR_OFFSET_CURR = 0xB8   # 当前编码字节
JQH_MAIN_COLOR_XOR_KEY = 0x13       # XOR密钥

# 主视角颜色索引 -> 名称
JQH_MAIN_COLOR_MAP = {0: "红", 1: "绿", 2: "蓝", 3: "黑"}


def decode_jqh_main_color(raw: bytes) -> tuple[int, str]:
    """解码联众复盘的主视角颜色

    编码公式: ((data[0xB7] + color) % 0x100) ^ 0x13 = data[0xB8]
    解码公式: color = ((data[0xB8] ^ 0x13) - data[0xB7]) % 0x100

    Returns:
        (color_index, color_name)  color_index: 0=红, 1=绿, 2=蓝, 3=黑
    """
    if len(raw) < JQH_MAIN_COLOR_OFFSET_CURR + 1:
        return -1, "数据不足"

    encoded = raw[JQH_MAIN_COLOR_OFFSET_CURR]
    prev = raw[JQH_MAIN_COLOR_OFFSET_PREV]

    color = ((encoded ^ JQH_MAIN_COLOR_XOR_KEY) - prev) % 0x100

    if color in JQH_MAIN_COLOR_MAP:
        return color, JQH_MAIN_COLOR_MAP[color]
    else:
        return color, f"未知({color})"


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


# ============================================================
# 联众 .JQH 行棋区解码（已完全破解并用200组配对验证）
# ============================================================
#
# 编码: 行棋区自 0xC8 起延续用户信息区的 XOR 累积链, 初始 prev = raw[0xC7]（不归零）:
#   plain[i] = ((raw[i] ^ 0x13) - prev) & 0xFF,  prev = raw[i]
# 结构(链式解码后):
#   偏移0: 4字节小端 = 行棋步数 N（含 Pass/GiveUp）
#   偏移4: N × 4字节 = (起点行, 起点列, 终点行, 终点列), 行列均0基
# 哨兵: FE FE FE FE = GiveUp,  FF FF FF FF = Pass
# 坐标系: 解码坐标为"录制者视角"帧, 每文件是8种二面体变换之一的常量变换,
#         需按自洽判定解析(见 resolve_move_transform), 之后得到17×17全局坐标。

JQH_MOVE_START = 0xC8
JQH_CHAIN_PREV_OFFSET = 0xC7
JQH_CHAIN_XOR_KEY = 0x13

JQH_MOVE_GIVEUP = (0xFE,) * 4
JQH_MOVE_PASS = (0xFF,) * 4


def decode_jqh_move_region(raw: bytes) -> bytes:
    """链式解码行棋区(0xC8 起至文件尾), prev 从 raw[0xC7] 连续, 不归零。"""
    out = bytearray()
    prev = raw[JQH_CHAIN_PREV_OFFSET]
    for i in range(JQH_MOVE_START, len(raw)):
        out.append(((raw[i] ^ JQH_CHAIN_XOR_KEY) - prev) & 0xFF)
        prev = raw[i]
    return bytes(out)


def parse_jqh_moves_raw(raw: bytes) -> tuple[int, list[tuple[int, int, int, int] | str]]:
    """解析行棋区, 返回 (步数, 步列表)。

    每步为坐标四元组 (r1, c1, r2, c2)(录制者视角帧) 或 'Pass'/'GiveUp'。
    """
    dec = decode_jqh_move_region(raw)
    if len(dec) < 4:
        raise ValueError("jqh move region too short")
    count = int.from_bytes(dec[0:4], "little")
    moves: list[tuple[int, int, int, int] | str] = []
    for k in range(count):
        off = 4 + 4 * k
        quad = tuple(dec[off:off + 4])
        if len(quad) < 4:
            break
        if quad == JQH_MOVE_GIVEUP:
            moves.append("GiveUp")
        elif quad == JQH_MOVE_PASS:
            moves.append("Pass")
        else:
            moves.append(quad)  # type: ignore[arg-type]
    return count, moves


# 8种二面体变换 (行,列) -> (行,列); 坐标域 0..16
_JQH_MOVE_TRANSFORMS: dict[str, object] = {
    "I":    lambda r, c: (r, c),
    "R90":  lambda r, c: (c, 16 - r),
    "R180": lambda r, c: (16 - r, 16 - c),
    "R270": lambda r, c: (16 - c, r),
    "FH":   lambda r, c: (r, 16 - c),
    "FV":   lambda r, c: (16 - r, c),
    "TR":   lambda r, c: (c, r),
    "ATR":  lambda r, c: (16 - c, 16 - r),
}


def jqh_transform_names() -> list[str]:
    return list(_JQH_MOVE_TRANSFORMS)


def apply_jqh_move_transform(name: str, r: int, c: int) -> tuple[int, int]:
    fn = _JQH_MOVE_TRANSFORMS[name]  # type: ignore[assignment]
    return fn(r, c)  # type: ignore[operator]


def resolve_move_transform(
    moves: list[tuple[int, int, int, int] | str],
    pieces: dict[tuple[int, int], tuple[str, str]] | None = None,
) -> tuple[str, float]:
    """判定行棋坐标 -> 布阵网格的变换, 返回 (变换名, 违规率)。

    分类式判定(非统一范式):
      1) 格式常量: 行棋四元组坐标相对布阵网格恒为转置 TR
         (200 组配对验证: 唯一 0 违规变换恒为 TR);
      2) TR 严格推演 0 违规即采信; 否则退回 8 变换全量推演,
         取违规数最小者, 平局倾向 TR。
    注意: 文本 .JQH.TXT 导出帧自身带旋转/镜像变体(共 8 种, 随座位与
    镜像变化), 文本坐标比对所需的变换与本函数结果不同, 需另行按坐标
    全等搜索(见 test_sample/test_jqh_moves_regression.py)。
    """
    quad_moves = [m for m in moves if not isinstance(m, str)]
    if not quad_moves or not pieces:
        return _JQH_PIECES_TRANSFORM, 0.0

    best = None
    for name in _JQH_MOVE_TRANSFORMS:
        res = simulate_jqh_game(moves, pieces, name)
        key = (res["viol"], name != _JQH_PIECES_TRANSFORM, name)
        if best is None or key < best[0]:
            best = (key, name, res["viol_rate"])
    assert best is not None
    return best[1], best[2]


# 格式常量: 行棋坐标相对布阵网格恒为转置(200/200 验证, 见 resolve_move_transform)
_JQH_PIECES_TRANSFORM = "TR"

# 二面体变换求逆 (R90/R270 互逆, 其余为对合)
_JQH_TRANSFORM_INVERSE = {
    "I": "I", "R90": "R270", "R180": "R180", "R270": "R90",
    "FH": "FH", "FV": "FV", "TR": "TR", "ATR": "ATR",
}


def jqh_transform_compose(outer: str, inner: str) -> str:
    """求复合变换 F = outer ∘ inner (先 inner 后 outer), 返回变换名。"""
    fo = _JQH_MOVE_TRANSFORMS[outer]
    fi = _JQH_MOVE_TRANSFORMS[inner]
    probe = [(r, c) for r in (0, 3, 8, 12, 16) for c in (0, 5, 8, 11, 16)]
    target = [fo(*fi(r, c)) for r, c in probe]  # type: ignore[operator]
    for name, fn in _JQH_MOVE_TRANSFORMS.items():
        if [fn(r, c) for r, c in probe] == target:
            return name
    raise ValueError(f"uncomposable transforms: {outer} ∘ {inner}")

# 行营(空, 不可被攻击) 在全局17x17的坐标: 各家本地 (1,1)(1,3)(2,2)(3,1)(3,3)
_JQH_CAMP_LOCAL = ((1, 1), (1, 3), (2, 2), (3, 1), (3, 3))


def jqh_global_camps() -> set[tuple[int, int]]:
    """四家行营在全局 17x17 的格子集合 (上蓝/下红/左绿/右黑 固定视角)。"""
    camps = set()
    for i, j in _JQH_CAMP_LOCAL:
        camps.add((11 + i, 6 + j))   # 下(红)
        camps.add((5 - i, 10 - j))   # 上(蓝)
        camps.add((6 + j, 5 - i))    # 左(绿)
        camps.add((10 - j, 11 + i))  # 右(黑)
    return camps


# 固定行棋色序: 下(红)→左(绿)→上(蓝)→右(黑), 依 glworld TURN_ORDER 推得
_JQH_TURN_CYCLE = ("红", "绿", "蓝", "黑")


def simulate_jqh_game(
    moves: list[tuple[int, int, int, int] | str],
    pieces: dict[tuple[int, int], tuple[str, str]] | None,
    transform: str,
) -> dict:
    """按四国军棋规则严格推演(指定变换), 返回违规统计与终局信息。

    规则: 吃子按 glworld _resolve_combat; 行营内子不可被攻击;
          GiveUp=该家认输并清除其子; 扛旗清除被扛方全部子;
          连跳2次判负清除; 同色落点=违规(不可吃己子)。
    违规项: empty(起点无子) / immov(起点雷旗) / own(吃己子) /
            camp(攻击行营内子) / owner(非轮走方行棋)。
    """
    fn = _JQH_MOVE_TRANSFORMS[transform]
    cells: dict[tuple[int, int], tuple[str, str]] = dict(pieces or {})
    camps = jqh_global_camps()
    flag_cells = {pos for pos, v in cells.items() if v[1] == "旗"}

    # 轮次走向有两种可能, 按违规率取优
    result = _simulate_walk(moves, cells, fn, camps, flag_cells)
    viol_detail = result["viol_detail"]
    total = result["total"]
    viol = sum(viol_detail.values())
    out = {
        "viol_detail": viol_detail,
        "viol": viol,
        "viol_rate": viol / total if total else 0.0,
        "total": total,
        "flag_hits": result["flag_hits"],
        "immov_starts": viol_detail.get("immov", 0),
        "eliminated": result["eliminated"],
        "last_mover": result["last_mover"],
    }
    return out


def _simulate_walk(moves, cells, fn, camps, flag_cells):
    """单向推演: 轮次循环方向由前几个行棋方自适应拟合(两种走向取优)。"""
    best = None
    for direction in ("FWD", "REV"):
        r = _walk(moves, dict(cells), fn, camps, flag_cells, direction)
        if best is None or sum(r["viol_detail"].values()) < sum(best["viol_detail"].values()):
            best = r
    return best


def _walk(moves, cells, fn, camps, flag_cells, direction):
    from .glworld_replay import _resolve_combat

    # 分类: 按参与颜色数定轮转(四人对局4家轮转, 二人对局2家交替)
    part = {v[0] for v in cells.values()}
    cycle = [c for c in _JQH_TURN_CYCLE if c in part] or list(_JQH_TURN_CYCLE)
    if direction == "REV":
        cycle = [cycle[0]] + cycle[1:][::-1]

    # 起点颜色 -> 循环偏移; 开局的 Pass/GiveUp 各占一轮, 须回退相应轮数
    lead = 0
    first_src = None
    for m in moves:
        if isinstance(m, str):
            lead += 1
            continue
        first_src = cells.get(fn(m[0], m[1]))
        break
    offset = 0
    if first_src is not None and first_src[0] in cycle:
        offset = (cycle.index(first_src[0]) - lead) % len(cycle)

    active = set(cycle)
    skip: dict[str, int] = {c: 0 for c in cycle}
    last_mover = None
    viol_detail: dict[str, int] = {}
    flag_hits = 0
    eliminated: dict[str, str] = {}
    idx = offset

    def next_turn():
        nonlocal idx
        for _ in range(len(cycle) + 1):
            c = cycle[idx % len(cycle)]
            idx += 1
            if c in active:
                return c
        return None

    def out_player(color, cause):
        if color in active:
            active.discard(color)
            eliminated[color] = cause
        for k in [k for k, v in cells.items() if v[0] == color]:
            cells.pop(k, None)

    def bump(key):
        viol_detail[key] = viol_detail.get(key, 0) + 1

    mover = next_turn()
    for m in moves:
        if isinstance(m, str):
            if m == "GiveUp":
                if mover is not None:
                    out_player(mover, "认输")
                mover = next_turn()
            else:  # Pass
                if mover is not None:
                    skip[mover] = 0
                    last_mover = mover
                mover = next_turn()
            continue

        s = fn(m[0], m[1]); e = fn(m[2], m[3])
        src = cells.get(s)
        if src is None:
            bump("empty")
            mover = next_turn()
            continue
        if src[1] in ("雷", "旗"):
            bump("immov")
        if mover is not None and src[0] != mover:
            bump("owner")
        # 跳过判负
        if last_mover is not None and mover is not None:
            order = [c for c in cycle]
            i0 = order.index(last_mover)
            for step in range(1, len(order)):
                c = order[(i0 + step) % len(order)]
                if c == mover:
                    break
                if c in active:
                    skip[c] += 1
                    if skip[c] >= 2:
                        out_player(c, "超时判负")
        if mover is not None:
            skip[mover] = 0
        last_mover = mover if mover is not None else last_mover

        cells.pop(s, None)
        dst = cells.get(e)
        if dst is not None and dst[0] == src[0]:
            bump("own")
        if dst is not None and e in camps:
            bump("camp")
        if e in flag_cells:
            flag_hits += 1
        if dst is None:
            cells[e] = src
        else:
            res = _resolve_combat(src[1], dst[1])
            if res == "flag_captured":
                out_player(dst[0], "被挖旗")
                cells[e] = src
            elif res == "attacker_wins":
                cells[e] = src
            elif res == "both_die":
                cells.pop(e, None)
        mover = next_turn()

    return {"viol_detail": viol_detail, "flag_hits": flag_hits,
            "eliminated": eliminated, "last_mover": last_mover,
            "total": len(moves)}


def determine_jqh_end_result(
    eliminated: dict[str, str],
    piece_colors: set[str] | None = None,
) -> tuple[list[str], str]:
    """终局判定 (分类规则)。

    - 二人对局(仅两家参与): 两家为对手, 恰一家存活 -> 该方"胜负已分";
    - 四人对局: 联盟 = 对家 (蓝,红) 上下 vs (绿,黑) 左右; 恰有一方联盟两家
      均出局(被挖旗/认输/判负) 且对方尚有在场 -> 对方联盟胜;
    - 否则(双方联盟的两家都被挖旗或认输/四家全灭, 或双方均未被消灭
      —— 记录中断、超时、和局) -> ("异常中断或和局")。
    """
    colors = set(piece_colors) if piece_colors else set(_JQH_TURN_CYCLE)
    if len(colors) == 2:
        alive = [c for c in colors if c not in eliminated]
        if len(alive) == 1:
            return [alive[0]], "胜负已分"
        return [], "异常中断或和局"
    alliances = (("蓝", "红"), ("绿", "黑"))
    dead = set(eliminated)
    surv_a = [c for c in alliances[0] if c not in dead]
    surv_b = [c for c in alliances[1] if c not in dead]
    a_out = not surv_a
    b_out = not surv_b
    if a_out and not b_out:
        return list(alliances[1]), "胜负已分"
    if b_out and surv_a:
        return list(alliances[0]), "胜负已分"
    return [], "异常中断或和局"


def parse_jqh_moves(
    raw: bytes,
    pieces: dict[tuple[int, int], tuple[str, str]] | None = None,
) -> tuple[list[tuple[int, int, int, int] | str], str, float]:
    """解析行棋记录并输出全局(布阵网格)坐标。

    返回 (步列表, 使用的变换名, 违规率)。步为 (r1,c1,r2,c2) 全局坐标或 'Pass'/'GiveUp'。
    """
    _, moves_raw = parse_jqh_moves_raw(raw)
    name, score = resolve_move_transform(moves_raw, pieces)

    def t(r, c):
        return apply_jqh_move_transform(name, r, c)

    out: list[tuple[int, int, int, int] | str] = []
    for m in moves_raw:
        if isinstance(m, str):
            out.append(m)
        else:
            r1, c1 = t(m[0], m[1])
            r2, c2 = t(m[2], m[3])
            out.append((r1, c1, r2, c2))
    return out, name, score


def analyze_jqh_game(raw: bytes) -> dict:
    """完整分析单个 JQH: 四家布阵 + 行棋全局坐标 + 推演终局判定。

    返回 dict: pieces / main_color / moves / transform / viol /
               eliminated / winners / result_desc。
    """
    pieces = jqh_global_pieces(raw)
    main_color, main_color_name = decode_jqh_main_color(raw)
    moves, transform, _score = parse_jqh_moves(raw, pieces)
    sim = simulate_jqh_game(moves, pieces, "I")
    winners, desc = determine_jqh_end_result(
        sim["eliminated"], {c for c, _ in pieces.values()})
    return {
        "pieces": pieces,
        "main_color": main_color_name,
        "moves": moves,
        "transform": transform,
        "viol": sim["viol_detail"],
        "eliminated": sim["eliminated"],
        "winners": winners,
        "result_desc": desc,
    }


# ============================================================
# 联众 .JQH 通用引擎: 状态迭代 / 司令军长轨迹 / 空炸筛选
# ============================================================

# 座位映射: 联众固定视角(蓝上/绿左/红下/黑右) -> QQ 规范色(黄上/紫左/绿下/蓝右)
JQH_BLOCK_TO_QQ = {"蓝": "黄", "绿": "紫", "红": "绿", "黑": "蓝"}
JQH_BLOCK_SEAT = {"蓝": "上", "绿": "左", "红": "下", "黑": "右"}

# "最大仅吃到连长"允许的击杀棋子(与 QQ 侧一致)
JQH_SMALL_KILL_PIECES = ("连长", "排长", "工兵")

_JQH_NAME_FULL = {
    "司": "司令", "军": "军长", "师": "师长", "旅": "旅长",
    "团": "团长", "营": "营长", "连": "连长", "排": "排长",
    "兵": "工兵", "弹": "炸弹", "雷": "地雷", "旗": "军旗",
}


def _jqh_replay_walk(raw: bytes):
    """联众 .jqh 推演核心, 依次产出 (步号, board, ev)。

    board: {(r,c): (块色, 棋子字)}, 每步更新(对象复用, 消费方立即读取);
    ev: None(初始) 或 dict:
        {'kind':'move', 'start','end','attacker','target','result'}
        {'kind':'pass'} / {'kind':'giveup','color'}
      其中 attacker/target 为 (块色,棋子字) 或 None; result ∈
        move/attacker_wins/defender_wins/both_die/flag_captured/error;
      'cleared': [(块色, 原因)] 记录本步被清子的玩家(认输/连跳判负/扛旗)。
    轮转方向按开局行棋颜色拟合; 开局 Pass/GiveUp 各占一轮;
    连续两次未行棋判负清子; 扛旗清除被扛方全部子。
    """
    from .glworld_replay import _resolve_combat

    board = dict(jqh_global_pieces(raw))
    _, moves_raw = parse_jqh_moves_raw(raw)
    fn = _JQH_MOVE_TRANSFORMS[_JQH_PIECES_TRANSFORM]

    part = {v[0] for v in board.values()}
    cycle = [c for c in _JQH_TURN_CYCLE if c in part] or list(_JQH_TURN_CYCLE)

    # 轮转方向拟合: 取开局若干手的行棋颜色, 看相邻转换更贴合哪个走向
    probe: list[str] = []
    b2 = dict(board)
    for m in moves_raw:
        if isinstance(m, str):
            if probe:
                break
            continue
        s = fn(m[0], m[1])
        e = fn(m[2], m[3])
        p = b2.get(s)
        if p is None:
            continue
        probe.append(p[0])
        b2.pop(s, None)
        tgt = b2.get(e)
        if tgt is not None and tgt[0] != p[0]:
            res = _resolve_combat(p[1], tgt[1])
            if res in ("attacker_wins", "flag_captured"):
                b2[e] = p
            elif res == "both_die":
                b2.pop(e, None)
        else:
            b2[e] = p
        if len(probe) >= 8:
            break
    fwd = rev = 0
    for a, b in zip(probe, probe[1:]):
        if a not in cycle or b not in cycle:
            continue
        i = cycle.index(a)
        if cycle[(i + 1) % len(cycle)] == b:
            fwd += 1
        if cycle[(i - 1) % len(cycle)] == b:
            rev += 1
    if rev > fwd:
        cycle = [cycle[0]] + cycle[1:][::-1]

    # 开局哨兵各占一轮, 起始偏移回退
    lead = 0
    first_src = None
    for m in moves_raw:
        if isinstance(m, str):
            lead += 1
            continue
        first_src = board.get(fn(m[0], m[1]))
        break
    offset = 0
    if first_src is not None and first_src[0] in cycle:
        offset = (cycle.index(first_src[0]) - lead) % len(cycle)

    idx = offset
    active = set(cycle)
    skip = {c: 0 for c in cycle}
    last_mover = None

    def next_turn():
        nonlocal idx
        for _ in range(len(cycle) + 1):
            c = cycle[idx % len(cycle)]
            idx += 1
            if c in active:
                return c
        return None

    def clear_color(color, cause, cleared):
        if color in active:
            active.discard(color)
            cleared.append((color, cause))
        for k in [k for k, v in board.items() if v[0] == color]:
            del board[k]

    yield 0, board, None
    mover = next_turn()
    for no, m in enumerate(moves_raw, 1):
        cleared = []
        if isinstance(m, str):
            if m == "GiveUp":
                if mover is not None:
                    clear_color(mover, "认输", cleared)
                mover = next_turn()
                ev = {"kind": "giveup", "color": None, "cleared": cleared}
            else:
                if mover is not None:
                    skip[mover] = 0
                    last_mover = mover
                mover = next_turn()
                ev = {"kind": "pass", "cleared": cleared}
            yield no, board, ev
            continue

        # 连跳判负
        if last_mover is not None and mover is not None:
            i0 = cycle.index(last_mover)
            for step in range(1, len(cycle)):
                c = cycle[(i0 + step) % len(cycle)]
                if c == mover:
                    break
                if c in active:
                    skip[c] += 1
                    if skip[c] >= 2:
                        clear_color(c, "超时判负", cleared)
        if mover is not None:
            skip[mover] = 0
        last_mover = mover if mover is not None else last_mover

        s = fn(m[0], m[1])
        e = fn(m[2], m[3])
        attacker = board.get(s)
        target = board.get(e)
        ev = {"kind": "move", "start": s, "end": e,
              "attacker": attacker, "target": target,
              "result": "error", "cleared": cleared}
        if attacker is None:
            ev["result"] = "error"
        elif target is not None and target[0] == attacker[0]:
            ev["result"] = "error"
        elif target is None:
            del board[s]
            board[e] = attacker
            ev["result"] = "move"
        else:
            res = _resolve_combat(attacker[1], target[1])
            ev["result"] = res
            del board[s]
            if res == "attacker_wins":
                board[e] = attacker
            elif res == "both_die":
                board.pop(e, None)
            elif res == "defender_wins":
                pass
            elif res == "flag_captured":
                board[e] = attacker
                clear_color(target[0], "被扛旗", cleared)
        mover = next_turn()
        yield no, board, ev


def iter_jqh_states(raw: bytes):
    """按行棋顺序推演全局状态, 依次产出 (步号, 状态dict)。

    首个产出为初始布阵(步号0), 之后每步产出一次; 含认输/扛旗/连跳判负清子。
    状态 dict: {(r,c): (QQ规范色, ItemType)}; 消费方应立即读取, 勿保存引用。
    """
    c2i = get_char_to_itemtype()
    for no, board, _ev in _jqh_replay_walk(raw):
        yield no, {
            pos: (JQH_BLOCK_TO_QQ[col], c2i[ch])
            for pos, (col, ch) in board.items()
        }


def trace_jqh_heroes(raw: bytes) -> list[dict]:
    """追踪四家司令/军长轨迹(联众 .jqh)。

    返回 dict 列表: block(块色)/seat/player/name/kills/death_step/
    death_kind/death_by_bomb。击杀含主动吃子与被撞反吃; 被炸弹炸死 =
    与敌方炸弹同尽。阵亡方式: 被炸弹炸死/被吃/同尽/触雷/清子。
    """
    heroes: list[dict] = []
    hero_at: dict[tuple[int, int], dict] = {}
    names = decode_jqh_all_usernames(raw)
    block_name_idx = {"红": 0, "绿": 1, "蓝": 2, "黑": 3}

    def die(h, step, kind, by_bomb):
        h["death_step"] = step
        h["death_kind"] = kind
        h["death_by_bomb"] = by_bomb

    for no, board, ev in _jqh_replay_walk(raw):
        if no == 0:
            for pos, (col, ch) in board.items():
                if ch in ("司", "军"):
                    h = {
                        "block": col, "seat": JQH_BLOCK_SEAT[col],
                        "player": names[block_name_idx[col]],
                        "name": _JQH_NAME_FULL[ch], "char": ch, "pos": pos,
                        "kills": [], "death_step": None,
                        "death_kind": "在场", "death_by_bomb": False,
                    }
                    heroes.append(h)
                    hero_at[pos] = h
            continue
        for color, cause in ev.get("cleared", ()):
            for h in heroes:
                if h["block"] == color and h["death_kind"] == "在场":
                    die(h, no, "清子", False)
        if ev["kind"] != "move":
            continue
        atk = hero_at.get(ev["start"])
        dfn = hero_at.get(ev["end"])
        res = ev["result"]
        a_name = _JQH_NAME_FULL.get(ev["attacker"][1], "?") if ev["attacker"] else ""
        d_name = _JQH_NAME_FULL.get(ev["target"][1], "?") if ev["target"] else ""
        if res == "move":
            if atk:
                hero_at.pop(ev["start"], None)
                atk["pos"] = ev["end"]
                hero_at[ev["end"]] = atk
        elif res == "attacker_wins":
            if atk:
                atk["kills"].append(d_name)
                hero_at.pop(ev["start"], None)
                atk["pos"] = ev["end"]
                hero_at[ev["end"]] = atk
            if dfn:
                die(dfn, no, "被吃", False)
        elif res == "defender_wins":
            if atk:
                if d_name == "地雷":
                    die(atk, no, "触雷", False)
                else:
                    die(atk, no, "被吃", False)
                hero_at.pop(ev["start"], None)
            if dfn:
                dfn["kills"].append(a_name)
        elif res == "both_die":
            if atk:
                die(atk, no, "被炸弹炸死" if d_name == "炸弹" else "同尽",
                    d_name == "炸弹")
                hero_at.pop(ev["start"], None)
            if dfn:
                die(dfn, no, "被炸弹炸死" if a_name == "炸弹" else "同尽",
                    a_name == "炸弹")
        elif res == "flag_captured":
            if atk:
                hero_at.pop(ev["start"], None)
                atk["pos"] = ev["end"]
                hero_at[ev["end"]] = atk
    return heroes


def find_jqh_empty_bomb_victims(raw: bytes, mode: str = "none") -> list[dict]:
    """空炸筛选(联众 .jqh): 司令/军长被对手炸弹换掉且击杀记录满足条件。

    mode="none":     什么都没吃;
    mode="max_lian": 最大仅吃到连长(连/排/工, 含什么都没吃)。
    返回字段与 QQ 版 find_empty_bomb_victims 一致。
    """
    out = []
    for h in trace_jqh_heroes(raw):
        if not h["death_by_bomb"]:
            continue
        if mode == "none":
            if h["kills"]:
                continue
        elif any(name not in JQH_SMALL_KILL_PIECES for name in h["kills"]):
            continue
        out.append({
            "color": h["block"],
            "seat": h["seat"],
            "player": h["player"],
            "piece": h["name"],
            "kills": list(h["kills"]),
            "death_step": h["death_step"],
            "death_kind": h["death_kind"],
            "empty": not h["kills"],
        })
    return out


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


def jqh_global_pieces(raw: bytes) -> dict[tuple[int, int], tuple[str, str]]:
    """解析 JQH 文件全部棋子的全局 17×17 位置。

    返回 {(行,列): (颜色, 棋子)}; 使用 chess_board_diff_process 的位置识别
    + 四色累积 delta 解码(同 decode_jqh_all_boards 的核心)。
    """
    import sys
    _parent = os.path.join(os.path.dirname(__file__), "..")
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    from .chess_board_diff_process import load_groups, classify_groups, GRID_SIZE

    groups = load_groups(raw)
    is_piece = classify_groups(groups)
    seed_b1 = groups[0][1]

    piece_positions = []
    for i in range(1, len(is_piece)):
        if is_piece[i]:
            gi = i - 1
            row = gi // GRID_SIZE
            col = gi % GRID_SIZE
            piece_positions.append((i, row, col))

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

    prev_list = [seed_b1] + [groups[gi][1] for gi, _, _ in piece_positions[:-1]]
    gi_list = [gi for gi, _, _ in piece_positions]
    row_list = [r for _, r, _ in piece_positions]
    col_list = [c for _, _, c in piece_positions]

    results = [_decode_piece(p, g, r, c)
               for p, g, r, c in zip(prev_list, gi_list, row_list, col_list)]

    decoded: dict[tuple[int, int], tuple[str, str]] = {}
    for _, (row, col), (color, piece) in results:
        if piece == '空' or piece == '?':
            continue
        decoded[(row, col)] = (color, piece)
    return decoded


def decode_jqh_all_boards(raw: bytes) -> dict[str, list[list[str]]]:
    """用完整解码逻辑(棋子位置识别+四色累积delta)解析 JQH 文件, 返回四家 6x5 布阵。

    与 parse_jqh_color_board 不同, 本函数使用 chess_board_diff_process 的 17×17 棋盘
    位置识别, 正确处理绿/蓝/黑方在棋盘不同区域的数据。
    """
    decoded = jqh_global_pieces(raw)

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
