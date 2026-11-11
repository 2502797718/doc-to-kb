#!/usr/bin/env python3
"""chip-manual-to-kb · 主脚本：PDF 手册 → 按书签分卷的 Markdown

=== 设计依据（全部来自 AT32F423 RM V2.04 实测，不是推测）===
1) 全书 UTF-8 文本 1.09MB / 699,435 字符，495 页，738 条书签（1级26/2级122/3级354/4级236）。
2) 按 1 级章切会产出 236KB 的 TIMER 章、187KB 的 OTGFS 章 —— 远超向量库 65,535B 硬限。
   按 2 级切仍有 6 块超 45KB。
   ⇒ 必须**按最深书签递归切分**，实测得 363 个叶子块，仅 1 块超限（p1~21 封面+目录）。
3) 表格：OV 的 PDFParser（pdfplumber）与 pymupdf find_tables 在本手册上都返回 0 张表；
   必须自行重建（见 table_rebuild.py）。
4) 与 AI 的分工：程序定位+切分+算置信度+渲染截图；AI 读图复核低置信度表。

=== 用法 ===
  # 1) 只勘察结构，不写文件
  python pdf_to_md.py --probe <pdf>

  # 2) 生成 md（默认按最深书签切分，单卷 <=60KB）
  python pdf_to_md.py --out <out_dir> <pdf>

  # 3) 指定芯片/手册版本目录名
  python pdf_to_md.py --out <out_dir> --chip at32f423 --manual RM_V2.04_CH <pdf>

产出:
  <out_dir>/<NN>_<章节名>.md        分卷正文
  <out_dir>/README.md              导航（章↔文件↔页码）
  <out_dir>/_review/manifest.json  待 AI 复核表格清单（由 render_crops.py 生成）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 单卷 UTF-8 字节上限（65,535 是向量库硬限，留 8% 余量）
VOLUME_LIMIT = 60000

# 图片：默认不导（实测本 PDF 有 25,806 个矢量碎片，导了会灌爆 media）
EXTRACT_IMAGES = False

# 文件名净化
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _ensure_deps():
    miss = []
    for m in ("pymupdf", "pdfplumber"):
        try:
            __import__(m)
        except Exception:
            miss.append(m)
    if miss:
        print("[FAIL] 缺依赖: %s" % " ".join(miss))
        print("       先跑 deps_check.py 并按提示安装。")
        sys.exit(1)


def sanitize(name: str, maxlen: int = 60) -> str:
    name = _BAD.sub("_", name).strip("_ ")
    name = re.sub(r"\s+", "_", name)
    return name[:maxlen] or "untitled"


# ---------------------------------------------------------------- 书签树


def build_toc_tree(doc) -> list:
    """把扁平书签表建成树"""
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


def page_bytes(doc, a: int, b: int) -> int:
    """[a, b) 页区间的 UTF-8 字节数"""
    total = 0
    for i in range(a - 1, min(b, doc.page_count)):
        total += len(doc[i].get_text().encode("utf-8"))
    return total


def flatten_with_pages(nodes: list, total_pages: int, parent_end: int = None) -> list:
    """给每个节点补 end 页。

    规则: 节点的 end = 下一个同级节点的起始页 - 1；
          若它是最后一个同级节点，则继承父节点的 end（父级范围）；
          顶层最后节点 = 全书末页。
    这是分卷正确性的关键：早期版本让末节点一律取全书末页，导致
    `11.7.11 发送寄存器` 被算成 p164-495（746KB），实际应为单页。
    """
    out = []
    for i, n in enumerate(nodes):
        if i + 1 < len(nodes):
            end = nodes[i + 1]["page"] - 1
        else:
            end = parent_end if parent_end is not None else total_pages
        out.append({**n, "end": max(n["page"], end)})
    return out


def split_volumes(doc, nodes: list, limit: int = VOLUME_LIMIT,
                  parent_path: list = None, parent_end: int = None) -> list:
    """递归按书签切分，直到每块 <= limit 字节"""
    parent_path = parent_path or []
    flat = flatten_with_pages(nodes, doc.page_count, parent_end)
    volumes = []
    for n in flat:
        b = page_bytes(doc, n["page"], n["end"] + 1)
        if b <= limit or not n["children"]:
            volumes.append({
                "title": n["title"], "level": n["level"],
                "page": n["page"], "end": n["end"], "bytes": b,
                "path": parent_path + [n["title"]],
            })
        else:
            sub = split_volumes(doc, n["children"], limit,
                                parent_path + [n["title"]], n["end"])
            if sub:
                # 补「父级引言卷」：父书签页到首个子书签页之间的内容，子卷覆盖不到。
                # 实测反例: 14 章书签在 p211、首个子书签 14.1 在 p212 →
                # p211 无人覆盖，正文丢失（verify_md.py 报「p.210 之后直接 p.212」）。
                first_sub_page = sub[0]["page"]
                if first_sub_page > n["page"]:
                    intro = {
                        "title": "%s（引言 p.%d）" % (n["title"], n["page"]),
                        "level": n["level"],
                        "page": n["page"], "end": first_sub_page - 1,
                        "path": parent_path + [n["title"]],
                        "bytes": page_bytes(doc, n["page"], first_sub_page),
                        "is_intro": True,
                    }
                    volumes.append(intro)
                volumes.extend(sub)
            else:
                volumes.append({
                    "title": n["title"], "level": n["level"],
                    "page": n["page"], "end": n["end"], "bytes": b,
                    "path": parent_path + [n["title"]],
                })
    return volumes


# ---------------------------------------------------------------- 表格


def rebuild_tables(doc, plumber_doc, page_no: int):
    """返回该页 TableBlock 列表；失败返回空列表（不阻断正文生成）"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import table_rebuild as tr
    except Exception as exc:  # noqa: BLE001
        print("[WARN] table_rebuild 不可用: %s" % exc)
        return [], None
    mp = doc[page_no - 1]
    pp = plumber_doc.pages[page_no - 1]
    try:
        tabs = tr.rebuild_tables_on_page(pp, mp, page_no)
    finally:
        try:
            pp.close()
        except Exception:
            pass
    return tabs, tr


# ---------------------------------------------------------------- 正文渲染


def find_title_y(page, title: str) -> float:
    """在页内定位小节标题的 y 坐标（用于同页多小节的内容切分）。

    为什么需要: PDF 书签只记录**页码**，不含页内 y。实测 I2C 章 11.1/11.2/11.3
    三个书签都指向 p133，若不切分会导致同一页内容被 3 个卷重复输出
    （实测 11.1 与 11.2 两卷内容完全一致，知识库中同一内容被索引多次）。

    定位策略: 标题字符串归一化（去空白/全角空格）后逐行匹配，取首个命中行的 y。
    匹配失败返回 -1（该小节按整页输出，并在卷头标注）。
    """
    norm_target = re.sub(r"\s+", "", title)
    if not norm_target:
        return -1.0
    best = -1.0
    for blk in page.get_text("dict").get("blocks", []):
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            txt = "".join(s.get("text", "") for s in ln.get("spans", []))
            if not txt.strip():
                continue
            if re.sub(r"\s+", "", txt) == norm_target:
                return float(ln["bbox"][1])
            # 前缀匹配兜底（标题后面可能紧跟其他内容）
            if best < 0 and re.sub(r"\s+", "", txt).startswith(norm_target):
                best = float(ln["bbox"][1])
    return best


def split_y_range(doc, plumber_doc, vol: dict, all_vols: list) -> tuple:
    """计算该卷在「起始页」上的有效 y 区间，避免同页内容重复输出。

    返回 (y_start, y_end)；整页有效时返回 (0, 页高)。

    规则:
      · 起始页若有多卷共享 → 本卷从自己的标题 y 开始，到同页下一个卷的标题 y 为止；
      · 起始页只有本卷 → 从 0 开始；
      · 结束页若被后续卷共享 → 本卷到自己的内容末尾即可（由 _in_table/页码逻辑处理）。
    """
    page = doc[vol["page"] - 1]
    page_h = page.rect.height

    # 同一页上的所有卷（按起始顺序）
    same_page = [v for v in all_vols if v["page"] == vol["page"]]
    same_page.sort(key=lambda v: v.get("_order", 0))

    y_start = 0.0
    if len(same_page) > 1:
        ty = find_title_y(page, vol["title"])
        if ty >= 0:
            y_start = ty - 4.0

    y_end = page_h
    idx = next((i for i, v in enumerate(same_page) if v is vol), -1)
    if idx >= 0 and idx + 1 < len(same_page):
        ty_next = find_title_y(page, same_page[idx + 1]["title"])
        if ty_next >= 0:
            y_end = ty_next - 2.0

    return y_start, y_end


def render_volume(doc, plumber_doc, vol: dict, with_tables: bool = True,
                  all_vols: list = None) -> str:
    """渲染单卷 Markdown

    两个「不重复」原则:
      ① 表格区域**不重复输出正文** —— 取 TableBlock 的 y 区间，用 get_text("blocks")
         逐块过滤掉与表格区间重叠的块。早期版本既输出整页文字又输出重建表，
         实测 55 卷里 9 卷因此超限（最大 78.7KB）。
      ② 同页多小节**不重复输出** —— 实测 I2C 章 11.1/11.2/11.3 三个书签都指向
         p133，早期版本三个卷各输出整页 p133，内容完全重复。
         现按标题文字 y 定位页内切分点（见 split_y_range）。
    """
    lines = []
    lines.append("---")
    lines.append('source: "%s"' % vol["source_name"])
    lines.append('section: "%s"' % vol["title"].replace('"', "'"))
    lines.append("pages: %d-%d" % (vol["page"], vol["end"]))
    lines.append('bookmark_path: "%s"' % " > ".join(vol["path"]).replace('"', "'"))
    lines.append("---")
    lines.append("")
    lines.append("# %s" % vol["title"])
    lines.append("")
    lines.append("> 原册页码：p.%d–%d" % (vol["page"], vol["end"]))
    lines.append("")

    tr_mod = None
    bf_mod = None
    for pno in range(vol["page"], vol["end"] + 1):
        page = doc[pno - 1]
        tabs = []
        if with_tables:
            tabs, tr_mod = rebuild_tables(doc, plumber_doc, pno)

        # 寄存器位域表：独立解析器（2,279 条位域行是手册主体内容）
        bf_tables = []
        if with_tables:
            try:
                if bf_mod is None:
                    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    import bitfield_parse as bf_mod
                bf_tables = bf_mod.parse_bitfield_tables(
                    plumber_doc.pages[pno - 1], pno)
            except Exception as exc:  # noqa: BLE001
                print("[WARN] bitfield_parse 失败 p.%d: %s" % (pno, exc))
                bf_tables = []

        # 起始页的 y 区间（同页多小节时按标题切分）
        y_lo, y_hi = 0.0, page.rect.height
        if all_vols is not None and pno == vol["page"]:
            y_lo, y_hi = split_y_range(doc, plumber_doc, vol, all_vols)

        # 表格占用的 y 区间（含少量余量）；位域表也要剔除，避免与正文重复
        spans = [(t.y0 - 2.0, t.y1 + 2.0) for t in tabs]
        spans += [(t.y0 - 2.0, t.y1 + 2.0) for t in bf_tables]

        def _in_table(y0: float, y1: float) -> bool:
            for (a, b) in spans:
                if y0 < b and y1 > a:
                    return True
            return False

        # 逐块输出，跳过表格区与页外区间
        for blk in page.get_text("blocks"):
            x0, y0, x1, y1, text = blk[0], blk[1], blk[2], blk[3], blk[4]
            if not (text or "").strip():
                continue
            if _in_table(y0, y1):
                continue
            # 块中心落在本卷的 y 区间内才输出
            cy = (y0 + y1) / 2.0
            if pno == vol["page"] and not (y_lo <= cy <= y_hi):
                continue
            lines.append(text.strip())
            lines.append("")

        for tb in tabs:
            # 表格块也受同页切分约束：块中心落在本卷 y 区间内才输出
            if pno == vol["page"]:
                tcy = (tb.y0 + tb.y1) / 2.0
                if not (y_lo <= tcy <= y_hi):
                    continue
            md = tr_mod.to_markdown(tb) if tr_mod else ""
            if md.strip():
                lines.append(md)
                lines.append("")

        # 输出寄存器位域表（结构化 Markdown 表）
        for bft in bf_tables:
            if pno == vol["page"]:
                tcy = (bft.y0 + bft.y1) / 2.0
                if not (y_lo <= tcy <= y_hi):
                    continue
            md = bf_mod.to_markdown(bft) if bf_mod else ""
            if md.strip():
                lines.append(md)
                lines.append("")
    return "\n".join(lines)


def probe(pdf_path: str) -> int:
    _ensure_deps()
    import pymupdf

    doc = pymupdf.open(pdf_path)
    print("=" * 70)
    print("结构勘察：%s" % os.path.basename(pdf_path))
    print("=" * 70)
    print("页数        : %d" % doc.page_count)

    total_bytes = sum(len(doc[i].get_text().encode("utf-8")) for i in range(doc.page_count))
    print("文本 UTF-8  : %.2f MB (%d 字节)" % (total_bytes / 1024 / 1024, total_bytes))

    toc = doc.get_toc()
    print("书签条数    : %d" % len(toc))
    from collections import Counter
    lv = Counter(t[0] for t in toc)
    print("书签层级    : %s" % dict(sorted(lv.items())))

    tree = build_toc_tree(doc)
    vols = split_volumes(doc, tree)
    print("分卷数      : %d（上限 %dB）" % (len(vols), VOLUME_LIMIT))
    over = [v for v in vols if v["bytes"] > VOLUME_LIMIT]
    print("超限卷      : %d" % len(over))
    for v in over[:5]:
        print("    %7dB p%3d-%3d %s"
              % (v["bytes"], v["page"], v["end"], v["title"][:40]))

    sizes = sorted(v["bytes"] for v in vols)
    if sizes:
        print("体积分布    : min=%d 中位=%d max=%d" %
              (sizes[0], sizes[len(sizes) // 2], sizes[-1]))

    # 栅格网格页（决定哪些表能被精确重建）
    grid_pages = []
    for pno in range(1, doc.page_count + 1):
        tall = 0
        for im in doc[pno - 1].get_image_info():
            x0, y0, x1, y1 = im["bbox"]
            if (x1 - x0) < 2.0 and (y1 - y0) >= 2.0:
                tall += 1
        if tall >= 50:
            grid_pages.append(pno)
    print("栅格网格页  : %d 页（这些页的表格列边界可精确还原）" % len(grid_pages))
    if grid_pages:
        print("    %s%s" % (grid_pages[:20], " ..." if len(grid_pages) > 20 else ""))
    print("无网格页    : %d 页（表格走 rule 路径，需 AI 复核比例更高）"
          % (doc.page_count - len(grid_pages)))
    print()
    print("结论：可安全分卷生成。表格需复核的页 = 无网格页中含表的页。")
    return 0


def split_by_pages(doc, vol: dict, limit: int, src_name: str,
                   plumber_doc, with_tables: bool) -> list:
    """叶子卷超限时按**页区间**硬切（书签已无法再细分）。

    为什么需要这条兜底: 按书签递归切到叶子后，若单页内容本身就超限（如大寄存器表
    页），继续收紧书签切分上限只会增加卷数而不减小单卷体积（实测迭代 4 轮从 6 卷
    超限涨到 185 卷仍超限）。此时唯一正确的做法是按页切。
    """
    parts = []
    start = vol["page"]
    while start <= vol["end"]:
        # 贪心：尽量多装几页
        end = start
        best = None
        for cand in range(start, vol["end"] + 1):
            trial = dict(vol)
            trial["page"] = start
            trial["end"] = cand
            trial["source_name"] = src_name
            md = render_volume(doc, plumber_doc, trial, with_tables=with_tables)
            if len(md.encode("utf-8")) <= limit:
                best = cand
            else:
                break
        if best is None:
            best = start          # 单页仍超限：只能单独成卷并告警
        parts.append({
            "title": "%s（p.%d–%d）" % (vol["title"], start, best)
            if best > start else "%s（p.%d）" % (vol["title"], start),
            "level": vol["level"], "page": start, "end": best,
            "path": vol["path"], "source_name": src_name,
        })
        start = best + 1
    return parts


def main():
    ap = argparse.ArgumentParser(description="PDF 手册 → 分卷 Markdown")
    ap.add_argument("pdf", help="PDF 路径")
    ap.add_argument("--out", help="产出目录（不传则只勘察）")
    ap.add_argument("--probe", action="store_true", help="只勘察结构，不写文件")
    ap.add_argument("--chip", default="", help="芯片名（如 at32f423）")
    ap.add_argument("--manual", default="", help="手册标识（如 RM_V2.04_CH）")
    ap.add_argument("--limit", type=int, default=VOLUME_LIMIT,
                    help="单卷字节上限（默认 %d）" % VOLUME_LIMIT)
    ap.add_argument("--no-tables", action="store_true", help="跳过表格重建")
    args = ap.parse_args()

    _ensure_deps()
    import pymupdf
    import pdfplumber

    if args.probe or not args.out:
        return probe(args.pdf)

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    doc = pymupdf.open(args.pdf)
    tree = build_toc_tree(doc)
    if not tree:
        print("[FAIL] 该 PDF 无书签，无法按章节分卷。请改用其他切分策略或人工切分。")
        return 2

    # 分卷策略：书签递归切分 → 渲染 → 超限卷按页区间硬切（只做一轮，不迭代）
    # 为什么不做迭代: 收紧上限重跑只会把叶子块切得更多而不减小单卷体积
    # （实测 4 轮迭代从 6 卷超限涨到 185 卷仍超限）。
    limit = args.limit
    src_name = os.path.basename(args.pdf)
    vols = split_volumes(doc, tree, limit)
    # 卷序：同页多小节切分时用于判定「下一个卷是谁」
    for i, v in enumerate(vols):
        v["_order"] = i

    with pdfplumber.open(args.pdf) as pl:
        # 第一轮：书签切分
        rendered = []
        overflow = []
        for v in vols:
            v["source_name"] = src_name
            md = render_volume(doc, pl, v, with_tables=not args.no_tables,
                               all_vols=vols)
            size = len(md.encode("utf-8"))
            if size > limit:
                overflow.append(v)
            else:
                rendered.append((v, md, size))

        # 第二轮：超限叶子卷按页硬切
        if overflow:
            print("[INFO] %d 卷超限，按页区间硬切" % len(overflow))
            for v in overflow:
                for part in split_by_pages(doc, v, limit, src_name,
                                           pl, not args.no_tables):
                    part["_order"] = v.get("_order", 0)
                    md = render_volume(doc, pl, part, with_tables=not args.no_tables,
                                       all_vols=vols)
                    rendered.append((part, md, len(md.encode("utf-8"))))
            # 保持页码顺序
            rendered.sort(key=lambda t: (t[0]["page"], t[0].get("_order", 0)))

        manifest = []
        for idx, (v, md, size) in enumerate(rendered, 1):
            fname = "%02d_%s.md" % (idx, sanitize(v["title"]))
            with open(os.path.join(out_dir, fname), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(md)
            manifest.append({
                "index": idx, "file": fname, "title": v["title"],
                "page": v["page"], "end": v["end"],
                "bytes": size, "path": v.get("path", [v["title"]]),
                "needs_agent_tables": md.count("TABLE_NEEDS_AGENT"),
                "suspect_tables": md.count("TABLE_SUSPECT"),
            })

    # 导航 README
    chip = args.chip or os.path.splitext(src_name)[0]
    manual = args.manual or ""
    readme = []
    readme.append("# %s %s 手册转写\n" % (chip, manual))
    readme.append("> 来源：`%s`（由 chip-manual-to-kb skill 转写）\n" % src_name)
    readme.append("> 章节 ↔ 文件 ↔ 原册页码对照；**所有页码均为原册页码**。\n")
    readme.append("\n## 分卷清单\n")
    readme.append("| # | 文件 | 章节 | 原册页 | 体积(B) | 待复核表 | 疑表(未出表) |")
    readme.append("|---|---|---|---|---|---|---|")
    for m in manifest:
        readme.append("| %d | [%s](%s) | %s | p.%d–%d | %d | %d | %d |"
                      % (m["index"], m["file"], m["file"], m["title"],
                         m["page"], m["end"], m["bytes"],
                         m["needs_agent_tables"], m["suspect_tables"]))
    total_agent = sum(m["needs_agent_tables"] for m in manifest)
    total_suspect = sum(m["suspect_tables"] for m in manifest)
    readme.append("\n**合计**：%d 卷，%d 字节；待复核表 **%d** 张，疑表未出表 **%d** 处。"
                  % (len(manifest), sum(m["bytes"] for m in manifest),
                     total_agent, total_suspect))
    readme.append("\n## 未自动重建的表格怎么处理\n")
    readme.append("1. 跑 `render_crops.py` 生成表格截图（`_review/*.png`）；")
    readme.append("2. 由 agent（具备视觉能力）读图重写为标准 Markdown 表；")
    readme.append("3. 删除原文中的 `TABLE_NEEDS_AGENT` / `TABLE_SUSPECT` 标记块；")
    readme.append("4. 重跑 `verify_md.py` 确认待复核数归零。")
    with open(os.path.join(out_dir, "README.md"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(readme))

    with open(os.path.join(out_dir, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print("GENERATE_DONE volumes=%d bytes=%d needs_agent=%d suspect=%d dir=%s"
          % (len(manifest), sum(m["bytes"] for m in manifest),
             total_agent, total_suspect, out_dir))
    over = [m for m in manifest if m["bytes"] > args.limit]
    if over:
        print("[WARN] %d 卷超限：" % len(over))
        for m in over[:5]:
            print("   %s %dB" % (m["file"], m["bytes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
