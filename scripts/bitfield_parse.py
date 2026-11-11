#!/usr/bin/env python3
"""chip-manual-to-kb · 寄存器位域表解析器

=== 为什么需要专用解析器（2026-10-29 实测依据）===
AT32F423 技术手册里，**寄存器位域表是主体内容**：
  · 「位NN」开头的位域行 **2,279 条**
  · Markdown 表格行仅 236 条
  · 比例 9.7 : 1
通用表格算法（table_rebuild）对这类表无效：它们无栅格几何、无规整横线，
被归入 TABLE_SUSPECT 后只输出线性文字，字段对应关系靠语序推断，有错位风险。

=== 位域表的排版真相（p181 USART_CTRL1 逐词实测）===
固定 5 列，x 坐标稳定（同一手册内几乎不变）:
    域(x≈88) | 简称(x≈137) | 复位值(x≈229) | 类型(x≈293) | 功能(x≈328)

每个位域块形如:
    位31：29  | 保留位 | 0x00000 | resd | 硬件强制为0。
    位28      | DBN1   | 0x0     | rw   | 数据位个数高位…（多行）

**关键难点**: 功能描述在 y 上与「位NN」行**不对齐**——
    `位28` 在 y=356，但它的功能文字从 y=314.9 开始（在上一行上方）。
    原因是 Word 里功能单元格跨多行，垂直居中对齐导致。
⇒ 不能用 y 对齐，必须**按 x 分区**（x≈88~130 是「位」列，x≈328+ 是「功能」列），
   然后把「功能」列的文字按 y 归属到**最近的「位」行**。

=== 输出格式 ===
标准 Markdown 表:
    | 位 | 简称 | 复位值 | 类型 | 功能 |
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# ---- 五列的 x 坐标锚点（pt）。AT32 手册实测稳定，允许 ±6pt 抖动
COL_X_DOMAIN = 88.0     # 域（位号）
COL_X_NAME = 137.0      # 简称
COL_X_RESET = 229.0     # 复位值
COL_X_TYPE = 293.0      # 类型
COL_X_DESC = 328.0      # 功能

COL_TOL = 22.0          # 列归属容差（相邻列间距约 50~90pt，22 足够安全）

# ---- 位号行的正则：位31：9 / 位8：0 / 位28 / 位0
RE_BIT_ROW = re.compile(r"^位\s*(\d+)(?:\s*[：:]\s*(\d+))?")

# ---- 复位值形态：0x 十六进制 / 纯数字 / X 占位
RE_RESET_VAL = re.compile(r"^(?:0[xX][0-9A-Fa-f]+|0x[X]+|[Xx]+|\d+)$")

# ---- 类型形态：rw / ro / wo / resd / rw1c / w1c 等
RE_TYPE_VAL = re.compile(r"^(?:rw|ro|wo|resd|res|rw1c|w1c|rc_w0|rc_w1|r0w1)$", re.I)

# ---- 功能描述的分段阈值（pt）
# 实测 p181: 同一功能段内相邻行 y 间隔约 11~12pt；
# 跨功能段的间隔明显更大（如「硬件强制为0」→「数据位个数高位」间隔约 12pt，
# 但「enable）」→「0：关闭；」间隔约 11.6pt）。
# 用 16pt 作为阈值：同一段内不切，跨段才切。
DESC_GAP = 16.0


@dataclass
class BitField:
    """一个位域条目"""
    bits: str = ""          # 位号，如 "31：9" 或 "28"
    name: str = ""          # 简称
    reset: str = ""         # 复位值
    type_: str = ""         # 类型
    desc: List[str] = field(default_factory=list)   # 功能描述（多行）

    def to_row(self) -> List[str]:
        return [self.bits, self.name, self.reset, self.type_,
                " ".join(self.desc).strip()]


@dataclass
class BitFieldTable:
    page: int
    y0: float
    y1: float
    fields: List[BitField] = field(default_factory=list)
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.fields) and self.confidence >= CONF_ACCEPT


CONF_ACCEPT = 0.75


# ---------------------------------------------------------------- 列归属


def _col_of(x0: float) -> int:
    """按 x 坐标判定属于哪一列：0=域 1=简称 2=复位值 3=类型 4=功能"""
    anchors = [COL_X_DOMAIN, COL_X_NAME, COL_X_RESET, COL_X_TYPE, COL_X_DESC]
    # 找最近的锚点，但要求距离 <= COL_TOL
    best = min(range(5), key=lambda i: abs(x0 - anchors[i]))
    if abs(x0 - anchors[best]) <= COL_TOL:
        return best
    # 超出容差：按区间分（x < 112 归域列；x >= 310 归功能列；中间按边界）
    if x0 < 112:
        return 0
    if x0 < 180:
        return 1
    if x0 < 262:
        return 2
    if x0 < 310:
        return 3
    return 4


# ---------------------------------------------------------------- 主解析


def parse_bitfield_tables(plumber_page, page_no: int,
                          y_from: float = 58.0, y_to: float = 782.0
                          ) -> List[BitFieldTable]:
    """从单页解析所有寄存器位域表。

    返回 BitFieldTable 列表（每张表一个），内含结构化位域条目。
    """
    words = []
    for w in plumber_page.extract_words(use_text_flow=False, keep_blank_chars=False):
        cy = (w["top"] + w["bottom"]) / 2.0
        if not (y_from < cy < y_to):
            continue
        txt = (w["text"] or "").strip()
        if not txt:
            continue
        words.append({"text": txt, "x0": w["x0"], "x1": w["x1"],
                      "top": w["top"], "bottom": w["bottom"], "cy": cy})

    if not words:
        return []

    # 1) 找表头行「域 / 简称 / 复位值 / 类型 / 功能」
    header_ys = _find_table_headers(words)

    # 1b) 无表头但有位行 -> 视为跨页续页，整页作一个表区域
    # == 跨页表问题（2026-10-29 实测）==
    # p47 的 CRM_CTRL 位域表在「位1」结束，「位0」延续到 p48；
    # 而 p48 顶部**没有表头行**（表头只在 p47 出现一次）。
    # 早期实现要求必须找到表头才解析，导致续页的「位0」漏成线性文字
    # （实测 CRM 章 68 条未结构化行里，大部分是这类跨页续页）。
    if not header_ys:
        has_bit_rows = any(_col_of(w["x0"]) == 0 and RE_BIT_ROW.match(w["text"])
                           for w in words)
        if has_bit_rows:
            header_ys = [y_from]

    if not header_ys:
        return []

    # 2) 按表头切分表格区域（表头到下一个表头或页末）
    tables: List[BitFieldTable] = []
    for i, hdr_y in enumerate(header_ys):
        y_start = (hdr_y + 6.0) if hdr_y > y_from else y_from
        y_end = header_ys[i + 1] - 4.0 if i + 1 < len(header_ys) else y_to
        region = [w for w in words if y_start <= w["cy"] <= y_end]
        if not region:
            continue
        tb = _parse_one_table(region, page_no, y_start, y_end)
        if tb and tb.fields:
            tables.append(tb)
    return tables


def _find_table_headers(words: Sequence[dict]) -> List[float]:
    """找表头行 y 坐标：同一 y 上出现「域」「简称」「复位值」「类型」「功能」

    实测 p181: 表头在 y=96.3 / 210.9 / 288.5，五个词 x 分别是 88/137/229/293/328。
    """
    # 按 y 聚类
    rows: dict = {}
    for w in words:
        rows.setdefault(round(w["cy"] / 2), []).append(w)

    header_ys = []
    for key in sorted(rows):
        ws = rows[key]
        texts = {w["text"] for w in ws}
        # 要求至少命中 4 个表头词（容忍个别缺失）
        hits = sum(1 for h in ("域", "简称", "复位值", "类型", "功能") if h in texts)
        if hits >= 4:
            cy = sum(w["cy"] for w in ws) / len(ws)
            header_ys.append(cy)
    return header_ys


def _parse_one_table(region: Sequence[dict], page_no: int,
                     y0: float, y1: float) -> Optional[BitFieldTable]:
    """解析单个位域表区域

    == 功能描述的归属规则（2026-10-29 逐词实测后确定）==
    不能简单用「本位行 y 到下一位行 y」分块，因为 Word 的功能单元格
    **垂直居中**，其文字在 y 上跨越位行：
      · 位31:29 在 y=302.8，其功能「硬件强制为0。」也在 y=302.8 ✓ 对齐
      · 位28   在 y=355.8，但其功能「数据位个数高位…」从 y=314.9 起
        （在上一行位31:29 的下方、位28 的上方）✗ 不对齐
      · 位27   在 y=426.2，功能「接收器超时检测使能位…」从 y=408.8 起 ✗ 不对齐

    正确做法: 按**功能列的段落边界**切分——功能列文字按 y 排序后，
    相邻两行的 y 间隔若明显变大（> DESC_GAP），说明进入了新位域块。
    然后按「块序号」与「位行序号」一一对应。

    这样每块的功能描述都完整且不串行。
    """
    # 1) 收集「位号」行
    bit_rows: List[Tuple[float, str]] = []
    for w in region:
        if _col_of(w["x0"]) != 0:
            continue
        if RE_BIT_ROW.match(w["text"]):
            bit_rows.append((w["cy"], w["text"].replace("：", ":")))
    if not bit_rows:
        return None
    bit_rows.sort(key=lambda t: t[0])

    # 2) 收集功能列文字，按 y 排序
    desc_words = [w for w in region if _col_of(w["x0"]) == 4]
    desc_words.sort(key=lambda w: (round(w["cy"], 1), w["x0"]))

    # == 功能描述的分段：用**文字特征**而非 y 间隔（2026-10-29 实测结论）==
    # 实测 p181 功能列所有相邻行的 y 间隔都是 11.3~12.4pt，**几乎没有区分度**
    # （只有首行「硬件强制为0。」是 14.2pt）。按间隔切段必然失败（实测把所有功能
    # 并成一段，全塞进「位25:21」）。
    #
    # 真正的段落特征: 每个功能条目都以「中文名（English name）」或「中文位（...）」
    # 形态开头，且**以「）」或「）」结尾**。用「行文本是否包含中文括号左括号」
    # 判断是否为新条目起点。
    desc_lines = _group_desc_lines(desc_words)
    desc_groups = _split_desc_by_titles(desc_lines)

    # 3) 位行与功能段配对（**顺序单调贪心**，2026-10-29 四次校准）
    # == 为什么必须顺序单调 ==
    # 实测 p47: 位25 @237.8 的归属区间 [202.8, 293.1] 同时包含
    #   段1「PLL时钟稳定…」first_y=220.3 和 段2「PLL使能…」first_y=268.9。
    # 若「每段独立找区间」，两段都会匹配到位25（区间重叠）→ 位24 功能为空。
    #
    # 正确做法: 位行与功能段都自上而下有序，故用**单调贪心**：
    #   段 i 只允许匹配到 >= 上一段已匹配位行索引 的位行。
    # 这样段1→位25，段2→位24（因为位24 >= 位25 的下一索引）。
    pairs: List[Tuple[int, List[dict]]] = []
    if desc_groups and bit_rows:
        n = len(bit_rows)
        last_idx = 0
        for grp in desc_groups:
            first_y = min(w["cy"] for w in grp)
            idx = None
            # 从 last_idx 起找第一个 y 落在该位行归属区间的
            for i in range(last_idx, n):
                lo = bit_rows[i][0] - 35.0
                hi = (bit_rows[i + 1][0] - 5.0) if i + 1 < n else (bit_rows[i][0] + 60.0)
                if lo <= first_y <= hi:
                    idx = i
                    break
            if idx is None:
                # 兜底：从 last_idx 起取 y 最近的
                cand = list(range(last_idx, n)) or [n - 1]
                idx = min(cand, key=lambda i: abs(bit_rows[i][0] - first_y))
            # 关键：下一段必须从**下一位行**开始找（+1），否则两段会抢同一位行
            last_idx = idx + 1
            pairs.append((idx, grp))

    # 4) 构造位域条目：每位行一个，功能取配对的段
    fields: List[BitField] = []
    for i, (cy, bits_txt) in enumerate(bit_rows):
        bf = BitField(bits=bits_txt)
        # 该位行附近的「简称/复位值/类型」（y 容差 ±8pt）
        for w in region:
            if abs(w["cy"] - cy) > 8.0:
                continue
            col = _col_of(w["x0"])
            txt = w["text"]
            if col == 1 and not bf.name:
                bf.name = txt
            elif col == 2 and not bf.reset and txt.startswith("0x"):
                bf.reset = txt
            elif col == 2 and not bf.reset and RE_RESET_VAL.match(txt):
                bf.reset = txt
            elif col == 3 and not bf.type_ and RE_TYPE_VAL.match(txt):
                bf.type_ = txt
        # 功能描述：取配到本位的段
        for idx, grp in pairs:
            if idx == i:
                grp_sorted = sorted(grp, key=lambda w: (round(w["cy"], 1), w["x0"]))
                bf.desc = [w["text"] for w in grp_sorted]
                break
        fields.append(bf)

    tb = BitFieldTable(page=page_no, y0=y0, y1=y1, fields=fields)
    tb.confidence = _score(fields, tb.reasons)
    return tb


def _group_desc_lines(words: Sequence[dict]) -> List[List[dict]]:
    """把功能列的词按「文字行」聚成行（同一 y 的词归一行）"""
    if not words:
        return []
    lines: List[List[dict]] = [[words[0]]]
    prev_y = words[0]["cy"]
    for w in words[1:]:
        if abs(w["cy"] - prev_y) <= 1.5:
            lines[-1].append(w)
        else:
            lines.append([w])
        prev_y = w["cy"]
    # 行内按 x 排序
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


# ---- 功能条目起点的文字特征
# 实测 p181: 条目形如「接收器超时检测使能位（receiver time out detection
# interrupt enable）」「发送器开始延迟时间（transmit start delay time）」
# 特征: 该行含中文左括号「（」且括号前是中文描述 → 新条目起点
RE_DESC_TITLE = re.compile(r"[（(]")


def _split_desc_by_titles(lines: Sequence[List[dict]]) -> List[List[dict]]:
    """按「条目标题特征」把功能行切成段。

    判据（实测 p181 校准）:
      · 含「（」或「(」的行 → 可能是新条目起点
      · 且该行文字在「（」之前有 >=2 个中文字符（排除「（STIS=000）」这类纯参数行）
      · 否则视为上一行的续接（如「0：关闭；」「enable）」）
    """
    if not lines:
        return []

    def is_title(ln: List[dict]) -> bool:
        txt = "".join(w["text"] for w in ln)
        m = RE_DESC_TITLE.search(txt)
        if not m:
            return False
        before = txt[:m.start()]
        # 括号前至少 2 个中文字符才算标题
        cn = sum(1 for ch in before if "\u4e00" <= ch <= "\u9fff")
        return cn >= 2

    groups: List[List[dict]] = []
    cur: List[dict] = []
    for ln in lines:
        if is_title(ln):
            if cur:
                groups.append(cur)
            cur = list(ln)
        else:
            if cur:
                cur.extend(ln)
            else:
                cur = list(ln)
    if cur:
        groups.append(cur)
    return groups


def _split_by_gaps(words: Sequence[dict], gap: float) -> List[List[dict]]:
    """按 y 间隔切段：相邻行 y 差 > gap 即另起一段"""
    if not words:
        return []
    groups: List[List[dict]] = [[words[0]]]
    prev_y = words[0]["cy"]
    for w in words[1:]:
        if abs(w["cy"] - prev_y) > gap:
            groups.append([w])
        else:
            groups[-1].append(w)
        prev_y = w["cy"]
    return groups


def _score(fields: Sequence[BitField], reasons: List[str]) -> float:
    """置信度：位号齐备率、简称齐备率、类型齐备率"""
    if not fields:
        return 0.0
    n = float(len(fields))
    has_name = sum(1 for f in fields if f.name) / n
    has_reset = sum(1 for f in fields if f.reset) / n
    has_type = sum(1 for f in fields if f.type_) / n
    has_desc = sum(1 for f in fields if f.desc) / n

    # 位号必须 100%（否则解析已错位）
    score = 0.30 * has_name + 0.20 * has_reset + 0.20 * has_type + 0.30 * has_desc
    if has_type < 0.5:
        reasons.append("type_sparse")
    if has_name < 0.5:
        reasons.append("name_sparse")
    if has_desc < 0.3:
        reasons.append("desc_sparse")
    return round(max(0.0, min(1.0, score)), 2)


# ---------------------------------------------------------------- Markdown


def to_markdown(tb: BitFieldTable, caption: str = "") -> str:
    """输出标准 Markdown 位域表"""
    if not tb.fields:
        return ""
    out = []
    if caption:
        out.append("**%s**\n" % caption.strip())
    out.append("| 位 | 简称 | 复位值 | 类型 | 功能 |")
    out.append("| --- | --- | --- | --- | --- |")
    for f in tb.fields:
        row = f.to_row()
        out.append("| " + " | ".join(_esc(c) for c in row) + " |")
    if tb.confidence < CONF_ACCEPT:
        out.insert(0, "<!-- BITFIELD_LOWCONF: page=%d confidence=%.2f fields=%d reasons=%s -->\n"
                   % (tb.page, tb.confidence, len(tb.fields), ",".join(tb.reasons)))
    return "\n".join(out)


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>").strip()
