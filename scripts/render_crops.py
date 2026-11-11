#!/usr/bin/env python3
"""chip-manual-to-kb · 表格区域截图（供 AI 读图复核）

=== 为什么需要本步骤 ===
程序能定位表格、切网格、算置信度，但「哪个 MUX 列对应哪个功能」需要看图判语义。
所以程序在没把握的地方**不硬猜**，改为留下标记 + 渲染 300dpi 截图，交 AI 复核。
本脚本负责把标记变成截图。

=== 两种用法 ===
  # A. 从产出目录自动找标记（完整流程用这个）
  python render_crops.py --pdf <pdf> --out <out_dir> --from-md <out_dir>

  # B. 指定页码（调试用）
  python render_crops.py --pdf <pdf> --out <out_dir> --pages 95,96,47

产出（都在 <out_dir>/_review/）:
  pNNN_tYY.png        表格区域截图（300dpi）
  manifest.json       待复核清单（page / bbox / png / 标记类型 / 原始文字）
  manifest.md         人读版清单
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ---- 截图参数
CROP_DPI = 300          # 寄存器小字需 300dpi 才可读
CROP_PAD = 6.0          # 四周留白（pt），避免切掉线框/边缘字
CROP_X0 = 55.0          # 正文列区左界（pt）
CROP_X1 = 565.0         # 正文列区右界（pt）
MAX_CROP_HEIGHT = 1200  # 单张 PNG 像素高上限，超了纵向分段（防 AI 读图被压缩到看不清）

# ---- 正文区上界（剔除页眉）
BODY_TOP = 58.0

# ---- 表头向上搜索范围（pt）：区域顶部被切掉表头时向上找真表头
# 实测 p101 表头在 y=144.6 而区域 y0=178（相差 33pt）；p173 类似。
HEADER_SEARCH_UP = 70.0

# ---- 标记解析
_AGENT = re.compile(r"<!--\s*TABLE_NEEDS_AGENT:\s*(.*?)-->", re.S)
_SUSPECT = re.compile(r"<!--\s*TABLE_SUSPECT:\s*(.*?)-->", re.S)
_KV = re.compile(r"(\w+)=([^\s]+)")


def _ensure_deps():
    miss = []
    for m in ("pymupdf", "pdfplumber"):
        try:
            __import__(m)
        except Exception:
            miss.append(m)
    if miss:
        print("[FAIL] 缺依赖: %s" % " ".join(miss))
        print("       先跑 deps_check.py 并按提示安装，不要跳过自检。")
        sys.exit(1)


def _find_header_top(page, y0: float, x0: float, x1: float,
                     search_up: float = HEADER_SEARCH_UP) -> float:
    """向上搜索表头行，返回应包含表头的截图起点 y。

    == 为什么必须这么做（2026-10-29 AI 核验发现的缺陷）==
    TableBlock 的 y0 是**表格数据区**的起点，而表头行常落在它之上：
      · p101 表头「寄存器简称/基址偏移量/复位值」在 y=144.6，
        而区域 y0=178 → 截图从 178 开始，表头被切掉；
      · p173 表头「序号/Kbps/实际…」同样被切。
    后果: AI 拿到截图却看不到列名，只能靠内容猜列，复核效果大打折扣。

    做法: 在 [y0 - search_up, y0] 范围内找文字行，取**最靠近区域顶部**
    且**词数 >= 2** 的那一行作为表头，把截图起点提到它上方 4pt。
    找不到（区域内无多词行）则原样返回 y0。
    """
    lo = max(BODY_TOP, y0 - search_up)
    rows: dict = {}
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            spans = ln.get("spans", [])
            txt = "".join(s.get("text", "") for s in spans).strip()
            if not txt:
                continue
            bx0, by0, bx1, by1 = ln["bbox"]
            cy = (by0 + by1) / 2.0
            if not (lo <= cy < y0):
                continue
            # 只考虑落在截图横向范围内的行
            if bx1 < x0 or bx0 > x1:
                continue
            key = round(cy / 3)
            rows.setdefault(key, []).append((cy, txt, bx0, bx1))

    if not rows:
        return y0

    # 从下往上收集所有「多词行」，再选出最上面的**列名行**。
    # == 为什么要继续往上找（2026-10-29 二次修正）==
    # 只取「最靠近区域顶部」的多词行是不够的：p101 区域 y0=178，
    # 向下第一个多词行是 y=163.8 的**首条数据行**（GPIOA_CFGR / 0x00 / 0xA800 0000），
    # 而真正的列名行「寄存器简称 / 基址偏移量 / 复位值」在 y=144.6 更靠上。
    # 判据: 列名行由**短词**构成（无 0x 十六进制、无长标识符），
    #       数据行含 0x 数值或长标识符。
    candidates = []
    for key in sorted(rows, reverse=True):
        items = rows[key]
        items.sort(key=lambda t: t[2])
        words = [t[1] for t in items]
        if len(words) < 2:
            continue
        top_y = min(t[0] for t in items)
        # 该行是否像「列名行」：无 0x 数值，且词都较短
        looks_like_header = (
            not any(w.startswith("0x") or w.startswith("0X") for w in words)
            and max(len(w) for w in words) <= 12
        )
        candidates.append((top_y, looks_like_header))

    # 优先取「像列名行」的最上面那行；没有则退回最靠近区域的那行
    header_candidates = [c for c in candidates if c[1]]
    if header_candidates:
        return max(BODY_TOP, min(c[0] for c in header_candidates) - 4.0)
    return max(BODY_TOP, candidates[0][0] - 4.0) if candidates else y0


def render_crop(doc, page_no: int, bbox, out_path: str, dpi: int = CROP_DPI,
                include_header: bool = True) -> list:
    """按 bbox(pt) 渲染 PNG；过高时纵向分段，返回实际写出的路径列表

    include_header=True（默认）时，会把 bbox 上方的表头行纳入截图范围，
    避免「有截图但看不到列名」的复核障碍。
    """
    page = doc[page_no - 1]
    x0, y0, x1, y1 = bbox
    x0 = max(0.0, x0 - CROP_PAD)
    x1 = min(page.rect.width, x1 + CROP_PAD)

    if include_header:
        y0 = _find_header_top(page, y0, x0, x1)
    y0 = max(0.0, y0 - CROP_PAD)
    y1 = min(page.rect.height, y1 + CROP_PAD)
    if x1 <= x0 or y1 <= y0:
        return []

    written = []
    scale = dpi / 72.0
    h_px = (y1 - y0) * scale
    if h_px <= MAX_CROP_HEIGHT:
        segs = [(y0, y1)]
    else:
        n = int(h_px // MAX_CROP_HEIGHT) + 1
        step = (y1 - y0) / n
        # 分段间重叠 4pt，保证不漏行
        segs = [(y0 + i * step, min(y1, y0 + (i + 1) * step + 4)) for i in range(n)]

    for i, (sy0, sy1) in enumerate(segs):
        clip = pymupdf.Rect(x0, sy0, x1, sy1)   # Rect 顺序必须是 (x0, y0, x1, y1)
        pix = page.get_pixmap(clip=clip, dpi=dpi)
        path = out_path if len(segs) == 1 else out_path.replace(".png", "_%d.png" % i)
        pix.save(path)
        written.append(path)
    return written


def parse_markers(out_dir: str) -> list:
    """从产出目录的 md 文件里解析待复核标记"""
    entries = []
    for fname in sorted(os.listdir(out_dir)):
        if not fname.endswith(".md") or fname == "README.md":
            continue
        path = os.path.join(out_dir, fname)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for kind, rx in (("needs_agent", _AGENT), ("suspect", _SUSPECT)):
            for m in rx.finditer(text):
                kv = dict(_KV.findall(m.group(1)))
                try:
                    page = int(kv["page"])
                    y0 = float(kv["y0"])
                    y1 = float(kv["y1"])
                except (KeyError, ValueError):
                    continue
                entries.append({
                    "kind": kind,
                    "volume": fname,
                    "page": page,
                    "bbox": [float(kv.get("x0", CROP_X0)), y0,
                             float(kv.get("x1", CROP_X1)), y1],
                    "confidence": float(kv.get("confidence", 0) or 0),
                    "rows": int(kv.get("rows", 0) or 0),
                    "cols": int(kv.get("cols", 0) or 0),
                    "mode": kv.get("mode", ""),
                    "reasons": kv.get("reasons", ""),
                })
    return entries


def main():
    ap = argparse.ArgumentParser(description="渲染表格区域截图供 AI 解析")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True, help="产出根目录（截图落到 <out>/_review/）")
    ap.add_argument("--from-md", help="从 <out> 目录的 md 标记自动生成（完整流程用）")
    ap.add_argument("--pages", help="逗号分隔页码（调试用，需配 table_rebuild）")
    ap.add_argument("--dpi", type=int, default=CROP_DPI)
    args = ap.parse_args()

    _ensure_deps()
    global pymupdf
    import pymupdf

    review_dir = os.path.join(args.out, "_review")
    os.makedirs(review_dir, exist_ok=True)
    doc = pymupdf.open(args.pdf)

    if args.from_md:
        entries = parse_markers(args.from_md)
        if not entries:
            print("CROPS_DONE count=0（无待复核标记，全部表格已自动重建通过）")
            _write_manifest(review_dir, [])
            return 0
    elif args.pages:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import table_rebuild as tr
        import pdfplumber

        entries = []
        with pdfplumber.open(args.pdf) as pl:
            for pno in [int(p) for p in args.pages.split(",") if p.strip()]:
                mp, pp = doc[pno - 1], pl.pages[pno - 1]
                for idx, tb in enumerate(tr.rebuild_tables_on_page(pp, mp, pno)):
                    if tb.accepted:
                        continue
                    entries.append({
                        "kind": "needs_agent" if tb.mode == "grid" else "suspect",
                        "volume": "-", "page": pno,
                        "bbox": [tb.x0, tb.y0, tb.x1, tb.y1],
                        "confidence": tb.confidence,
                        "rows": tb.n_rows, "cols": tb.n_cols,
                        "mode": tb.mode,
                        "reasons": ",".join(tb.reasons),
                    })
                pp.close()
    else:
        print("[FAIL] 需要 --from-md 或 --pages")
        return 2

    # 渲染
    manifest = []
    for e in entries:
        name = "p%03d_t%d_%s.png" % (e["page"], int(round(e["bbox"][1])), e["kind"][:4])
        pngs = render_crop(doc, e["page"], tuple(e["bbox"]),
                           os.path.join(review_dir, name), args.dpi)
        if not pngs:
            print("[WARN] p.%d bbox 无效，跳过: %s" % (e["page"], e["bbox"]))
            continue
        e["png"] = os.path.relpath(pngs[0], review_dir).replace("\\", "/")
        e["png_parts"] = [os.path.relpath(p, review_dir).replace("\\", "/") for p in pngs]
        manifest.append(e)

    _write_manifest(review_dir, manifest)
    print("CROPS_DONE count=%d dir=%s" % (len(manifest), review_dir))
    return 0


def _write_manifest(review_dir: str, manifest: list) -> None:
    with open(os.path.join(review_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    md = []
    md.append("# 待 AI 复核表格清单\n")
    md.append("共 **%d** 张。\n" % len(manifest))
    md.append("## 处理要求\n")
    md.append("逐条读 PNG → 重写为标准 Markdown 表 → 删除原文中的对应标记块。\n")
    md.append("- 列数与截图表头一致，不增删列；")
    md.append("- 空单元格留空，不填「-」，不猜测；")
    md.append("- 单元格内折行文字合并为一行；")
    md.append("- 表后补 `> 来源：原册 p.NNN`。\n")
    md.append("## 清单\n")
    md.append("| # | 类型 | 原册页 | 所属卷 | 截图 | 置信度 | 未达标原因 |")
    md.append("|---|---|---|---|---|---|---|")
    for i, e in enumerate(manifest, 1):
        md.append("| %d | %s | p.%d | `%s` | `%s` | %.2f | `%s` |"
                  % (i, e["kind"], e["page"], e["volume"],
                     e["png"], e["confidence"], e["reasons"] or "-"))
    with open(os.path.join(review_dir, "manifest.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(md))


if __name__ == "__main__":
    sys.exit(main())
