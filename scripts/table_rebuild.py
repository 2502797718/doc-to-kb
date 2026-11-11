#!/usr/bin/env python3
"""chip-manual-to-kb · 表格重建（v6 · 保守策略）

=== 为什么必须自行重建（实测依据）===
OV 的 PDFParser 用 pdfplumber 直转 PDF→Markdown。在 AT32F423 技术手册（V2.04，
495 页）上实测:
  · pdfplumber `extract_tables()` 对 p95/96/173/50 返回 **0 张表**
  · pymupdf `find_tables()` 同样 **0 张**
文字层被平铺后，MUX 复用表、寄存器位域表全部错列 → 直接入库等于污染知识库。

=== v6 核心决策：只重建有栅格几何的表（保守）===
实测全书 495 页中仅 **31 页**带栅格网格碎片（含 MUX 表 p89~99）。
只有这些页的**列边界**能被精确还原（p95: 1152 张细高碎片 → 8 条内部竖线 → 9 列）。

其余 464 页没有栅格几何，列边界只能靠表头文字 x 聚类猜。v3/v4 的教训是：
  猜出来的列会在「功能列垂直偏移」「单元格折行」上系统性错位，而且置信度会虚高
  （v4 曾给出 conf=1.00 但内容错乱的结果）。
  ⇒ v6 对这些页**不做表格重建**，只标记「本页疑有表格，原始文字为线性排列，
     请对照原册 p.NNN 核对」，由 agent 读截图决定怎么处理。
  ⇒ 宁可少给一张表，也不给错表。

=== 与 AI 的分工（用户明确要求）===
程序：定位表格、切网格、算置信度、渲染 300dpi 截图。
AI  ：读截图核对、修正错位、重写低置信度表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

# ---- 正文区（剔除页眉页脚）
BODY_TOP = 58.0
BODY_BOTTOM = 782.0

# ---- 栅格碎片判定（pt）
FRAG_THIN = 2.0
FRAG_MIN_LEN = 2.0
CLUSTER_TOL = 3.0

# ---- 列锚点聚类容差（pt）：表头/词起始位置聚类用
COL_TOL = 10.0

# ---- 矢量横线
RULE_MIN_LEN = 20.0

# ---- 行带
MIN_ROW_GAP = 8.0
MAX_ROW_GAP = 100.0

# ---- 表头向上搜索范围（pt）：区域顶部被切掉表头时向上找真表头
HEADER_SEARCH_UP = 60.0

# ---- 表头词最大长度：超过此长度视为单元格内容而非列名（不参与列锚点聚类）
# 实测 p173 表头含「置于波特率寄存器中的值」，若不限制会拆成多个伪列（8列→11列）
HEADER_WORD_MAX_LEN = 8

# ---- 表体最低规模
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2

# ---- grid 路径最低列数：至少 3 列（= 2 条内部边界）才算真有网格。
# 实测依据: 门槛降到 2 列后，p178 的框图/正文（页面有 1 条无关栅格碎片）
# 被误判为表格；要求 >=3 列可过滤掉这类假阳性。
MIN_GRID_COLS = 3

# ---- 置信度阈值（只用于 grid 路径）
CONF_ACCEPT = 0.85

# ---- 「疑有表格」判据（严格：宁可漏标，不制造噪音）
# 实测校准依据: AT32F423 RM 上松判据产出 555 条标记，其中 101 条跨度近整页，
# 绝大多数是正文段落而非表格。收紧后要求「短跨度 + 每行词数少」。
SUSPECT_MIN_RULES = 3       # 区域内至少几条矢量横线
SUSPECT_MIN_COLS = 4        # 文字 x 起始位置至少聚成几簇
SUSPECT_MIN_ROWS = 3        # 至少几行文字
SUSPECT_MAX_ROWS = 40       # 超过这么多行多半是整页正文
SUSPECT_MAX_SPAN = 300.0    # 单表纵向跨度上限（pt）；正文段落常跨 400~700pt
SUSPECT_MIN_WORDS = 12      # 至少这么多词才可能是表
SUSPECT_MAX_WORDS_PER_ROW = 5.0  # 每行平均词数上限；正文段落每行 10+ 词


@dataclass
class TableBlock:
    page: int
    y0: float
    y1: float
    x0: float = 60.0
    x1: float = 560.0
    rows: List[List[str]] = field(default_factory=list)
    col_bounds: List[float] = field(default_factory=list)
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    raw_text: str = ""
    accepted: bool = False
    mode: str = ""          # "grid" | "suspect"
    caption: str = ""
    # suspect 路径用：区域内逐词坐标，供 _suspect_kv 重建「列名：值」
    suspect_words: List[dict] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max(0, len(self.col_bounds) - 1)


# ---------------------------------------------------------------- 几何


def _cluster(vals: Sequence[float], tol: float = CLUSTER_TOL) -> List[float]:
    out: List[float] = []
    for v in sorted(vals):
        if out and abs(v - out[-1]) <= tol:
            continue
        out.append(v)
    return out


def grid_vlines(page) -> List[float]:
    """从栅格碎片还原**内部**竖线 x 坐标（页级汇总，仅用于快速判定该页有无网格）

    实测 p95: 1410 张图里 1152 张是 0.5×2.4pt 细高碎片，x 聚类后 8 条。
    这 8 条是列之间的内部边界（表格无外框线），故 9 列 = 8 内部线 + 左右外边界。
    """
    vx = []
    for im in page.get_image_info():
        x0, y0, x1, y1 = im["bbox"]
        if (x1 - x0) < FRAG_THIN and (y1 - y0) >= FRAG_MIN_LEN:
            vx.append((x0 + x1) / 2.0)
    return _cluster(vx)


def grid_vlines_in(page, y0: float, y1: float) -> List[float]:
    """**区域内**的竖线 x 坐标（按碎片 y 区间与目标区间重叠判定）。

    为什么必须按区域取（2026-10-29 实测教训）:
    p178 的 10 个竖线碎片全部落在 y=452~476，只是表 12-5 里 RDBF/ROERR 那两行的
    **合并单元格虚线**，纵向跨度仅 24pt。若按页级复用，这条线会被错误套用到同页
    的框图与正文区域，把非表格判成表格（实测产出 3 个假表格）。
    ⇒ 竖线必须「真的落在该区域纵向范围内」才算数。
    """
    vx = []
    for im in page.get_image_info():
        x0, fy0, x1, fy1 = im["bbox"]
        if (x1 - x0) >= FRAG_THIN or (fy1 - fy0) < FRAG_MIN_LEN:
            continue
        # 碎片纵向与目标区间有重叠（留 2pt 容差）
        if fy1 < y0 - 2.0 or fy0 > y1 + 2.0:
            continue
        vx.append((x0 + x1) / 2.0)
    return _cluster(vx)


def grid_col_bounds(vx: Sequence[float], words: Sequence[dict],
                    y0: float, y1: float) -> List[float]:
    """内部竖线 + 文字横向覆盖 -> 完整列边界（含左右外边界）

    x 应传入**区域内**的竖线（grid_vlines_in），不是页级汇总。

    == 设计决策（2026-10-29 多次实测后的最终版）==
    只认竖线，**不用表头文字锚点补列**。

    为什么放弃锚点方案（两次实测反例，均已复现）:
      1) p173 波特率误差表只有 2 条竖线（实际 8 列）-> 用锚点补列得到 9~11 列，
         因为表头含「置于波特率寄存器中的值」这类长文本会被拆成多个伪列；
      2) p95/p89 的 MUX 表本来 9 列正确，锚点合并后变 10 列（多出空列）。
    结论: 文字锚点与矢量/栅格竖线混用不可靠，宁可列数不足（交给 AI 复核）
          也不产出列数错误的表。

    代价（诚实记录）: p173 这类「竖线只覆盖部分列」的表会列数不足，
    但它会以 TABLE_NEEDS_AGENT 形式交给 AI 读截图重写，不会污染知识库。
    """
    if not vx:
        return []
    ws = [w for w in words if y0 <= w["cy"] <= y1]
    if not ws:
        return []
    left = min(w["x0"] for w in ws) - 6.0
    right = max(w["x1"] for w in ws) + 6.0
    inner = [b for b in vx if left < b < right]
    if len(inner) < 2:
        # 内部边界不足 2 条 -> 定不出 >=3 列，判为「无网格」（走 suspect 路径）
        return []
    return [left] + inner + [right]


def h_rules(page) -> List[float]:
    ys = []
    for d in page.get_drawings():
        for item in d["items"]:
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.6 and abs(p2.x - p1.x) > RULE_MIN_LEN:
                    ys.append((p1.y + p2.y) / 2.0)
            elif kind == "re":
                r = item[1]
                if r.height < 1.2 and r.width > RULE_MIN_LEN:
                    ys.append((r.y0 + r.y1) / 2.0)
    return _cluster(ys)


def page_words(plumber_page) -> List[dict]:
    out = []
    for w in plumber_page.extract_words(use_text_flow=False, keep_blank_chars=False):
        cy = (w["top"] + w["bottom"]) / 2.0
        if not (BODY_TOP < cy < BODY_BOTTOM):
            continue
        txt = (w["text"] or "").strip()
        if not txt:
            continue
        out.append({"text": txt, "x0": w["x0"], "x1": w["x1"],
                    "top": w["top"], "bottom": w["bottom"], "cy": cy})
    return out


# ---------------------------------------------------------------- 行带


def row_bands(ys: Sequence[float]) -> List[Tuple[float, float]]:
    return [(a, b) for a, b in zip(ys, ys[1:]) if MIN_ROW_GAP <= (b - a) <= MAX_ROW_GAP]


def band_groups(bands: Sequence[Tuple[float, float]]) -> List[List[Tuple[float, float]]]:
    if not bands:
        return []
    groups = [[bands[0]]]
    for a, b in bands[1:]:
        if abs(a - groups[-1][-1][1]) <= 1.5:
            groups[-1].append((a, b))
        else:
            groups.append([(a, b)])
    return [g for g in groups if len(g) >= MIN_TABLE_ROWS]


def fix_oversized_first_band(words: Sequence[dict],
                             groups: Sequence[List[Tuple[float, float]]]
                             ) -> List[List[Tuple[float, float]]]:
    """修正「表格缺外框线」导致表头/标题并入首个数据行（组内重切，不拆组）。

    实测 p95: 首块 [57.5~131.9] 高 74pt 同时含「表标题/表头/PD0」，后续数据行高约 23pt。
    做法: 组内首块高度 > 中位行高 1.8 倍时，从底部向上按中位行高重切；
    重切后剥掉空行与表标题行，剥完后第一行带即表头。
    """
    out: List[List[Tuple[float, float]]] = []
    for g in groups:
        if len(g) < MIN_TABLE_ROWS:
            out.append(list(g))
            continue
        heights = sorted(b - a for a, b in g)
        med = heights[len(heights) // 2] if heights else 0.0
        if med <= 0:
            out.append(list(g))
            continue
        if (g[0][1] - g[0][0]) <= med * 1.8:
            out.append(list(g))
            continue

        bands: List[Tuple[float, float]] = []
        bottom, top_limit = g[0][1], g[0][0]
        while bottom > top_limit + 1e-6:
            top = max(top_limit, bottom - med)
            bands.append((top, bottom))
            bottom = top
        bands.reverse()
        bands = strip_leading_non_table_bands(words, bands)
        out.append(bands + list(g[1:]))
    return out


def strip_leading_non_table_bands(words: Sequence[dict],
                                  bands: List[Tuple[float, float]]
                                  ) -> List[Tuple[float, float]]:
    """剥掉表格前的空行与表标题行，让表头成为第一行"""
    bands = list(bands)
    while len(bands) >= 2:
        ws = [w for w in words if bands[0][0] <= w["cy"] <= bands[0][1]]
        if not ws:
            bands = bands[1:]
            continue
        if _looks_like_caption(ws):
            bands = bands[1:]
            continue
        xs = sorted(w["x0"] for w in ws)
        if len(ws) <= 3 and len(xs) >= 2:
            span = max(w["x1"] for w in ws) - min(w["x0"] for w in ws)
            gaps = [b - a for a, b in zip(xs, xs[1:])]
            if span > 300 and (max(gaps) > 40 if gaps else True):
                bands = bands[1:]
                continue
        break
    return bands


def _looks_like_caption(ws: Sequence[dict]) -> bool:
    """表标题特征：含「表」+ 数字编号，文字量不大"""
    txt = " ".join(w["text"] for w in ws)
    return ("表" in txt) and any(ch.isdigit() for ch in txt) and len(txt) < 120


# ---------------------------------------------------------------- 单元格


def col_index(cx: float, bounds: Sequence[float]) -> int:
    for i in range(len(bounds) - 1):
        if cx < bounds[i + 1]:
            return i
    return len(bounds) - 2


def _join_hint(prev: str, nxt: str) -> bool:
    """两段文字是否应无空格拼接（折行断点续接）。

    == 依据: p91 MUX 表 128 格逐格 AI 核验（2026-10-29）==

    必须拼接（否则标识符被空格劈开）:
      · `SPI2_CS/I2S`    + `2_WS`      -> `SPI2_CS/I2S2_WS`    （nxt 以数字开头）
      · `USART3_RT`      + `S_DE`      -> `USART3_RTS_DE`      （nxt 形如 X_）
      · `SPI1_MISO/I2`   + `S1_MCK`    -> `SPI1_MISO/I2S1_MCK` （nxt 形如 X1_）
      · `SPI1_MISO`      + `/I2S1_MCK` -> `SPI1_MISO/I2S1_MCK` （nxt 以 / 开头）
      · `SPI3_MOSI/I2S3_S` + `D`       -> `SPI3_MOSI/I2S3_SD`  （nxt 单字符，续接末字母）
      · `USART1_CT`      + `S`         -> `USART1_CTS`         （同上）
      · `ERTC_REFI`      + `N`         -> `ERTC_REFIN`         （同上）

    必须保留空格（两个独立功能名）:
      · `JTDO` + `SWO`         -> `JTDO SWO`
      · `TMR2_CH1` + `TMR2_EXT` -> `TMR2_CH1 TMR2_EXT`

    == 核心判别思想 ==
    看 **nxt 是否「以残片形态」开头**，而不是看 prev 结尾：
      · nxt 单字符（`D`/`S`/`N`）              -> 一定是残片，拼接
      · nxt 以数字 / `/` / `_` / 小写字母开头    -> 残片，拼接
      · nxt 形如 `[A-Z]{1,2}(_|数字)`           -> 残片（`S_DE`、`S1_MCK`），拼接
      · nxt 是完整标识符（多字符大写、含数字的多段）-> 独立功能，保留空格
    """
    if not prev or not nxt:
        return False

    # nxt 单字符 -> 残片（`D`、`S`、`N` 之类，续接 prev 末字母）
    if len(nxt) == 1:
        return True

    # prev 以分隔符结尾 -> 断在分隔符处
    if prev[-1] in "/_-：:,":
        return True

    # nxt 以数字 / 斜杠 / 下划线 / 小写字母开头 -> 残片
    if nxt[0].isdigit() or nxt[0] in "/_" or nxt[0].islower():
        return True

    # nxt 形如「1~2 个大写字母 + 下划线/数字」-> 残片（S_DE、S1_MCK）
    # 注意: 必须先排除「完整外设标识符」，否则 `I2C1_SMBA` 会被 `I`+`2` 误判为残片。
    # 实测反例: `I2C2_SDA` + `I2C1_SMBA` 被错拼成 `I2C2_SDAI2C1_SMBA`。
    if re.match(r"^[A-Z]{1,2}(?:_|\d)", nxt) and not _is_full_periph_name(nxt):
        return True

    # nxt 为纯大写残片（如 `CK` 是 `MCK` 的尾巴、`SD` 是 `I2S3_SD` 的尾巴）:
    # 判据 = nxt 长度 <=3 且全大写 且 **prev 以大写字母结尾**
    # （`SPI3_MISO/I2S3_M`+`CK` 应拼；`JTDO`+`SWO` 的 prev 也以 O 结尾，
    #   但 SWO 是完整功能名 —— 用「nxt 是否构成常见缩写残片」区分不可靠，
    #   故限定 nxt 长度 <=2，`SWO`(3字符) 不受影响）
    if len(nxt) <= 2 and nxt.isupper() and prev[-1].isupper():
        return True

    # 其余（nxt 是完整标识符）-> 保留空格
    return False


# ---- 已知外设/功能前缀：以这些开头的标识符视为**完整名字**，不参与残片判定
# 依据: AT32 手册里的复用功能命名规律（外设名 + 通道/编号 + 可选后缀）
_FULL_PERIPH_PREFIX = re.compile(
    r"^(?:I2C|SPI|USART|UART|TMR|CAN|ADC|DAC|ERTC|EXINT|GPIO|XMC|SDIO|USB|OTG|"
    r"EVENTOUT|IR_OUT|CLKOUT|JTDO|JNTRST|JTDI|JTMS|JTCK|SWO|TRACESWO|"
    r"COMP|OPAMP|CMP|DMA|WDT|WWDT|I2S)[0-9A-Z_]*$"
)


def _is_full_periph_name(s: str) -> bool:
    """判断是否为完整外设名（如 I2C1_SMBA、SPI1_MOSI、USART2_TX）

    这些是**独立功能名**，即使形如「大写+数字」也不能当残片拼到前一格。
    """
    return bool(_FULL_PERIPH_PREFIX.match(s))


def build_rows(words: Sequence[dict], bounds: Sequence[float],
               bands: Sequence[Tuple[float, float]]) -> List[List[str]]:
    """行带 = 行；行带内按列归位；同格多段文字按 y 升序拼接"""
    ncols = len(bounds) - 1
    rows: List[List[str]] = []
    for (a, b) in bands:
        cells: List[List[dict]] = [[] for _ in range(ncols)]
        for w in words:
            if a <= w["cy"] <= b:
                cells[col_index((w["x0"] + w["x1"]) / 2.0, bounds)].append(w)
        out = []
        for cws in cells:
            cws.sort(key=lambda w: (round(w["cy"], 1), w["x0"]))
            text = ""
            for w in cws:
                t = w["text"]
                if not text:
                    text = t
                    continue
                sep = "" if _join_hint(text, t) else " "   # 不要用 and/or 三目，空串 falsy
                text = text + sep + t
            out.append(text.strip())
        rows.append(out)
    return rows


def merge_wrapped_rows(rows: Sequence[Sequence[str]]) -> List[List[str]]:
    """合并跨行带的折行续行。

    续行必要条件 = **首列为空** 且非空格 <= 2。
    反例（必须不合并）: p95 的 `PD5` + `USART2_TX` —— 首列有值，是本行独立数据。
    """
    out: List[List[str]] = []
    for r in rows:
        if not out:
            out.append(list(r))
            continue
        nonempty = sum(1 for c in r if c)
        if nonempty == 0:
            continue
        prev = out[-1]
        if not r[0] and nonempty <= 2:
            for i, c in enumerate(r):
                if not c:
                    continue
                if i < len(prev):
                    prev[i] = (prev[i] + " " + c).strip() if prev[i] else c
            continue
        out.append(list(r))
    return out


# ---------------------------------------------------------------- 主流程


def rebuild_tables_on_page(plumber_page, mupdf_page, page_no: int) -> List[TableBlock]:
    """返回该页的 TableBlock 列表。

    · 有栅格几何 → mode="grid"，精确重建，置信度按实际质量打分；
    · 无栅格几何但有表格特征 → mode="suspect"，**不出表**，只标记待复核；
    · 都不是 → 不返回任何块（该页按纯文字处理）。
    """
    words = page_words(plumber_page)
    if not words:
        return []

    rules = h_rules(mupdf_page)
    vx = grid_vlines(mupdf_page)

    # ---- 路径 A：有栅格几何 → 精确重建 ----
    # v3（2026-10-29 二次修正）: 竖线门槛不是简单降到 1。
    # 实测教训: 直接降到 >=1 后，p178 的**框图与正文**也被判成表格（因为它页面上
    # 有 1 条无关的栅格碎片），而真正的表 12-5 列数还算错了（5 列而非 3 列）。
    #
    # 正确判据: 页级竖线只是「候选」，每个表格区域**独立**校验：
    #   grid_col_bounds 返回的列数必须 >= 3（即至少 2 条内部边界），
    #   且这些边界必须真的落在该区域的文字横向范围内。
    # 这样只有真正带网格的区域才走 grid，其余退回 suspect。
    if len(vx) >= 1 and len(rules) >= 3:
        groups = fix_oversized_first_band(words, band_groups(row_bands(rules)))
        blocks: List[TableBlock] = []
        for g in groups:
            band_top, band_bottom = g[0][0], g[-1][1]
            # 按区域取竖线（不是页级），避免同页无关碎片污染非表格区域
            vx_region = grid_vlines_in(mupdf_page, band_top, band_bottom)
            if not vx_region:
                continue
            bounds = grid_col_bounds(vx_region, words, band_top, band_bottom)
            # 至少 3 列（= 2 条内部边界）才算真有网格；否则该区域退回 suspect 路径
            if len(bounds) < MIN_GRID_COLS + 1:
                continue
            rows = merge_wrapped_rows(build_rows(words, bounds, g))
            tb = TableBlock(page=page_no, y0=band_top, y1=band_bottom,
                            x0=bounds[0], x1=bounds[-1],
                            rows=rows, col_bounds=bounds,
                            mode="grid",
                            caption=_caption(words, band_top),
                            raw_text=_raw(words, band_top, band_bottom))
            tb.confidence = _score_grid(tb)
            tb.accepted = tb.confidence >= CONF_ACCEPT
            blocks.append(tb)
        if blocks:
            return blocks
        # 一个区域都没通过 → 落到下方 suspect 路径（不 return 空列表，
        # 否则该页所有疑表区域都会消失，正文侧也不会补上内容）

    # ---- 路径 B：无栅格 → 只标记「疑有表格」，不出表 ----
    # 判据必须严格，否则会产出数百条噪音标记（实测松判据 555 条，绝大多数是
    # 整段正文而非表格），反而淹没真正需要复核的少数表格。
    #
    # 真表格 vs 正文段落的判别特征（AT32 手册实测）：
    #   · 真表格：多行、每行词数少（<=5）、行间有规则空隙、横向分成多个 x 簇；
    #   · 正文段落：每行词数多（10+）、行间紧凑、整行连续无列间隙。
    groups = band_groups(row_bands(rules))
    suspects: List[TableBlock] = []
    for g in groups:
        band_top, band_bottom = g[0][0], g[-1][1]

        # 跨度过滤：单表跨度很少超过 300pt（超过多半是整页正文）
        if (band_bottom - band_top) > SUSPECT_MAX_SPAN:
            continue

        ws = [w for w in words if band_top <= w["cy"] <= band_bottom]
        if len(ws) < SUSPECT_MIN_WORDS:
            continue

        row_ys = _cluster([w["cy"] for w in ws], tol=3.0)
        if not (SUSPECT_MIN_ROWS <= len(row_ys) <= SUSPECT_MAX_ROWS):
            continue

        starts = _cluster([w["x0"] for w in ws], tol=12.0)
        if len(starts) < SUSPECT_MIN_COLS:
            continue

        n_rules = sum(1 for y in rules if band_top <= y <= band_bottom)
        if n_rules < SUSPECT_MIN_RULES:
            continue

        # 关键判据：每行平均词数。正文段落每行 10+ 词；表格每行通常 2~6 词。
        per_row = len(ws) / float(len(row_ys))
        if per_row > SUSPECT_MAX_WORDS_PER_ROW:
            continue

        # 决定性判据：**列对齐规整性**。
        # 真表格的同一列在每行 x 位置高度一致；框图/时序图/散乱文字则不然。
        # 实测反例: p22 框图（文字散乱）、p168 时序图（文字散布）都因「每行词数少」
        # 被误判为表格。加上本判据后二者被正确排除。
        if not _columns_aligned(ws, row_ys):
            continue

        tb = TableBlock(page=page_no, y0=band_top, y1=band_bottom,
                        x0=min(w["x0"] for w in ws), x1=max(w["x1"] for w in ws),
                        mode="suspect",
                        caption=_caption(words, band_top),
                        raw_text=_raw(words, band_top, band_bottom),
                        suspect_words=list(ws))
        tb.confidence = 0.0
        tb.reasons.append("no_grid_geometry")
        tb.accepted = False
        suspects.append(tb)
    return suspects


def _columns_aligned(ws: Sequence[dict], row_ys: Sequence[float],
                     tol: float = 8.0) -> bool:
    """判断区域内文字是否**按列对齐**（真表格的决定性特征）。

    做法: 把每行的词按 x0 归入「起始位置簇」，统计各簇在多少行出现过。
    真表格: 多数簇出现在 >=60% 的行里（因为每行都有该列的值）。
    框图/时序图: 文字散布，簇的行覆盖率低。

    实测校准（AT32F423 RM）:
      · p95 MUX 表 → 9 列，各列行覆盖率 ~100% → 判为表 ✓
      · p22 框图    → 簇行覆盖率 <40% → 判为非表 ✓
      · p168 时序图 → 簇行覆盖率 <50% → 判为非表 ✓
    """
    if len(row_ys) < SUSPECT_MIN_ROWS:
        return False

    # 全部词的 x0 聚类成列锚点
    anchors = _cluster([w["x0"] for w in ws], tol=tol)
    if len(anchors) < SUSPECT_MIN_COLS:
        return False

    def col_of(x0: float) -> int:
        return min(range(len(anchors)), key=lambda i: abs(x0 - anchors[i]))

    # 每行命中了哪些列
    row_cols: List[set] = []
    for cy in row_ys:
        row_ws = [w for w in ws if abs(w["cy"] - cy) <= 3.0]
        if not row_ws:
            continue
        row_cols.append({col_of(w["x0"]) for w in row_ws})

    if not row_cols:
        return False

    # 各列的行覆盖率
    n_rows = len(row_cols)
    cover = []
    for ci in range(len(anchors)):
        hit = sum(1 for rc in row_cols if ci in rc)
        cover.append(hit / float(n_rows))

    # 真表格: 至少一半的列被 >=60% 的行命中
    aligned = sum(1 for c in cover if c >= 0.6)
    return aligned >= max(2, int(len(anchors) * 0.5))


def _caption(words: Sequence[dict], band_top: float) -> str:
    lo = band_top - 26.0
    ws = [w for w in words if lo <= w["cy"] < band_top]
    ws.sort(key=lambda w: (w["cy"], w["x0"]))
    return " ".join(w["text"] for w in ws)


def _raw(words: Sequence[dict], y0: float, y1: float) -> str:
    ws = [w for w in words if y0 <= w["cy"] <= y1]
    ws.sort(key=lambda w: (round(w["cy"], 1), w["x0"]))
    lines, cur_y, cur = [], None, []
    for w in ws:
        if cur_y is None or abs(w["cy"] - cur_y) <= 3.0:
            cur.append(w["text"])
            cur_y = w["cy"] if cur_y is None else cur_y
        else:
            lines.append(" ".join(cur))
            cur, cur_y = [w["text"]], w["cy"]
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def _score_grid(tb: TableBlock) -> float:
    """grid 路径置信度：几何可靠，主要看内容规整度"""
    reasons = tb.reasons
    n_rows, n_cols = tb.n_rows, tb.n_cols
    if n_rows < MIN_TABLE_ROWS or n_cols < MIN_TABLE_COLS:
        return 0.0

    # a) 首列稳定（引脚名/位/序号几乎每行都有值）
    firsts = [r[0] for r in tb.rows]
    a = sum(1 for f in firsts if f) / float(len(firsts))
    a = 1.0 if a >= 0.85 else (0.6 if a >= 0.5 else 0.25)
    if a < 0.5:
        reasons.append("first_column_sparse")

    # b) 行填充稳定
    filled = [sum(1 for c in r if c) for r in tb.rows[1:]] or [0]
    mean = sum(filled) / len(filled)
    sd = (sum((f - mean) ** 2 for f in filled) / len(filled)) ** 0.5
    b = 1.0 if sd <= 1.5 else (0.65 if sd <= 3.0 else 0.3)
    if mean < 1.2:
        b = 0.2
        reasons.append("rows_almost_empty")

    # c) 表头横跨多列
    hdr_filled = sum(1 for c in tb.rows[0] if c) if tb.rows else 0
    ratio = hdr_filled / float(n_cols)
    c = 1.0 if ratio >= 0.6 else (0.55 if ratio >= 0.35 else 0.2)
    if ratio < 0.35:
        reasons.append("header_sparse")

    # d) 列宽规律
    widths = [tb.col_bounds[i + 1] - tb.col_bounds[i] for i in range(n_cols)]
    total = sum(widths) or 1.0
    widest = max(widths) / total
    d = 1.0 if widest <= 0.5 else (0.55 if widest <= 0.7 else 0.25)
    if widest > 0.7:
        reasons.append("column_too_wide")

    # e) 列数合理
    e = 1.0
    if n_cols > 16:
        e = 0.3
        reasons.append("too_many_columns")
    elif n_cols > 12:
        e = 0.7

    # f) 【决定性】单元格文字密度异常 = 列数不足（多列内容被压进一格）
    # 实测反例（2026-10-29，最危险的一类错误）:
    #   p173 表 12-1（波特率误差表，实际 8 列）只有 2 条竖线 -> 被判 3 列，
    #   每个单元格塞进多个字段，例如 `115.3843120.15%`（= 115.384 + 312 + 0.15%）
    #   而 _score_grid 仍给出 conf=1.00 —— 高置信度的错表，入库即污染。
    #
    # 判据设计（两次校准后）:
    #   不能用「数字段个数」——`SPI2_MISO/I2S2_MCK` 有 3 个数字（误伤 p95 的 MUX 表），
    #   而 `115.3843120.15%` 的正则只识别出 2 段（`115.3843120` 被当成一个数）。
    #   可靠特征: **一个数字串里出现 >=2 个小数点**（多字段被无分隔拼接），
    #   或单元格长度 >=14 且含百分号（数值+误差被拼）。
    def _looks_compressed(cell: str) -> bool:
        if len(cell) < 12:
            return False
        # 特征1: 单个 token 内多个小数点（如 115.3843120.15）
        for tok in cell.split():
            if tok.count(".") >= 2:
                return True
        # 特征2: 长且含百分号（如 230.7691560.16%）
        if len(cell) >= 13 and cell.count("%") >= 1:
            return True
        # 特征3: 同一格内出现两个百分号
        if cell.count("%") >= 2:
            return True
        return False

    cell_lens = [c for r in tb.rows[1:] for c in r if c]
    if cell_lens:
        dense = sum(1 for c in cell_lens if _looks_compressed(c))
        if dense >= max(2, len(cell_lens) // 4):
            f_score = 0.0
            reasons.append("cells_density_suspect_columns_too_few")
        else:
            f_score = 1.0
    else:
        f_score = 0.5

    score = (0.22 * a + 0.24 * b + 0.16 * c + 0.12 * d + 0.10 * e + 0.16 * f_score)
    if f_score == 0.0:
        # 列数不足是结构性缺陷，直接判不合格（不能靠其他项拉回 0.85）
        return min(round(max(0.0, min(1.0, score)), 2), round(CONF_ACCEPT - 0.01, 2))
    return round(max(0.0, min(1.0, score)), 2)


# ---------------------------------------------------------------- Markdown


def crop_name(tb: TableBlock) -> str:
    return "_review/p%03d_t%d.png" % (tb.page, int(round(tb.y0)))


def to_markdown(tb: TableBlock) -> str:
    if tb.mode == "suspect":
        return _suspect_md(tb)
    if not tb.accepted:
        return _needs_agent_md(tb)
    if not tb.rows:
        return ""
    cols = max(len(r) for r in tb.rows)
    norm = [list(r) + [""] * (cols - len(r)) for r in tb.rows]
    out = []
    if tb.caption:
        out.append("**%s**\n" % tb.caption.strip())
    out.append(_row_md(norm[0]))
    out.append(_row_md(["---"] * cols))
    for r in norm[1:]:
        out.append(_row_md(r))
    return "\n".join(out)


def _row_md(cells: Sequence[str]) -> str:
    return "| " + " | ".join(_esc(c) for c in cells) + " |"


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>").strip()


def _needs_agent_md(tb: TableBlock) -> str:
    """grid 路径但置信度不足 → 给 agent 读图重写"""
    why = ",".join(tb.reasons) if tb.reasons else "low_confidence"
    crop = crop_name(tb)
    return (
        "<!-- TABLE_NEEDS_AGENT: page=%d confidence=%.2f rows=%d cols=%d "
        "y0=%.1f y1=%.1f mode=%s reasons=%s crop=%s -->\n"
        "\n> ⚠ **本表自动重建置信度 %.2f，未采用自动结果。**\n"
        "> 请 agent 用视觉能力读取截图 `%s`（原册 p.%d，y=%.1f~%.1f）后重写为 Markdown 表，"
        "并删除本标记块。要求：\n"
        "> 1. 列数与截图表头一致，不增删列；\n"
        "> 2. 空单元格留空，不填「-」，不猜测；\n"
        "> 3. 单元格内折行合并为一行；\n"
        "> 4. 表后补 `> 来源：原册 p.%d`。\n"
        "> 未达标项：`%s`\n\n"
        "**程序提取的原始文字（供交叉核对）**\n\n"
        "```text\n%s\n```\n"
        % (tb.page, tb.confidence, tb.n_rows, tb.n_cols, tb.y0, tb.y1, tb.mode, why, crop,
           tb.confidence, crop, tb.page, tb.y0, tb.y1, tb.page, why, tb.raw_text)
    )


def _suspect_md(tb: TableBlock) -> str:
    """无栅格几何 -> 不硬猜表格，输出**带列名提示的线性文字**。

    == 设计决策（2026-10-29 实测后的最终版）==
    曾试过「列名键值对」方案（输出 中断事件：发送数据寄存器空 | 事件标志：TDBE），
    实测发现表头识别失败时会产生**假结构**——列名退化成「列1/列3」，
    且把本该分列的内容拼成 EXINT_LINE[16]：EXINT_LINE[17]，
    看起来有结构但结构是错的，比纯线性文字更危险。

    ⇒ 最终选择: 保留线性文字（保持原文顺序，不制造结构），
      但明确告知「列对应关系需对照原册核对」，并把表头词单独列出供参考。
    """
    crop = crop_name(tb)
    cap = ("（%s）" % tb.caption) if tb.caption else ""

    # 表头词（区域内最上面一行文字），仅作「可能的列名」提示，不据此重组内容
    hdr_hint = _header_hint(tb)

    return (
        "<!-- TABLE_SUSPECT: page=%d y0=%.1f y1=%.1f crop=%s -->\n"
        "\n> ℹ 本页此处**疑为表格**%s（原册 p.%d，y=%.1f~%.1f）。\n"
        "> 该处无栅格几何线，程序**未自动生成表格**（避免错列污染知识库）。\n"
        ">%s"
        "> ⚠ 下方为原文线性排列，**列对应关系需对照原册 p.%d 核对**，勿凭语序推断。\n"
        "> 若需标准表格，请 agent 读取截图 %s 后重写。\n\n"
        "`	ext\n%s\n`\n"
        % (tb.page, tb.y0, tb.y1, crop, cap, tb.page, tb.y0, tb.y1,
           hdr_hint, tb.page, crop, tb.raw_text)
    )


def _header_hint(tb: TableBlock) -> str:
    """提取区域内最上面一行文字，作为「可能的列名」提示（不重组内容）"""
    ws = tb.suspect_words
    if not ws:
        return ""
    row_ys = _cluster([w["cy"] for w in ws], tol=3.0)
    if not row_ys:
        return ""
    top = [w for w in ws if abs(w["cy"] - row_ys[0]) <= 3.0]
    top.sort(key=lambda w: w["x0"])
    names = [w["text"] for w in top if w["text"]]
    if len(names) < 2:
        return ""
    return "可能的列名（程序推断，需核对）：%s\n" % " | ".join(names[:12])
