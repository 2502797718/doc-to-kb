#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_iec_img_links.py — 把 IEC 标准里的「注释式图片引用」改成真 Markdown 图片

现状（2026-11-01 实测 IEC 61000-4-7）：
  引用写成：  > 原图见原册 p.30（images/page_29_chart_13.jpg）
  这是**引用块注释**，不是图片语法 → 渲染器不显示图片，AI 也难判断「这里有图」。

目标：
  ![图 B.1 频带示意图](viking://resources/shared/specs/IEC-61000-4-7/images/page_29_chart_13.jpg)

alt 文本来源优先级：
  1. 紧邻上文里最近的 `### Figure X.Y – ...` / `Figure ...` 标题
  2. 回退成 `原册 p.N 图`

用法：
  python fix_iec_img_links.py --dir <目录> --uri <images 的 viking URI 前缀> [--apply]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# > 原图见原册 p.30（images/page_29_chart_13.jpg）
PAT = re.compile(r"^>\s*原图见原册\s*p\.(\d+)（images/([^）]+)）\s*$", re.M)
# 上文里的 Figure 标题。实测格式有两种：
#   "### Figure B.1 – Illustration of frequency bands ..."
#   "Figure 8 - ..."（正文里）
FIG = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?((?:Figure|Fig\.)\s+[A-Z]?\.?\d+[^\n]{0,140})",
    re.I,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--uri", required=True, help="images 的 viking URI 前缀，末尾带 /")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[iec] 目录不存在: {root}", file=sys.stderr)
        return 2
    uri = args.uri if args.uri.endswith("/") else args.uri + "/"

    total = 0
    changed = 0
    samples: list[str] = []

    for p in root.rglob("*.md"):
        if p.name.startswith("."):
            continue
        txt = p.read_text(encoding="utf-8")
        if not PAT.search(txt):
            continue

        def repl(m: re.Match) -> str:
            page, fname = m.group(1), m.group(2)
            # 只在**紧邻上文 6 行内**找 Figure 标题，避免把前面很远的一张图的
            # 标题套到本图上（实测踩过：B.3 里 4 张图全被套成 "Figure B.1"）。
            before = txt[:m.start()]
            tail = "\n".join(before.rstrip().splitlines()[-6:])
            figs = FIG.findall(tail)
            if figs:
                alt = re.sub(r"\s+", " ", figs[-1].strip().lstrip("#")).strip()[:110]
            else:
                alt = f"原册 p.{page} 图"
            return f"![{alt}]({uri}{fname})"

        new, n = PAT.subn(repl, txt)
        if n == 0:
            continue
        total += n
        changed += 1
        if len(samples) < 3:
            m = PAT.search(txt)
            before = txt[:m.start()]
            figs = FIG.findall(before)
            alt = figs[-1].strip().lstrip("#").strip() if figs else f"原册 p.{m.group(1)} 图"
            samples.append(f"    {p.name}\n      → ![{(re.sub(chr(92)+'s+', ' ', alt))[:70]}]({uri}{m.group(2)})")
        if args.apply:
            p.write_text(new, encoding="utf-8", newline="\n")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {root.name}: {changed} 文件 / {total} 处引用")
    for s in samples:
        print(s)
    if not args.apply:
        print("  （未写盘；确认后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
