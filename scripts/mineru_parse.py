#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mineru_parse.py — 用 MinerU 把 PDF 解析成 Markdown（本流程主力路径）

为什么有这一步
--------------
v1 用 pymupdf + pdfplumber 自研几何重建表格，实测问题：
  · RN7326E 手册 1513 张表中 291 张（19%）列错位，真实信息挤在少数列里；
  · 页眉色条与 logo 被当图片抽出，761 张图里 634 张（83%）是装饰图。
MinerU 4.1.3 的 VLM 端到端解析直接给出结构化 Markdown：
  · 表格为 HTML/pipe 表，带 rowspan/colspan；
  · 公式转 LaTeX；
  · 版面元素有类型标记（title/text/table/image/...）。

本脚本只负责「调用 + 规范输出」，不做分卷（分卷交给 postprocess.py）。

用法
----
  python mineru_parse.py --pdf <pdf> --out <out_dir> [--pages all] [--tier standard]

产出
----
  <out_dir>/raw/            MinerU 原始产物（md + images + middle_json）
  <out_dir>/raw/*.md        合并后的 Markdown（含页标记，供 postprocess 分卷）

环境
----
  mineru-kit 可执行文件路径按以下顺序查找：
    1) --mineru-kit 参数
    2) 环境变量 MINERU_KIT
    3) PATH
    4) 常见 venv 位置兜底
  MINERU_HOME 若已设置会被原样透传（用于复用另一套模型目录）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 常见 venv 兜底位置（优先走 PATH 与 MINERU_KIT 环境变量）
FALLBACK_KIT_PATHS = [
    os.path.expanduser("~/.local/bin/mineru-kit"),
    os.path.expanduser("~/mineru/.venv/Scripts/mineru-kit.exe"),
    r"C:\MinerU\MinerU\.venv\Scripts\mineru-kit.exe",
    "/opt/mineru/.venv/bin/mineru-kit",
]


def find_mineru_kit(explicit: str | None = None) -> str | None:
    """按优先级定位 mineru-kit 可执行文件。"""
    if explicit:
        return explicit if Path(explicit).exists() else None

    env = os.environ.get("MINERU_KIT")
    if env and Path(env).exists():
        return env

    found = shutil.which("mineru-kit")
    if found:
        return found

    for p in FALLBACK_KIT_PATHS:
        if Path(p).exists():
            return p
    return None


def probe_available(kit: str) -> tuple[bool, str]:
    """探测 mineru-kit 是否可用，返回 (ok, 说明)。"""
    try:
        r = subprocess.run(
            [kit, "--version"],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return False, f"无法执行 mineru-kit: {e}"

    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, out.strip()[:400]
    return True, out.strip().splitlines()[0] if out.strip() else "ok"


def run_parse(
    kit: str,
    pdf: str,
    out_dir: Path,
    pages: str = "all",
    tier: str | None = None,
    fmt: str = "zip",
    wait: int = 3600,
    extra: list[str] | None = None,
    ocr_mode: str | None = None,
) -> subprocess.CompletedProcess:
    """调用 mineru-kit parse，产物落到 out_dir/raw。

    为什么默认 zip 而不是 markdown
    ------------------------------
    裸 markdown 不含页码与 block 类型，无法过滤页眉页脚、也无法按页分卷。
    zip 内含 middle_json.json（带 page_idx + block type）和 images/，
    这是本流程正确分卷与去噪的前提。实测 3 页 zip = 774KB。

    注意：mineru-kit 没有 --wait，它是同步阻塞的
    （实测 3 页 2.3s、44 页约 158s），因此直接用 subprocess 等待。
    """
    raw = out_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    cmd = [kit, "parse", pdf, "-o", str(raw), "--format", fmt, "--pages", pages]
    if tier:
        cmd += ["--tier", tier]
    if ocr_mode:
        cmd += ["--ocr-mode", ocr_mode]
    if extra:
        cmd += extra

    print("[mineru_parse] 执行:", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=None,
        encoding="utf-8",
        errors="replace",
    )


def unpack_zip(raw_dir: Path) -> Path | None:
    """把 raw 下的 zip 解包（幂等），返回解包目录。"""
    zips = sorted(raw_dir.rglob("*.zip"))
    if not zips:
        return None
    target = raw_dir / "unpacked"
    marker = target / "middle_json.json"
    if marker.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    import zipfile  # noqa: PLC0415

    with zipfile.ZipFile(zips[0]) as z:
        z.extractall(target)
    return target if marker.exists() else None


def collect_markdown(raw_dir: Path) -> list[Path]:
    """收集 MinerU 产出的全部 Markdown（含子目录）。"""
    return sorted(p for p in raw_dir.rglob("*.md") if p.is_file())


def merge_markdown(md_files: list[Path], out_file: Path) -> int:
    """把多份 md 合并成一份，保留原有页标记。返回合并后的字符数。"""
    parts: list[str] = []
    for p in md_files:
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"[mineru_parse] 跳过不可读文件 {p.name}: {e}", file=sys.stderr)
            continue
        parts.append(f"\n\n<!-- ===== source: {p.name} ===== -->\n\n{t.strip()}\n")

    body = "".join(parts)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(body, encoding="utf-8", newline="\n")
    return len(body)


def dump_meta(out_dir: Path, info: dict) -> None:
    (out_dir / "_mineru_meta.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="用 MinerU 解析 PDF 为 Markdown")
    ap.add_argument("--pdf", required=True, help="输入 PDF 路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--pages", default="all", help="页码，如 all / 1-5,8（默认 all）")
    ap.add_argument("--tier", default="standard",
                    help="档位：flash/basic/standard/advanced（默认 standard）")
    ap.add_argument("--format", default="zip", choices=["zip", "markdown", "middle_json"],
                    help="输出格式（默认 zip：含 middle_json + images，可分卷去噪）")
    ap.add_argument("--ocr-mode", default=None,
                    help="OCR 模式：auto/txt/ocr（有文字层可用 txt 提速）")
    ap.add_argument("--mineru-kit", default=None, help="mineru-kit 可执行文件路径")
    ap.add_argument("--no-merge", action="store_true", help="不合并 md")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[mineru_parse] PDF 不存在: {pdf}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    kit = find_mineru_kit(args.mineru_kit)
    if not kit:
        print(
            "[mineru_parse] 找不到 mineru-kit。请安装 MinerU 或用 --mineru-kit 指定路径。\n"
            "  安装：pip install mineru （或 uv tool install mineru）",
            file=sys.stderr,
        )
        return 3

    ok, note = probe_available(kit)
    if not ok:
        print(f"[mineru_parse] mineru-kit 不可用: {note}", file=sys.stderr)
        return 3
    print(f"[mineru_parse] mineru-kit: {kit} ({note})", flush=True)

    proc = run_parse(
        kit=kit,
        pdf=str(pdf),
        out_dir=out_dir,
        pages=args.pages,
        tier=args.tier,
        fmt=args.format,
        ocr_mode=args.ocr_mode,
    )

    if proc.stdout:
        print(proc.stdout, flush=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, flush=True)

    raw_dir = out_dir / "raw"
    info = {
        "pdf": str(pdf),
        "pages": args.pages,
        "tier": args.tier,
        "format": args.format,
        "mineru_kit": kit,
        "returncode": proc.returncode,
        "mineru_home": os.environ.get("MINERU_HOME", ""),
    }

    if proc.returncode != 0:
        dump_meta(out_dir, info)
        print(f"[mineru_parse] 解析失败，returncode={proc.returncode}", file=sys.stderr)
        return 4

    if args.format == "zip":
        unpacked = unpack_zip(raw_dir)
        info["unpacked"] = str(unpacked) if unpacked else ""
        if not unpacked:
            dump_meta(out_dir, info)
            print("[mineru_parse] zip 里没找到 middle_json.json，无法后处理", file=sys.stderr)
            return 5
        n_pages = 0
        try:
            mid = json.loads((unpacked / "middle_json.json").read_text(encoding="utf-8"))
            n_pages = len(mid.get("pages", []))
        except Exception as e:  # noqa: BLE001
            print(f"[mineru_parse] middle_json 解析告警: {e}", file=sys.stderr)
        info["middle_pages"] = n_pages
        dump_meta(out_dir, info)
        print(
            f"MINERU_DONE format=zip pages={n_pages} unpacked={unpacked} out={out_dir}",
            flush=True,
        )
        return 0

    # markdown / middle_json 模式：走旧合并逻辑
    md_files = collect_markdown(raw_dir)
    info["md_files"] = [str(p.relative_to(raw_dir)) for p in md_files]
    info["md_count"] = len(md_files)
    if not md_files:
        dump_meta(out_dir, info)
        print("[mineru_parse] 解析完成但未产出任何 .md，请检查 raw/ 目录", file=sys.stderr)
        return 5

    merged = out_dir / "merged.md"
    chars = merge_markdown(md_files, merged)
    info["merged"] = str(merged)
    info["merged_chars"] = chars
    dump_meta(out_dir, info)

    print(
        f"MINERU_DONE md_files={len(md_files)} merged_chars={chars} out={out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
