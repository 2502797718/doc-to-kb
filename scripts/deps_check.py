#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc-to-kb · 依赖自检（v2：MinerU 主路 + 几何兜底 双路径）

为什么分两条路径
----------------
v1 只用 pymupdf + pdfplumber 自研几何重建表格。实测 RN7326E 手册：
  · 1513 张表里 291 张（19%）列错位，真实信息挤在少数列；
  · 761 张图里 634 张（83%）是页眉色条与 logo。
v2 起默认走 MinerU（VLM 端到端，表格转 Markdown/HTML、公式转 LaTeX），
几何重建降级为「MinerU 不可用或结果退化时」的兜底。

本脚本**不把 MinerU 当硬依赖**：只要几何路径的包在，没有 MinerU 也能跑老流程。

用法:
    python deps_check.py [--json]

退出码:
    0 = 几何路径可用（MinerU 缺失仅 WARN）
    1 = 几何路径依赖缺失，无法运行
    2 = Python 版本过低
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 9)

# 几何兜底路径的硬依赖
DEPS = [
    ("pymupdf", "pymupdf", "PDF 书签树 / 逐页文字层 / 页数", "pymupdf"),
    ("pdfplumber", "pdfplumber", "逐词坐标（表格列对齐、行聚类）", "pdfplumber"),
]

# mineru-kit 常见安装位置（兜底用；优先走 PATH 与 MINERU_KIT 环境变量）
FALLBACK_KIT_PATHS = [
    os.path.expanduser("~/.local/bin/mineru-kit"),
    os.path.expanduser("~/mineru/.venv/Scripts/mineru-kit.exe"),
    r"C:\MinerU\MinerU\.venv\Scripts\mineru-kit.exe",
    "/opt/mineru/.venv/bin/mineru-kit",
]


def _import_version(mod_name: str) -> str | None:
    try:
        mod = __import__(mod_name)
    except ImportError:
        return None
    return getattr(mod, "__version__", "?")


def find_kit() -> str | None:
    env = os.environ.get("MINERU_KIT")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    w = shutil.which("mineru-kit")
    if w:
        return w
    for p in FALLBACK_KIT_PATHS:
        pp = Path(p).expanduser()
        if pp.exists():
            return str(pp)
    return None


def check_mineru(kit: str | None) -> tuple[bool, str, str]:
    """返回 (可用, 说明, 版本)。"""
    if not kit:
        return False, "未找到 mineru-kit（将走几何兜底路径）", ""
    try:
        r = subprocess.run(
            [kit, "--version"], capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return False, f"mineru-kit 调用失败: {e}", ""
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    first = text.splitlines()[0] if text else "?"
    if r.returncode != 0:
        return False, f"mineru-kit 执行异常: {first[:160]}", ""
    return True, first, first


def _models_show(kit: str) -> str:
    """取 mineru-kit models show 的原始文本（失败返回空串）。"""
    try:
        r = subprocess.run(
            [kit, "models", "show"],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def check_models(kit: str) -> tuple[bool, str]:
    """校验「实际生效后端」所需模型是否就绪（只校验，不下载）。

    为什么不再用 `models verify --tier standard` 的返回码
    ------------------------------------------------------
    2026-11-11 实测教训：`standard` 档名义上 = onnx + MinerU2.5-Pro-...GGUF，
    而本机只走 onnx（`Effective small backend: onnx`），GGUF 没有是**正常的**。
    `models verify --tier standard` 因此必然非 0 → 旧逻辑误报 WARN，
    连「模型其实齐了」的机器也被判缺，害人反复下载/排查。

    新判据（三步，全部满足才算就绪）：
      1. `models show` 能跑起来，且能读出 `Effective small backend: <backend>`；
      2. backend 对应的 repo 目录存在——onnx → `MinerU-4_models_onnx`；
      3. 该 repo 下 `Layout/` 与 `OCR/` 两个主权重目录都非空。

    `models verify` 里报缺的 `MFR/`（公式）、`Table/`（表格）、`seal_*`（印章 OCR）
    都是**可选增强组件**，不进判据；只在就绪时作为提示附带。
    """
    show = _models_show(kit)
    if not show:
        return False, "models show 无输出（mineru-kit 可能不可用）"

    # mineru-kit 的输出会随 locale 变化：英文 "Effective small backend: onnx"
    # / 中文 "生效的小模型后端: onnx"。两种都要匹配，否则明明齐了也判失败。
    m = (re.search(r"(?:Effective small backend|生效的小模型后端)\s*[:：]\s*(\S+)", show)
         or re.search(r"model\.small_backend:\s*(\S+)", show))
    if not m:
        return False, "读不到生效后端（models show 输出格式未识别）"
    backend = m.group(1).strip()
    if backend == "auto":
        # auto 时以仓库目录实际存在者为准，优先 onnx
        for cand in ("onnx", "torch"):
            repo_cand = os.path.join(
                os.environ.get("MINERU_HOME") or os.path.expanduser("~/.mineru"),
                "models", f"MinerU-4_models_{cand}")
            if os.path.isdir(repo_cand):
                backend = cand
                break
        else:
            backend = "onnx"

    repo_by_backend = {
        "onnx": "MinerU-4_models_onnx",
        "torch": "MinerU-4_models_torch",
    }
    repo = repo_by_backend.get(backend)
    if repo is None:
        # 未知 backend 不判死，交给运行时暴露
        return True, f"backend={backend}（未登记，跳过目录校验）"

    base = os.environ.get("MINERU_HOME") or os.path.join(
        os.path.expanduser("~"), ".mineru")
    models_root = os.path.join(base, "models")

    # MINERU_HOME 可能直接指向 models 的父目录，也可能指向 models 本身
    cands = [os.path.join(models_root, repo), os.path.join(base, repo)]
    repo_dir = next((p for p in cands if os.path.isdir(p)), None)
    if repo_dir is None:
        return False, f"backend={backend} 但 {repo} 目录不存在（查 {models_root}）"

    missing = []
    for sub in ("Layout", "OCR"):
        d = os.path.join(repo_dir, sub)
        if not os.path.isdir(d) or not os.listdir(d):
            missing.append(sub)
    if missing:
        return False, f"{repo} 缺主权重目录: {', '.join(missing)}"

    size = ""
    try:
        total = 0
        for root, _dirs, files in os.walk(repo_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        size = f", {total / 1024 / 1024:.0f}MB"
    except Exception:  # noqa: BLE001
        pass

    note = f"{repo} 就绪（backend={backend}{size}）"
    opt = re.findall(r"(MFR|Table|seal_[A-Za-z0-9_]*)", show)
    if opt:
        uniq = sorted(set(opt))
        note += f"；可选组件未装: {', '.join(uniq)}（不影响主路）"
    return True, note


def main() -> int:
    ap = argparse.ArgumentParser(description="chip-manual-to-kb 依赖自检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    py_ver = sys.version_info
    py_ok = (py_ver.major, py_ver.minor) >= MIN_PY

    geo: list[dict] = []
    for name, mod, purpose, pipname in DEPS:
        ver = _import_version(mod)
        geo.append({
            "name": name, "ok": ver is not None,
            "detail": ver or f"缺：pip install {pipname}",
            "purpose": purpose,
        })
    geo_ok = py_ok and all(g["ok"] for g in geo)

    kit = find_kit()
    mu_ok, mu_note, mu_ver = check_mineru(kit)
    mu_home = os.environ.get("MINERU_HOME", "")
    models_ok, models_note = (check_models(kit) if mu_ok and kit else (False, "（跳过）"))

    result = {
        "python": {"version": sys.version.split()[0], "ok": py_ok,
                   "min": ".".join(map(str, MIN_PY))},
        "platform": f"{platform.system()} {platform.machine()}",
        "geometry_path": {"ok": geo_ok, "deps": geo},
        "mineru_path": {
            "ok": mu_ok, "kit": kit or "", "version": mu_ver,
            "note": mu_note, "models_standard_ok": models_ok,
            "models_note": models_note, "mineru_home": mu_home,
        },
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if geo_ok else 1

    print("=" * 62)
    print("doc-to-kb 依赖自检（v2 · MinerU 主路 + 几何兜底）")
    print("=" * 62)
    print(f"解释器   : {sys.executable}")
    print(f"平台     : {result['platform']}")
    print(f"Python   : {sys.version.split()[0]}  {'OK' if py_ok else 'FAIL (< %s)' % result['python']['min']}")
    print("-" * 62)

    print("[几何兜底路径] 硬依赖")
    for g in geo:
        print(f"  [{'OK  ' if g['ok'] else 'FAIL'}]  {g['name']:<12} {g['detail']}")
        if not g["ok"]:
            print(f"        用途: {g['purpose']}")
    print("-" * 62)

    print("[MinerU 主路径] 推荐（缺失仅 WARN，不阻断）")
    print(f"  [{'OK  ' if mu_ok else 'WARN'}]  mineru-kit   {mu_note}")
    if kit:
        print(f"        路径: {kit}")
        print(f"        MINERU_HOME: {mu_home or '(未设置，使用默认 ~/.mineru)'}")
        print(f"  [{'OK  ' if models_ok else 'WARN'}]  模型 {models_note}")
        if not models_ok:
            print("        修复: mineru-kit models download --tier basic")
            print("        提示: 本流程只需 onnx 后端（Layout/OCR），不必装 GGUF/MFR/Table 可选组件")
            print("        若模型在别处，设置 MINERU_HOME 指向其父目录")
    else:
        print("        安装: pip install mineru   （或 uv tool install mineru）")
        print("        或 --mineru-kit 指定可执行文件路径")
    print("-" * 62)

    if not geo_ok:
        print("结论: 几何兜底路径依赖缺失，无法运行。请先安装标 FAIL 的包。")
        return 1
    if mu_ok and models_ok:
        print("结论: 双路径可用 —— 默认走 MinerU（表格质量显著更好），失败可回落。")
    else:
        print("结论: 仅几何兜底可用 —— 能跑，但表格可能列错位，建议补齐 MinerU。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
