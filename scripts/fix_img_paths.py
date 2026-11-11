#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_img_paths.py — 把 md 里的相对图片路径改成 OpenViking 绝对 URI

背景（2026-11-01 实测）：
OpenViking Studio 的 Markdown 渲染器**只认 `viking://` 绝对 URI**，
相对路径（`images/x.jpg`、`../images/x.jpg`）一律渲染成普通链接文字，图片显示不出来。

实测四种写法（同一文件、同一张图）：
  写法A  images/page_28_image_10.jpg              → ❌ 显示成 [page_28_image_10.jpg]
  写法B  images/images/page_28_image_10.jpg       → ❌ 同上（且多一层）
  写法C  viking://resources/.../images/x.jpg      → ✅ 正常渲染
  写法D  ../images/page_28_image_10.jpg           → ❌ 同上

另外 MinerU 的 `image_path` 自带 `images/` 前缀，旧脚本又加了一次，
导致出现 `images/images/` 双层。本脚本一并修掉。

用法：
  python fix_img_paths.py --dir <手册目录> --uri <该手册 images 的 viking URI 前缀>
  例：python fix_img_paths.py --dir .../RM_V2.04_CH \
        --uri viking://resources/shared/chips/AT32F423/RM_V2.04_CH/images/

默认 --dry-run 只统计不改；加 --apply 才落盘。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 匹配 ](images/xxx ) 或 ](images/images/xxx )
REL_IMG = re.compile(r"\]\(images/(?:images/)?([^)]+)\)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="手册目录（含 md）")
    ap.add_argument("--uri", required=True,
                    help="该手册 images 的 viking URI 前缀，末尾带 /")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只统计）")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[fix] 目录不存在: {root}", file=sys.stderr)
        return 2

    uri = args.uri if args.uri.endswith("/") else args.uri + "/"

    md_files = [p for p in root.rglob("*.md") if not p.name.startswith(".")]
    total_hits = 0
    changed_files = 0
    samples: list[str] = []

    for p in md_files:
        txt = p.read_text(encoding="utf-8")
        new, n = REL_IMG.subn(lambda m: "](" + uri + m.group(1) + ")", txt)
        if n == 0:
            continue
        total_hits += n
        changed_files += 1
        if len(samples) < 3:
            m = REL_IMG.search(txt)
            if m:
                samples.append(f"    {p.name}: {m.group(0)}  →  ]({uri}{m.group(1)})")
        if args.apply:
            p.write_text(new, encoding="utf-8", newline="\n")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {root.name}: {changed_files} 文件 / {total_hits} 处引用")
    for s in samples:
        print(s)
    if not args.apply:
        print("  （未写盘；确认后加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
