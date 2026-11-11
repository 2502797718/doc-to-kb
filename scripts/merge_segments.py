#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""merge_segments.py — 把分段 MinerU 的 middle_json 合并成一份完整手册

为什么需要
----------
实测 RM_AT32F423（495 页）一次性解析会在 30 分钟后卡死（日志 96 分钟无新输出，
但进程 CPU 持续增长 → 判定为 VLM 陷入循环）。改成每段 165 页后，
每段都能在 3~17 分钟内完成。

分段产出各自的 `middle_json.json`（含 `pages[].page_idx`），
本脚本按 page_idx 排序合并，并合并 images 目录，产出一份可直接
喂给 postprocess.py 的完整目录。

用法
----
  python merge_segments.py --out <merged_dir> <seg1_dir> <seg2_dir> [seg3_dir ...]

产出
----
  <merged_dir>/raw/unpacked/middle_json.json   合并后的完整 middle_json
  <merged_dir>/raw/unpacked/images/            合并后的全部图片
  <merged_dir>/_segments.json                  分段来源与页数统计
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def find_middle(seg_dir: Path) -> Path | None:
    """在分段目录里定位 middle_json.json。"""
    direct = seg_dir / "raw" / "unpacked" / "middle_json.json"
    if direct.exists():
        return direct
    for p in seg_dir.rglob("middle_json.json"):
        return p
    return None


def find_images(seg_dir: Path) -> Path | None:
    """定位该段的 images 目录。"""
    d = seg_dir / "raw" / "unpacked" / "images"
    if d.is_dir():
        return d
    for p in seg_dir.rglob("images"):
        if p.is_dir():
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="合并分段 MinerU middle_json")
    ap.add_argument("--out", required=True, help="合并后的输出目录")
    ap.add_argument("segments", nargs="+", help="各分段目录")
    args = ap.parse_args()

    out = Path(args.out)
    merged_pages: dict[int, dict] = {}
    seg_stats: list[dict] = []
    img_sources: list[Path] = []

    for seg in args.segments:
        sd = Path(seg)
        mid_path = find_middle(sd)
        if not mid_path:
            print(f"[merge] 跳过（无 middle_json）: {sd}", file=sys.stderr)
            continue
        data = json.loads(mid_path.read_text(encoding="utf-8"))
        pages = data.get("pages") or []
        idxs = []
        for pg in pages:
            idx = pg.get("page_idx")
            if idx is None:
                continue
            if idx in merged_pages:
                print(f"[merge] 警告：page_idx {idx} 重复，后段覆盖前段（{sd.name}）",
                      file=sys.stderr)
            merged_pages[idx] = pg
            idxs.append(idx)
        img_dir = find_images(sd)
        if img_dir:
            img_sources.append(img_dir)
        seg_stats.append({
            "segment": str(sd),
            "middle_json": str(mid_path),
            "pages": len(pages),
            "page_idx_min": min(idxs) if idxs else None,
            "page_idx_max": max(idxs) if idxs else None,
            "images_dir": str(img_dir) if img_dir else "",
        })
        print(f"[merge] {sd.name}: {len(pages)} 页 (idx {min(idxs) if idxs else '-'}"
              f"–{max(idxs) if idxs else '-'})", flush=True)

    if not merged_pages:
        print("[merge] 没有可合并的分段", file=sys.stderr)
        return 2

    ordered = [merged_pages[k] for k in sorted(merged_pages)]
    out_unpacked = out / "raw" / "unpacked"
    out_unpacked.mkdir(parents=True, exist_ok=True)

    # 保留第一段的 metadata/schema，pages 用合并结果
    first_mid = Path(seg_stats[0]["middle_json"])
    base = json.loads(first_mid.read_text(encoding="utf-8"))
    base["pages"] = ordered
    base.pop("is_full_document", None)  # 分段时该项为 False，合并后整本是完整文档

    (out_unpacked / "middle_json.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")

    # 合并图片
    out_images = out_unpacked / "images"
    out_images.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for src in img_sources:
        for f in src.iterdir():
            if not f.is_file():
                continue
            dst = out_images / f.name
            if dst.exists():
                skipped += 1
                continue
            shutil.copy2(f, dst)
            copied += 1

    meta = {
        "segments": seg_stats,
        "merged_pages": len(ordered),
        "page_idx_min": min(merged_pages),
        "page_idx_max": max(merged_pages),
        "images_copied": copied,
        "images_skipped_duplicate": skipped,
    }
    (out / "_segments.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"MERGE_DONE pages={len(ordered)} idx={meta['page_idx_min']}-{meta['page_idx_max']} "
          f"images={copied}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
