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
from .qq_replay import (PIECE_SHORT, Move, QQReplay, _auto_result,
                        _post_move_judgment, _remove_all, _tag_type, parse_jgs,
                        resolve_move)

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


def _fit_move_times(replay: QQReplay,
                    board: dict[tuple[int, int], "BoardPiece"],
                    times: int | None) -> int:
    """实证拟合走子帧相对规范帧的旋转次数。

    常规局走子帧=布阵帧(偏移 0); 个别老二人局(orient 旋转 180°)的走子
    记录在规范帧, 差半圈。以"空起点最少、tag 与子力裁决最一致"择优,
    平局维持 times。
    """
    best_key: tuple | None = None
    best_tm = times or 0
    for tm in range(4):
        b = {k: v for k, v in board.items()}
        empty = mism = 0
        for kind, item in replay.seq:
            if kind != "move":
                continue
            s = rotate_cw(item.start[0], item.start[1], tm)
            e = rotate_cw(item.end[0], item.end[1], tm)
            a = b.get(s)
            if a is None:
                empty += 1
                continue
            t = b.get(e)
            if t is not None and item.tag:
                if (_tag_type(item.tag) or "?") != _auto_result(a.piece, t.piece):
                    mism += 1
            resolve_move(Move(no=item.no, color=a.color, start=s, end=e,
                              result=item.result, tag=item.tag), b)
        key = (empty, mism, 0 if tm == (times or 0) else 1)
        if best_key is None or key < best_key:
            best_key, best_tm = key, tm
    return best_tm


def convert_qq_to_jqh(replay: QQReplay, template: bytes,
                      timestamp: float | None = None) -> bytes:
    """把 QQ 复盘转换为联众 .JQH 字节串(以 template 提供常量区字节)。"""
    if len(template) != JQH_SIZE:
        raise ValueError(f"template 需为 {JQH_SIZE} 字节, 实际 {len(template)}")
    times = rotation_times(replay.orient)
    board = normalize_board(replay.board, replay.orient)
    if times is None or board is None:
        raise ValueError("方位无法归一, 不支持转换")
    move_times = _fit_move_times(replay, board, times)

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
        else:
            piece = board.get(rotate_cw(item.start[0], item.start[1], move_times))
            if piece is not None:
                first_idx = JQH_COLOR_INDEX[QQ2JQH_COLOR[piece.color]]
                break
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

    # ---- 行棋区: QQ 流 -> 联众条目(轮次槽位对齐) ----
    # 联众条目逐槽占轮(走子自证行棋方, 哨兵靠轮转归属), 而 QQ 的
    # "行棋后即时判亡"在联众只能以死者自身槽位的 GiveUp 表达。翻译规则
    # (用户裁定 2026-11-06 三类枚举): 死亡登记保持 QQ 原位(其后任何真实
    # 行棋之前), GiveUp 落死者自身轮次槽, 途中/尾部空槽以 Pass 填充;
    # 扛旗即时清子不发哨兵(死者槽位由轮转跳过); 超时5次判负=自己行棋
    # 时刻认负, 零填充; 退出(code=4)纯标志位不入行棋区。
    PASS = bytes([0xFF]) * 4
    GIVEUP = bytes([0xFE]) * 4
    # 二人局等非满员局: 轮转只在参战家之间循环; 行棋方一律以起点棋子
    # 真色为准(部分老复盘的 move.color 字段与实际参战色不一致)
    participants = {p.color for p in board.values()}
    order = [c for c in (replay.turn_order or ["黄", "紫", "绿", "蓝"])
             if c in participants] or ["黄", "紫", "绿", "蓝"]
    alive = {c: c in participants for c in ["黄", "紫", "绿", "蓝"]}
    defeated_at: dict[str, int] = {}
    death_order: dict[str, int] = {}
    walk_board = dict(board)
    entries: list[bytes] = []
    slot_ptr = 0

    def next_owner() -> str | None:
        nonlocal slot_ptr
        for _ in range(len(order) + 1):
            color = order[slot_ptr % len(order)]
            slot_ptr += 1
            if alive.get(color):
                return color
        return None

    def emit_at(target: str | None, entry: bytes) -> None:
        """entry 落到 target 自身轮次槽, 途中存活家逐槽填 Pass。"""
        while target and alive.get(target):
            owner = next_owner()
            if owner is None:
                return
            if owner == target:
                entries.append(entry)
                return
            entries.append(PASS)

    def die(color: str, step_no: int) -> None:
        alive[color] = False
        defeated_at[color] = step_no
        death_order[color] = len(death_order)
        _remove_all(walk_board, color)

    for kind, item in replay.seq:
        if kind == "event":
            color = item.color
            if item is replay.end_event or item.code not in (2, 3):
                continue
            if item.code == 2 and item.b < 5:
                emit_at(color, PASS)          # 超时占轮
                continue
            if not color or not alive.get(color):
                continue                      # 扛旗收尾标记/重复记录
            emit_at(color, GIVEUP)            # 认负/超时5次判负: 自身槽位出局
            die(color, item.no)
            continue
        if item.tag in ("反弹", "交换"):
            continue                          # 非生效步, 无对应条目
        s = rotate_cw(item.start[0], item.start[1], move_times)
        e = rotate_cw(item.end[0], item.end[1], move_times)
        mover = walk_board.get(s)
        mover_color = mover.color if mover else item.color
        gr1, gc1 = _fq(*s)
        gr2, gc2 = _fq(*e)
        emit_at(mover_color, bytes((gc1, gr1, gc2, gr2)))
        move = Move(no=item.no, color=mover_color, start=s, end=e,
                    result=item.result, tag=item.tag)
        resolved = resolve_move(move, walk_board)
        if resolved.flag_down and resolved.defender:
            die(resolved.defender, item.no)   # 扛旗即时阵亡, 不发哨兵
        for died in _post_move_judgment(walk_board, mover_color, replay,
                                        alive, defeated_at, item.no,
                                        death_order, apply=False):
            emit_at(died, GIVEUP)             # 无棋可走: 死者自身槽位出局
            die(died, item.no)

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
                     template_path: str | os.PathLike | None = None) -> dict:
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
                             timestamp=os.path.getmtime(jgs_path))
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
