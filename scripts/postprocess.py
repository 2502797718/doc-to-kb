#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""postprocess.py — 把 MinerU 输出整理成「按章节分卷 + 可入库」的 Markdown

为什么需要这一步
----------------
MinerU 直接产出的 Markdown 有四个入库障碍（实测 RN7326E / IEC 61000-4-7）：

  1) **页眉页脚噪音**：middle_json 的 block 类型里，`header` / `footer` /
     `page_number` 全是版面噪音。实测 3 页里就有 6 个 header + 3 个 page_number。
     试点 44 页曾达 147/762 块（19%）。
  2) **无页锚点**：裸 markdown 不含页码，无法按书签切分。
  3) **体积不受控**：单卷可能超 65,535B（向量库单字段硬限，超了静默失索引）。
  4) **无导航**：缺 README（章↔文件↔页码对照）。

解法：**读 middle_json，按 page_idx + block type 重建干净 Markdown**。
这比「在裸 markdown 里猜页码」可靠得多——page_idx 是 MinerU 直接给的，
不需要任何锚点启发式。

用法
----
  python postprocess.py --pdf <pdf> --out <out_dir> [--chip X] [--manual Y]

  要求 <out_dir>/raw 下有 mineru_parse 用 --format zip 解包出的
  middle_json.json + images/。

产出
----
  <out_dir>/NN_<章名>.md      分卷
  <out_dir>/README.md         章↔文件↔页码对照
  <out_dir>/_manifest.json    卷清单（供 verify_md.py）
  <out_dir>/_postprocess_report.json  噪音过滤统计
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # pragma: no cover
    print("缺少 pymupdf：pip install pymupdf", file=sys.stderr)
    raise

VOLUME_LIMIT = 60000
NOISE_TYPES = {"header", "footer", "page_number", "aside_text", "page_footnote"}
KEEP_TABLE_AS_MD = True


# ------------------------------------------------------------------ 工具


def sanitize(name: str, maxlen: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    s = re.sub(r"\s+", "_", s).strip("._")
    return s[:maxlen] or "untitled"


def find_middle_json(raw_dir: Path) -> Path | None:
    """在 raw 目录里定位 middle_json.json（可能在子目录）。"""
    for p in raw_dir.rglob("middle_json.json"):
        return p
    return None


# ------------------------------------------------------------------ 书签


def build_toc_tree(doc) -> list:
    raw = [(l, t.strip(), p) for l, t, p in doc.get_toc() if t and p and p > 0]
    if not raw:
        return []

    def build(items):
        nodes, k = [], 0
        while k < len(items):
            l, t, p = items[k]
            nxt = k + 1
            while nxt < len(items) and items[nxt][0] > l:
                nxt += 1
            nodes.append({
                "level": l, "title": t, "page": p,
                "children": build(items[k + 1:nxt]),
            })
            k = nxt
        return nodes

    return build(raw)


def build_toc_from_titles(middle: dict) -> list:
    """无书签 PDF 的分卷回退：用 MinerU 的标题块构建章节树。

    实测 DL/T 645-2007（89 页、有文字层、书签 0 条）：
    MinerU 识别出 137 个 title 类块，其中 paragraph_title 带 level，
    本身就是「1 范围 / 4.1.1 读数头结构 / 4.1.5.1 波长」这样的章节树。

    处理两个实测噪声：
      1. 编号与术语名被拆成两个块（"3.8" + "总线 bus"）→ 合并；
      2. 所有块 level 相同（本手册全是 L2）→ 按标题文本里的编号深度
         推导层级（"4" → 1，"4.1" → 2，"4.1.1" → 3），比 level 可靠。
    """
    TITLE_TYPES = {"title", "doc_title", "paragraph_title"}

    seq: list[dict] = []
    for page in middle.get("pages", []):
        for b in page.get("blocks", []):
            if b.get("type") not in TITLE_TYPES:
                continue
            txt, _ = _flatten_content(b.get("content"))
            txt = " ".join(txt.split())
            if not txt:
                continue
            seq.append({
                "page": page.get("page_idx", 0) + 1,   # 1-based，与书签口径一致
                "text": txt,
                "level": b.get("level"),
            })

    if not seq:
        return []

    # 合并「纯编号」标题与其后的术语名
    merged: list[dict] = []
    i = 0
    while i < len(seq):
        t = seq[i]
        bare = re.fullmatch(r"\d+(\.\d+)*", t["text"]) is not None
        if bare and i + 1 < len(seq) and not re.fullmatch(
                r"\d+(\.\d+)*", seq[i + 1]["text"]):
            merged.append({**t, "text": t["text"] + " " + seq[i + 1]["text"]})
            i += 2
            continue
        merged.append(t)
        i += 1

    # 推导层级：优先用编号深度（"4.1.2" → 3）
    # 无编号标题的取舍（2026-11-03 实测 IEC 61000-4-30 扫描件）：
    #   362 个标题里 237 个无编号 —— 扫描件 OCR 会把「广告页条目 / 页眉 /
    #   正文句子」全误判成 paragraph_title，若一律当一级卷，70 页会被切成
    #   171 卷（最小 142 B，只有标题没正文）。
    #   所以无编号标题只在**白名单关键词**命中时才保留，其余丢弃。
    KEEP_UNNUM = re.compile(
        r"^(?:"
        r"FOREWORD|INTRODUCTION|CONTENTS|Annex\s+[A-Z]|"
        r"Bibliography|BIBLIOGRAPHY|Index|INDEX|"
        r"Figure\s+[A-Z]?\d+|Table\s+[A-Z]?\d+"
        r")\b", re.I)
    # 明显是广告页 / 版权页 / 页眉噪音
    DROP_UNNUM = re.compile(
        r"^(?:About\s+the\s+IEC|About\s+IEC|IEC\s+Just\s+Published|"
        r"IEC\s+Customer|IEC\s+online|Electropedia|"
        r"A\s+propos|Recherche\s+de|Service\s+Clients|webstore\.iec\.ch|"
        r"THIS\s+PUBLICATION|Copyright|All\s+rights|Droits|"
        r"INTERNATIONAL\s+ELECTROTECHNICAL\s+COMMISSION|"
        r"ELECTROMAGNETIC\s+COMPATIBILITY|"
        r"Part\s+\d+:|Part\s+4-30)",
        re.I)

    def depth_of(text: str) -> int | None:
        """返回编号深度；无编号返回 None（由调用方决定保留还是丢弃）。"""
        m = re.match(r"(\d+(?:\.\d+)*)\s", text) or re.match(r"(\d+(?:\.\d+)*)$", text)
        if m:
            return 1 + (m.group(1).count("."))
        return None

    raw = []
    for t in merged:
        d = depth_of(t["text"])
        if d is not None:
            raw.append((d, t["text"], t["page"]))
            continue
        # 无编号：先排除噪音，再按白名单保留
        if DROP_UNNUM.match(t["text"]):
            continue
        if KEEP_UNNUM.match(t["text"]):
            raw.append((1, t["text"], t["page"]))
            continue
        # 其余无编号标题：丢弃（多半是 OCR 误判的正文句子 / 广告条目）
        continue

    if not raw:
        return []
    # 去重同页同标题，避免重复节点
    seen = set()
    deduped = []
    for lvl, title, pg in raw:
        key = (pg, title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((lvl, title, pg))

    def build(items):
        nodes, k = [], 0
        while k < len(items):
            l, t, p = items[k]
            nxt = k + 1
            while nxt < len(items) and items[nxt][0] > l:
                nxt += 1
            nodes.append({
                "level": l, "title": t, "page": p,
                "children": build(items[k + 1:nxt]),
            })
            k = nxt
        return nodes

    return build(deduped)


def count_nodes(nodes: list) -> int:
    """统计章节树节点总数。"""
    n = 0
    for x in nodes:
        n += 1 + count_nodes(x.get("children", []))
    return n


def flatten_with_pages(nodes: list, total_pages: int, parent_end: int | None = None) -> list:
    out = []
    for i, n in enumerate(nodes):
        end = nodes[i + 1]["page"] - 1 if i + 1 < len(nodes) else (
            parent_end if parent_end is not None else total_pages
        )
        out.append({**n, "end": max(n["page"], end)})
    return out


# ------------------------------------------------------------------ 重建


def _flatten_content(content) -> tuple[str, str]:
    """把 block.content 归一成 (文本, 图片相对路径)。

    middle_json 的 content 是多形态的（实测 RN7326E / MinerU 4.1.3）：
      · str                                → 直接用
      · list[{"type","content"}]           → 拼接各段 content
      · list[{"type","image_path",...}]    → 取 image_path
      · list[{"type","content":[嵌套]}]    → 递归展开
    含 `<table>` 的字符串在此统一转成 Markdown pipe 表，避免上层漏转。
    """
    if content is None:
        return "", ""
    if isinstance(content, str):
        return _maybe_html_table(content.strip()), ""

    texts: list[str] = []
    img = ""
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                texts.append(_maybe_html_table(item.strip()))
                continue
            if not isinstance(item, dict):
                continue
            if not img:
                for key in ("image_path", "img_path"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        img = v.strip()
                        break
            inner = item.get("content")
            if isinstance(inner, str) and inner.strip():
                texts.append(_maybe_html_table(inner.strip()))
            elif isinstance(inner, list):
                sub, sub_img = _flatten_content(inner)
                if sub:
                    texts.append(sub)
                if not img and sub_img:
                    img = sub_img
    # MinerU 的 image_path 自带 `images/` 前缀（实测 RN7326E / DLT645 / AT32 均如此），
    # 这里统一剥掉，让上层拼接 img_prefix 时不会出现 `images/images/`。
    # 2026-11-01 踩过：AT32 RM 206 处、RN7326E 37 处、DS 36 处全成了双层路径。
    while img.startswith("images/"):
        img = img[len("images/"):]
    return "\n".join(t for t in texts if t).strip(), img


def _maybe_html_table(s: str) -> str:
    """字符串若是 HTML 表格则转成 Markdown pipe 表。"""
    if s and "<table" in s.lower():
        return html_table_to_md(s)
    return s


def html_table_to_md(html: str) -> str:
    """把 MinerU 的 HTML 表格转成 Markdown pipe 表（保留 rowspan/colspan 的语义降级）。"""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I)
    if not rows:
        return html

    parsed: list[list[str]] = []
    for r in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.S | re.I)
        clean = []
        for c in cells:
            t = re.sub(r"<[^>]+>", "", c)
            t = (t.replace("&nbsp;", " ").replace("&lt;", "<")
                  .replace("&gt;", ">").replace("&amp;", "&"))
            t = re.sub(r"\s+", " ", t).strip()
            # 单元格内的 | 必须转义，否则会把 5 列撑成 7 列
            # 实测：`|TEMP - TEMP_UD| ≥ LT_GAP` 导致 [5,7] 列数不一致
            t = t.replace("|", "\\|")
            clean.append(t)
        if clean:
            parsed.append(clean)

    if not parsed:
        return html

    width = max(len(r) for r in parsed)
    norm = [r + [""] * (width - len(r)) for r in parsed]

    lines = ["| " + " | ".join(norm[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for r in norm[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def blocks_to_markdown(page: dict, img_prefix: str) -> tuple[str, dict]:
    """把一页的 blocks 转成干净 Markdown，同时返回本页噪音统计。"""
    stats = {"header": 0, "footer": 0, "page_number": 0, "other_noise": 0,
             "table": 0, "text": 0, "title": 0, "image": 0, "equation": 0}
    lines: list[str] = []

    for b in page.get("blocks", []):
        t = b.get("type", "")
        content, img_rel = _flatten_content(b.get("content"))

        if t in NOISE_TYPES:
            key = t if t in ("header", "footer", "page_number") else "other_noise"
            stats[key] = stats.get(key, 0) + 1
            continue

        if t == "table":
            stats["table"] += 1
            if content:
                if content.lstrip().startswith("<"):
                    lines.append(html_table_to_md(content))
                else:
                    lines.append(content)
                lines.append("")
            elif img_rel:
                lines.append(f"![表格]({img_prefix}{img_rel})")
                lines.append("")
            else:
                # content 为空且无图：记录，供抽样复核（MinerU 偶发丢表）
                stats["table_empty"] = stats.get("table_empty", 0) + 1
            continue

        if t in ("paragraph_title", "title", "doc_title"):
            stats["title"] += 1
            if content:
                level = b.get("level") or 2
                try:
                    level = max(1, min(6, int(level)))
                except (TypeError, ValueError):
                    level = 2
                lines.append("#" * level + " " + content.replace("\n", " "))
                lines.append("")
            continue

        if t in ("image", "figure"):
            stats["image"] += 1
            if img_rel:
                alt = (content or "图").replace("\n", " ")[:80]
                lines.append(f"![{alt}]({img_prefix}{img_rel})")
                if content:
                    lines.append(f"*{content}*")
                lines.append("")
            elif content:
                lines.append(content)
                lines.append("")
            continue

        if t == "equation":
            stats["equation"] += 1
            if content:
                lines.append("$$")
                lines.append(content)
                lines.append("$$")
                lines.append("")
            continue

        stats["text"] += 1
        if content:
            lines.append(content)
            lines.append("")

    return "\n".join(lines).strip(), stats


def _block_top(b: dict) -> float:
    """块的上边界 y（归一化 0~1）。用于页内切分。"""
    bb = b.get("bbox")
    if isinstance(bb, (list, tuple)) and len(bb) >= 2:
        try:
            return float(bb[1])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _block_text(b: dict) -> str:
    txt, _ = _flatten_content(b.get("content"))
    return txt



def _blocks_before_title(blocks: list[dict], title: str) -> list[dict]:
    """Return non-title blocks that appear BEFORE the given title on a page.

    Walks backward from the title anchor, collecting all non-title blocks.
    Intermediate titles (parent or sibling sections) are skipped — the
    caller's page_consumed counter ensures each block is assigned to
    exactly one volume.
    """
    if not title or not blocks:
        return []
    tnorm = re.sub(r"\s+", "", title or "")
    if not tnorm:
        return []
    anchor = None
    for k, b in enumerate(blocks):
        if re.sub(r"\s+", "", _block_text(b)).startswith(tnorm[:24]):
            anchor = k
            break
    if not anchor or anchor <= 0:
        return []
    tail = []
    for k in range(anchor - 1, -1, -1):
        bt = blocks[k].get("type", "")
        if bt not in ("paragraph_title", "doc_title", "title"):
            tail.append(blocks[k])
        # 标题块跳过但继续向前找（父章节标题、兄弟章节标题不阻断）
    tail.reverse()
    return tail

def build_page_blocks(middle: dict) -> dict[int, list[dict]]:
    """page_idx -> 该页所有有效块（已剔除噪音）。保留 bbox 供页内切分。"""
    out: dict[int, list[dict]] = {}
    for page in middle.get("pages", []):
        idx = page.get("page_idx")
        if idx is None:
            continue
        keep = []
        for b in page.get("blocks", []):
            if b.get("type") in NOISE_TYPES:
                continue
            keep.append(b)
        # 按 y 排序，保证页内顺序稳定
        keep.sort(key=_block_top)
        out[idx] = keep
    return out


def render_blocks(blocks: list[dict], img_prefix: str) -> tuple[str, dict]:
    """渲染一组 block 为 Markdown（复用 blocks_to_markdown 的逻辑）。"""
    return blocks_to_markdown({"blocks": blocks}, img_prefix)


def page_md_from_blocks(page_blocks: dict[int, list[dict]], img_prefix: str) -> dict[int, str]:
    """整页渲染（用于统计与兜底）。"""
    out: dict[int, str] = {}
    for idx, blocks in page_blocks.items():
        md, _ = render_blocks(blocks, img_prefix)
        out[idx] = md
    return out


def slice_page_blocks(blocks: list[dict], start_title: str | None,
                      end_title: str | None) -> list[dict]:
    """在一页内按「小标题」切出归属本卷的块。

    为什么需要：实测同一页常有多个小节（如 p.80 上 4.5.2 与 4.5.3），
    按整页切会让两卷正文完全相同（verify_md 关2 报「同页切分失效」23 处）。

    规则：
      · start_title 给定时，从「该标题所在块」开始取（含）；
      · end_title 给定时，到「该标题所在块」之前结束（不含）；
      · 找不到 start_title 则整页返回（保守，不丢内容）。
    """
    if not blocks:
        return []

    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    def find(title: str, after: int = 0) -> int | None:
        t = norm(title)
        if not t:
            return None
        for i in range(after, len(blocks)):
            if norm(_block_text(blocks[i])).startswith(t[:24]):
                return i
        return None

    start = 0
    if start_title:
        s = find(start_title)
        if s is None:
            return list(blocks)
        start = s

    end = len(blocks)
    if end_title:
        e = find(end_title, start + 1)
        if e is not None:
            end = e

    return list(blocks[start:end])


# ------------------------------------------------------------------ 分卷


def page_bytes(page_md: dict, start: int, end: int) -> int:
    n = 0
    for p in range(start - 1, end):
        n += len(page_md.get(p, "").encode("utf-8"))
    return n


def split_volumes(nodes: list, page_md: dict, limit: int,
                  parent_path: list | None = None,
                  parent_end: int | None = None,
                  total_pages: int = 0) -> list[dict]:
    parent_path = parent_path or []
    flat = flatten_with_pages(nodes, total_pages, parent_end)
    volumes: list[dict] = []

    for n in flat:
        b = page_bytes(page_md, n["page"], n["end"])
        if b <= limit or not n["children"]:
            volumes.append({
                "title": n["title"], "level": n["level"],
                "page": n["page"], "end": n["end"],
                "path": parent_path + [n["title"]],
            })
        else:
            sub = split_volumes(n["children"], page_md, limit,
                                parent_path + [n["title"]], n["end"], total_pages)
            if sub:
                first_sub = sub[0]["page"]
                if first_sub > n["page"]:
                    volumes.append({
                        "title": f"{n['title']}（引言 p.{n['page']}）",
                        "level": n["level"], "page": n["page"],
                        "end": first_sub - 1,
                        "path": parent_path + [n["title"]],
                        "is_intro": True,
                    })
                volumes.extend(sub)
            else:
                volumes.append({
                    "title": n["title"], "level": n["level"],
                    "page": n["page"], "end": n["end"],
                    "path": parent_path + [n["title"]],
                })
    return volumes


def enforce_limit(volumes: list[dict], page_md: dict, limit: int) -> list[dict]:
    """超限卷按页硬切。"""
    out: list[dict] = []
    for v in volumes:
        if page_bytes(page_md, v["page"], v["end"]) <= limit or v["page"] >= v["end"]:
            out.append(v)
            continue
        p, part = v["page"], 1
        while p <= v["end"]:
            q = p
            while q < v["end"] and page_bytes(page_md, p, q + 1) <= limit:
                q += 1
            out.append({**v, "title": f"{v['title']}（第{part}部分）",
                        "page": p, "end": q, "split": True})
            p, part = q + 1, part + 1
    return out


# ------------------------------------------------------------------ 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="MinerU 输出后处理与分卷")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True, help="mineru_parse 的同一 out 目录")
    ap.add_argument("--chip", default="chip")
    ap.add_argument("--manual", default="manual")
    ap.add_argument("--limit", type=int, default=VOLUME_LIMIT)
    ap.add_argument("--img-prefix", default="",
                    help="图片路径额外前缀。MinerU 的 image_path 已自带 'images/' 前缀"
                         "（实测 DL/T645 与 RN7326E 均如此），故默认留空。"
                         "**若给了 --viking-base，本参数被忽略**（见下）。")
    ap.add_argument("--viking-base", default="",
                    help="该手册入库后的 viking URI 前缀，如 "
                         "viking://resources/shared/chips/AT32F423/RM_V2.04_CH/"
                         "给了它就生成 `![alt](viking://.../images/x.jpg)` 绝对引用。"
                         "**强烈建议总是给** —— 实测 OpenViking 渲染器只认 viking:// 绝对 URI，"
                         "相对路径一律渲染成普通链接文字、图片显示不出来（2026-11-01 四写法实测）。")
    args = ap.parse_args()

    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    mid_path = find_middle_json(raw_dir)
    if not mid_path:
        print(f"[postprocess] 在 {raw_dir} 下找不到 middle_json.json。\n"
              f"  请用 mineru_parse.py --format zip 重新解析。", file=sys.stderr)
        return 2

    middle = json.loads(mid_path.read_text(encoding="utf-8"))
    page_blocks = build_page_blocks(middle)

    # 图片路径前缀解析（2026-11-01 实测：OpenViking 只认 viking:// 绝对 URI）
    #   给了 --viking-base → 生成 viking://.../images/x.jpg（正确渲染）
    #   否则退回 --img-prefix（相对路径，OV 里显示不出图，仅作离线预览用）
    if args.viking_base:
        vb = args.viking_base if args.viking_base.endswith("/") else args.viking_base + "/"
        img_prefix = vb + "images/"
    else:
        img_prefix = args.img_prefix
    if not args.viking_base:
        print("[postprocess] ⚠️ 未给 --viking-base：图片将用相对路径，"
              "在 OpenViking 里不会渲染（只显示成链接文字）。"
              "入库前请补 --viking-base 重跑，或用 scripts/fix_img_paths.py 批量改写。",
              file=sys.stderr)

    page_md = page_md_from_blocks(page_blocks, img_prefix)

    # 全局统计（噪音计数 + 各类块数）
    stats = {"header": 0, "footer": 0, "page_number": 0, "other_noise": 0,
             "table": 0, "text": 0, "title": 0, "image": 0, "equation": 0,
             "table_empty": 0}
    for page in middle.get("pages", []):
        for b in page.get("blocks", []):
            t = b.get("type", "")
            if t in NOISE_TYPES:
                k = t if t in ("header", "footer", "page_number") else "other_noise"
                stats[k] = stats.get(k, 0) + 1
            elif t == "table":
                stats["table"] += 1
                txt, img = _flatten_content(b.get("content"))
                if not txt and not img:
                    stats["table_empty"] += 1
            elif t in ("title", "doc_title", "paragraph_title"):
                stats["title"] += 1
            elif t in ("image", "figure"):
                stats["image"] += 1
            elif t == "equation":
                stats["equation"] += 1
            else:
                stats["text"] += 1
    print(f"[postprocess] middle_json: {len(page_md)} 页；"
          f"噪音 header={stats['header']} footer={stats['footer']} "
          f"pageno={stats['page_number']}；表 {stats['table']} 图 {stats['image']}",
          flush=True)

    doc = pymupdf.open(args.pdf)
    total_pages = doc.page_count
    toc = build_toc_tree(doc)
    if not toc:
        # 无书签 PDF（实测 DL/T 645-2007：89 页、有文字层、书签 0 条）
        # 回退：用 MinerU 的 paragraph_title/doc_title 块构建章节树。
        # 这些块带 level，本身即天然的章节结构，无需人工加书签。
        print("[postprocess] PDF 无书签 → 回退到「按 MinerU 标题块分卷」", flush=True)
        toc = build_toc_from_titles(middle)
        if not toc:
            print("[postprocess] 标题块也为空，无法分卷。请人工切分。", file=sys.stderr)
            doc.close()
            return 3
        print(f"[postprocess] 由标题块构建出 {count_nodes(toc)} 个章节节点", flush=True)

    # 只覆盖实际解析到的页
    parsed_pages = sorted(page_md.keys())
    print(f"[postprocess] 已解析页范围: p.{parsed_pages[0]+1}–p.{parsed_pages[-1]+1}"
          f"（共 {len(parsed_pages)} 页）", flush=True)

    volumes = split_volumes(toc, page_md, args.limit, [], None, total_pages)
    # 只保留与「已解析页」有交集的卷，并把卷范围裁剪到实际解析区间
    lo, hi = parsed_pages[0] + 1, parsed_pages[-1] + 1
    trimmed: list[dict] = []
    for v in volumes:
        if v["end"] < lo or v["page"] > hi:
            continue
        nv = dict(v)
        nv["page"] = max(v["page"], lo)
        nv["end"] = min(v["end"], hi)
        trimmed.append(nv)
    volumes = enforce_limit(trimmed, page_md, args.limit)

    # carry-over 预计算：每卷首页上、自己标题之前的非标题块，
    # 归属于「上一个结束于本页或前一页的卷」。
    carry_for: list[list[dict]] = [[] for _ in volumes]
    page_consumed: dict[int, int] = {}
    for i in range(len(volumes)):
        v = volumes[i]
        pidx = v["page"] - 1
        blocks = page_blocks.get(pidx)
        if not blocks:
            continue
        before = _blocks_before_title(blocks, v["title"].split("（")[0])
        if not before:
            continue
        owner = -1
        for j in range(i - 1, -1, -1):
            if volumes[j]["end"] == v["page"] or volumes[j]["end"] == v["page"] - 1:
                owner = j
                break
        if owner < 0:
            continue
        consumed = page_consumed.get(pidx, 0)
        new_blocks = before[consumed:] if consumed < len(before) else []
        if new_blocks:
            carry_for[owner].extend(new_blocks)
            page_consumed[pidx] = consumed + len(new_blocks)
    print(f"[postprocess] 切分得 {len(volumes)} 卷（裁剪到 p.{lo}–p.{hi}）", flush=True)

    manifest = {
        "chip": args.chip, "manual": args.manual,
        "source_pdf": str(args.pdf), "total_pages": total_pages,
        "parsed_pages": {"from": lo, "to": hi, "count": len(parsed_pages)},
        "volume_limit": args.limit,
        "noise_filtered": stats,
        "volumes": [],
    }

    for i, v in enumerate(volumes):
        # 页内切分：同一页常含多个小节（实测 p.80 的 4.5.2 与 4.5.3），
        # 按整页取会让相邻两卷正文完全相同。用书签标题定位每卷在首页/末页的块边界。
        next_title = volumes[i + 1]["title"] if i + 1 < len(volumes) else None
        # 下一卷与本卷同页 → 用它的标题作为本卷的页内结束点
        end_title_in_page = None
        if next_title and i + 1 < len(volumes) and volumes[i + 1]["page"] == v["end"]:
            end_title_in_page = next_title

        parts: list[str] = []
        for p in range(v["page"] - 1, v["end"]):
            blocks = page_blocks.get(p)
            if not blocks:
                continue
            is_first = (p == v["page"] - 1)
            is_last = (p == v["end"] - 1)
            start_t = v["title"].split("（")[0] if is_first else None
            end_t = end_title_in_page if is_last else None
            sub = slice_page_blocks(blocks, start_t, end_t)
            if is_last and carry_for[i]:
                sub = sub + carry_for[i]
            md, _ = render_blocks(sub, img_prefix)
            if md:
                parts.append(md)
        body = "\n\n".join(parts).strip()
        header = (
            f"---\nchip: {args.chip}\nmanual: {args.manual}\n"
            f"pages: {v['page']}-{v['end']}\n---\n\n"
            f"# {v['title']}\n\n"
            f"> 原册页码：p.{v['page']}–{v['end']}\n\n"
        )
        content = header + body + "\n"
        encoded = content.encode("utf-8")
        if len(encoded) > args.limit:
            # 截断提示语本身也占字节，必须预留预算，否则会「截断后仍超限」
            # 实测踩过：60036 B > 60000 B，超的正是提示语的 36 字节。
            NOTE = "\n\n> ⚠️ 本卷超出单卷上限，已在完整行边界截断\n"
            budget = args.limit - len(NOTE.encode("utf-8"))
            safe = encoded[:budget].decode("utf-8", "ignore")
            cut_at = safe.rfind("\n")
            if cut_at > budget // 2:
                safe = safe[:cut_at]
            content = safe + NOTE
            # 兜底：仍超限则按字节硬切（去掉提示语也不能超过上限）
            if len(content.encode("utf-8")) > args.limit:
                content = encoded[:args.limit].decode("utf-8", "ignore")

        fname = f"{i:02d}_{sanitize(v['title'])}.md"
        (out_dir / fname).write_text(content, encoding="utf-8", newline="\n")
        manifest["volumes"].append({
            "file": fname, "title": v["title"],
            "page": v["page"], "end": v["end"],
            "bytes": len(content.encode("utf-8")), "path": v["path"],
        })

    # _manifest.json 必须是「卷数组」——verify_md.py / render_crops.py 直接按数组消费。
    # 元信息另放 _postprocess_meta.json，避免破坏 v1 既有的消费契约。
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest["volumes"], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "_postprocess_meta.json").write_text(
        json.dumps({k: v for k, v in manifest.items() if k != "volumes"},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# {args.chip} / {args.manual} 分卷索引", "",
        f"- 源 PDF：`{args.pdf}`",
        f"- 总页数：{total_pages}；已解析：p.{lo}–{hi}",
        f"- 分卷数：{len(volumes)}；单卷上限：{args.limit} B",
        f"- 噪音过滤：header {stats['header']} / footer {stats['footer']} / "
        f"page_number {stats['page_number']}",
        f"- 表格块：{stats['table']}；图片块：{stats['image']}",
        "", "| # | 文件 | 原册页码 | 字节 |", "|---|---|---|---:|",
    ]
    for i, v in enumerate(manifest["volumes"]):
        lines.append(f"| {i} | `{v['file']}` | p.{v['page']}–{v['end']} | {v['bytes']:,} |")
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (out_dir / "_postprocess_report.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    doc.close()
    over = [v for v in manifest["volumes"] if v["bytes"] > args.limit]
    print(f"POSTPROCESS_DONE volumes={len(volumes)} over_limit={len(over)} "
          f"noise_removed={stats['header']+stats['footer']+stats['page_number']}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
