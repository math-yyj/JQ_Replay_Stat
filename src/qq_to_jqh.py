"""QQ .jgs 复盘 -> 联众 .JQH 二进制转换器。

只保留对局者名字与公共信息；允许由 QQ 侧推演补出战果/终局。
编码层为 lianzhong 各解码函数的逆:
  - 0x00-0x23  头部: GL 魔数 + ctime 风格日期串;
  - 0x24-0x63  简介明文(GBK, NUL 填充): "win.  sp <名>. time 00:00:00. rule SMS. step N";
  - 0x64-0xB3  四家用户名 [红,绿,蓝,黑] ×20B, XOR 累积链(首字节明文);
  - 0xB4-0xC7  子链, 明文 [0x04,0,0,0, 主视角,0,0,0, 首手,0...],
               0xB8 主视角色索引(红0/绿1/蓝2/黑3), 0xBC 首手方索引;
  - 0xC8-      行棋区: LE32 步数 + N×4B 四元组 + 哨兵(Pass=FF×4, GiveUp=FE×4),
               链式编码 raw[i]=((plain[i]+raw[i-1])&0xFF)^0x13, prev 从 raw[0xC7] 起;
  - 移动区后-0x106F 零明文链(呈周期2常量);
  - 0x1070-0x14F7 布阵区 290×4B = 种子组 + 289 格(17×17, 组 i ↔ 格 i-1):
               组恒为 [b2+e, b1, b2, b1], b2=b1^0x13, 黑子 e=-1, 其余 e=0;
               棋子 b1=(前棋子b1+code+K)&0xFF, 红/绿/蓝 K=0x00/0x64/0xC8,
               黑 K=0x2B 且 (base&3)==3 时 +8(base&7==3 且 bit3=1 再按 prev bit4 ±32);
               空格沿用前组 b1/b2 恒写 e=0 形态。
坐标: QQ 规范帧 --(上下翻转 (16-r,c))--> 联众网格 --(转置 (c,r))--> 存储四元组。
颜色对应(用户裁定): QQ 黄=联众红, 紫=绿, 绿=蓝, 蓝=黑。
"""

from __future__ import annotations

import os
import struct
import time

from .jgs_types import ItemType
from .qq_board import normalize_board, rotation_times, rotate_cw
from .qq_replay import PIECE_SHORT, QQReplay, parse_jgs

JQH_SIZE = 5368
MOVE_START = 0xC8
PAD_END = 0x1070          # 零明文链写到 0x106F(含), 布阵区自 0x1070 起
LAYOUT_START = 0x1070
LAYOUT_GROUPS = 290       # 组0=标记, 组1..289 = 17×17 格(组 i ↔ 格 i-1)

# QQ 颜色 -> 联众块色(用户裁定)
QQ2JQH_COLOR = {"黄": "红", "紫": "绿", "绿": "蓝", "蓝": "黑"}
# 联众块色 -> K 偏移 / 格式
JQH_K = {"红": 0x00, "绿": 0x64, "蓝": 0xC8, "黑": 0x2B}
JQH_COLOR_INDEX = {"红": 0, "绿": 1, "蓝": 2, "黑": 3}
# 联众用户名槽序 [红, 绿, 蓝, 黑]
JQH_NAME_ORDER = ("红", "绿", "蓝", "黑")

# 单字棋名 -> .jqs/.JQH 布阵编码(与 JQS_CODE_MAP 反向一致)
CHAR_CODE = {"军": 0x01, "师": 0x02, "旅": 0x03, "团": 0x04, "营": 0x05,
             "连": 0x06, "排": 0x07, "兵": 0x08, "旗": 0x09, "司": 0x0A,
             "雷": 0x0B, "弹": 0x0C}

# 联众网格中的无效格(组下标, 同 chess_board_diff_process.out_board_ranges)
_OUT_BOARD = (
    (1, 6), (12, 17), (18, 23), (29, 34), (35, 40), (46, 51),
    (52, 57), (63, 68), (69, 74), (80, 85), (86, 91), (97, 102),
    (110, 110), (112, 112), (126, 130),
    (144, 144), (146, 146), (160, 164),
    (178, 178), (180, 180),
    (188, 193), (199, 204), (205, 210), (216, 221),
    (222, 227), (233, 238), (239, 244), (250, 255),
    (256, 261), (267, 272), (273, 278), (284, 289),
)
_OUT_SET = {i for s, e in _OUT_BOARD for i in range(s, e + 1)}


def _encode_names(names80: bytes) -> bytes:
    """用户名链编码: raw[0]=plain[0]; raw[i]=((raw[i-1]+plain[i])&0xFF)^0x13。"""
    out = bytearray(80)
    out[0] = names80[0]
    for i in range(1, 80):
        out[i] = ((out[i - 1] + names80[i]) & 0xFF) ^ 0x13
    return bytes(out)


def _encode_chain(plain: bytes, prev: int) -> bytes:
    """通用链编码(行棋区/填充区): raw[i]=((plain[i]+raw[i-1])&0xFF)^0x13。"""
    out = bytearray(len(plain))
    for i, p in enumerate(plain):
        out[i] = ((prev + p) & 0xFF) ^ 0x13
        prev = out[i]
    return bytes(out)


def _fq(r: int, c: int) -> tuple[int, int]:
    """QQ 规范帧 -> 联众网格(上下翻转)。"""
    return 16 - r, c


def convert_qq_to_jqh(replay: QQReplay, template: bytes,
                      timestamp: float | None = None,
                      sentinel_shift: int = 0) -> bytes:
    """把 QQ 复盘转换为联众 .JQH 字节串(以 template 提供常量区字节)。"""
    if len(template) != JQH_SIZE:
        raise ValueError(f"template 需为 {JQH_SIZE} 字节, 实际 {len(template)}")
    times = rotation_times(replay.orient)
    board = normalize_board(replay.board, replay.orient)
    if times is None or board is None:
        raise ValueError("方位无法归一, 不支持转换")

    buf = bytearray(template)

    # ---- 0x08..: ctime 风格日期串 ----
    when = time.localtime(timestamp if timestamp is not None
                          else os.path.getmtime(replay.path))
    date_str = time.strftime("%a %b %d %H:%M:%S %Y", when) + "\n"
    date_bytes = date_str.encode("ascii")
    buf[0x08:0x08 + len(date_bytes)] = date_bytes

    # ---- 0x24..: 简介明文(胜负按主视角推导) ----
    analysis = replay.analyze()
    if analysis.winner:
        verdict = "win." if replay.my_color in analysis.winner else "lose."
    else:
        verdict = "draw."
    my_name = replay.players.get(replay.my_color or "", "") or "player"

    # ---- 0x64..0xB3: 四家用户名 [红,绿,蓝,黑] ----
    names80 = bytearray(80)
    for qq_color, jqh_color in QQ2JQH_COLOR.items():
        slot = JQH_NAME_ORDER.index(jqh_color)
        name = replay.players.get(qq_color, "") or "player"
        raw_name = name.encode("gbk", errors="replace")[:20]
        names80[slot * 20:slot * 20 + len(raw_name)] = raw_name
    buf[0x64:0xB4] = _encode_names(bytes(names80))

    # ---- 0xB4..0xC7: 子链, 明文 [04,0,0,0, 主视角,0,0,0, 首手,0...] ----
    # 原生格式(200 对实测): 0xB4=0x04 四家标记, 0xB8=主视角色索引,
    # 0xBC=首手方索引; 0xB4 写杂值会使官方复盘器错位解析子链,
    # 轮次锚点整体偏一家(GiveUp 归属错后一槽)。
    my_idx = JQH_COLOR_INDEX[QQ2JQH_COLOR.get(replay.my_color or "", "红")]
    first_idx = my_idx
    for kind, item in replay.seq:
        if kind == "event":
            if item is replay.end_event or item.code not in (2, 3):
                continue
        elif item.tag in ("反弹", "交换"):
            continue
        first_idx = JQH_COLOR_INDEX[QQ2JQH_COLOR.get(item.color or "", "红")]
        break
    sub_plain = [0] * 20
    sub_plain[0] = 0x04
    sub_plain[4] = my_idx
    sub_plain[8] = first_idx
    prev = buf[0xB3]
    for k, v in enumerate(sub_plain):
        buf[0xB4 + k] = ((prev + v) & 0xFF) ^ 0x13
        prev = buf[0xB4 + k]

    # ---- 行棋区: 序列 -> 四元组/哨兵 ----
    # 先据 QQ 推演建立死亡时间线: 军旗被扛即时出局; code=3 事件若为
    # "军旗被扛,战败" 的收尾标记(玩家已死)则不再发哨兵(死亡由行棋推出)。
    analysis = replay.analyze()
    dead: set[str] = set()
    for step in analysis.steps:
        if step.kind == "move" and step.resolved is not None \
                and step.resolved.type == "flag" and step.resolved.defender:
            dead.add(step.resolved.defender)

    entries: list[bytes] = []
    for kind, item in replay.seq:
        if kind == "event":
            # 退出(code=4)仅为标志位(5.1 死者退出无效果, 5.2 逃跑由配对的
            # 认负事件承载死亡), 终局标记(code=5)同理不入行棋区。
            if item is replay.end_event or item.code not in (2, 3):
                continue
            if item.code == 2 and item.b < 5:
                entries.append(bytes([0xFF]) * 4)     # 超时(未判负)=Pass
                continue
            if item.color and item.color in dead:
                continue                              # 扛旗后的战败收尾标记
            entries.append(bytes([0xFE]) * 4)         # 认负/超时判负=GiveUp(阵亡)
            if item.color:
                dead.add(item.color)
            continue
        if item.tag == "反弹" or item.tag == "交换":
            continue                                  # 非生效步, 目标格式无对应
        s = rotate_cw(item.start[0], item.start[1], times)
        e = rotate_cw(item.end[0], item.end[1], times)
        gr1, gc1 = _fq(*s)
        gr2, gc2 = _fq(*e)
        entries.append(bytes((gc1, gr1, gc2, gr2)))   # 存储为 (列,行,列,行)

    # 对照实验: GiveUp 哨兵整体位移(-1=前移一条, +1=后移一条),
    # 用于适配官方复盘器的轮次归属模型
    if sentinel_shift:
        gu = bytes([0xFE]) * 4
        idxs = [i for i, e in enumerate(entries) if e == gu]
        for i in (reversed(idxs) if sentinel_shift > 0 else idxs):
            j = i + sentinel_shift
            if 0 <= j < len(entries):
                entries.pop(i)
                entries.insert(j, gu)

    intro = (f"{verdict}  sp {my_name}. time 00:00:00. "
             f"rule SMS. step {len(entries)}")
    intro_bytes = intro.encode("gbk", errors="replace")[:0x3F]
    buf[0x24:0x64] = b"\x00" * 0x40
    buf[0x24:0x24 + len(intro_bytes)] = intro_bytes

    plain_moves = struct.pack("<I", len(entries)) + b"".join(entries)
    tail = b"\x00" * max(0, (PAD_END - (MOVE_START + len(plain_moves))))
    chain = _encode_chain(plain_moves + tail, buf[0xC7])
    buf[MOVE_START:MOVE_START + len(chain)] = chain

    # ---- 0x1070..: 布阵区(种子组 + 289 格) ----
    # 组结构恒为 [b2+e, b1, b2, b1], b2 = b1^0x13; 黑子 e=-1(首字节=b2-1), 其余 e=0。
    # 空格沿用前组 b1/b2 但恒写 e=0 形态(跟在黑子后即呈 byte0+1 复制)。
    marker = bytes.fromhex("e5 f6 e5 f6")
    buf[LAYOUT_START:LAYOUT_START + 4] = marker
    prev_group = marker
    prev_piece_b1 = marker[1]
    for g in range(17 * 17):
        idx = g + 1
        r, c = divmod(g, 17)
        qq_r, qq_c = 16 - r, c          # 网格 -> QQ 规范帧(同为上下翻转)
        placed = board.get((qq_r, qq_c))
        if idx in _OUT_SET or placed is None:
            b1, b2 = prev_group[1], prev_group[2]
            group = bytes((b2, b1, b2, b1))
        else:
            jqh_color = QQ2JQH_COLOR[placed.color]
            char = PIECE_SHORT[placed.piece]
            code = CHAR_CODE[char]
            b1 = (prev_piece_b1 + code + JQH_K[jqh_color]) & 0xFF
            if jqh_color == "黑":
                # 黑方链进位怪癖(base=prev+code+0x2B): (base&3)!=3 不动;
                # ==3 时 +8, 但 base&7==3 且 base bit3=1 时按 prev bit4 再 ±32。
                if (b1 & 3) == 3:
                    if (b1 & 7) == 7 or (b1 & 8) == 0:
                        b1 = (b1 + 8) & 0xFF
                    else:
                        b1 = (b1 + 8 + (32 if prev_piece_b1 & 0x10 else -32)) & 0xFF
            prev_piece_b1 = b1
            b2 = b1 ^ 0x13
            e = -1 if jqh_color == "黑" else 0
            group = bytes(((b2 + e) & 0xFF, b1, b2, b1))
        buf[LAYOUT_START + 4 * idx:LAYOUT_START + 4 * idx + 4] = group
        prev_group = group
    return bytes(buf)


def convert_jgs_file(jgs_path: str | os.PathLike,
                     out_path: str | os.PathLike | None = None,
                     template_path: str | os.PathLike | None = None,
                     sentinel_shift: int = 0) -> dict:
    """转换单个 .jgs 为 .JQH, 返回自校验摘要 dict。"""
    from .lianzhong import (decode_jqh_all_usernames, decode_jqh_main_color,
                            jqh_global_pieces, parse_jqh_moves_raw)

    jgs_path = os.fspath(jgs_path)
    if template_path is None:
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "test_sample",
            "jqh_pairs_200", "001_20260809193340.JQH")
    template = open(template_path, "rb").read()
    replay = parse_jgs(jgs_path)
    data = convert_qq_to_jqh(replay, template,
                             timestamp=os.path.getmtime(jgs_path),
                             sentinel_shift=sentinel_shift)
    if out_path is None:
        out_path = os.path.splitext(jgs_path)[0] + ".JQH"
    out_path = os.fspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(data)

    # 回读自校验
    times = rotation_times(replay.orient)
    board = normalize_board(replay.board, replay.orient)
    got_names = decode_jqh_all_usernames(data)
    want_names = [replay.players.get(qq, "") for qq, lz in
                  sorted(QQ2JQH_COLOR.items(), key=lambda kv: JQH_NAME_ORDER.index(kv[1]))]
    got_layout = jqh_global_pieces(data)
    want_layout = {}
    for (r, c), p in board.items():
        gr, gc = _fq(r, c)
        want_layout[(gr, gc)] = (QQ2JQH_COLOR[p.color], PIECE_SHORT[p.piece])
    _, moves = parse_jqh_moves_raw(data)
    check = {
        "out": out_path,
        "size_ok": len(data) == JQH_SIZE,
        "names_ok": got_names == want_names,
        "layout_ok": got_layout == want_layout,
        "pieces": len(got_layout),
        "moves": len(moves),
    }
    return check
