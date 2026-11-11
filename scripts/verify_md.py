#!/usr/bin/env python3
"""chip-manual-to-kb · 产出自检（三关）

=== 三关是什么 ===
关1 体积关：每卷 UTF-8 字节 <= 上限（65,535B 是向量库硬限，默认阈值 60,000B）。
           超限会导致该条记录在向量库**静默失索引**，必须卡死。
关2 覆盖关：章节覆盖率 —— 原册 1 级书签是否都在产出里出现，避免漏章。
关3 表格关：统计待复核表 / 疑表数量，并抽样核对 Markdown 表结构
           （每行列数一致、分隔行列数与表头一致）。

=== 用法 ===
  python verify_md.py <out_dir> [--limit 60000] [--json]

退出码: 0 = 三关全过；1 = 有失败项
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

DEFAULT_LIMIT = 60000
MAX_MD_TABLE_ROWS_SAMPLE = 40

_FENCE = re.compile(r"```.*?```", re.S)
_ROW = re.compile(r"^\|.*\|$")

# 表格单元格里允许出现转义的竖线 `\|`（如 `|TEMP - TEMP_UD| ≥ LT_GAP`），
# 它属于内容而不是列分隔符。统计列数前必须先剔除，否则会把 5 列误判成 7 列。
_ESCAPED_PIPE = re.compile(r"\\\|")


def count_cols(row: str) -> int:
    """统计一行 Markdown 表的列数（忽略转义竖线）。"""
    return _ESCAPED_PIPE.sub("", row).count("|") - 1


def strip_code_blocks(text: str) -> str:
    return _FENCE.sub("", text)


def _volume_files(dir_path: str) -> list:
    """只统计分卷（NN_ 前缀），排除 README.md 与 _manifest.json"""
    return sorted(f for f in os.listdir(dir_path)
                  if f.endswith(".md") and re.match(r"^\d\d_", f))


def check_sizes(dir_path: str, limit: int) -> list:
    """关1：体积关"""
    issues = []
    for f in _volume_files(dir_path):
        p = os.path.join(dir_path, f)
        size = os.path.getsize(p)
        if size > limit:
            issues.append({"level": "FAIL", "file": f,
                           "detail": "%d B > 上限 %d B" % (size, limit)})
        elif size > int(limit * 0.92):
            issues.append({"level": "WARN", "file": f,
                           "detail": "%d B 接近上限 %d B" % (size, limit)})
    return issues


def check_tables(dir_path: str) -> list:
    """关3：表格结构关 —— 每张 Markdown 表的行内列数必须一致"""
    issues = []
    for f in _volume_files(dir_path):
        p = os.path.join(dir_path, f)
        with open(p, encoding="utf-8") as fh:
            text = strip_code_blocks(fh.read())
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            if _ROW.match(lines[i].strip()):
                block = []
                j = i
                while j < len(lines) and _ROW.match(lines[j].strip()):
                    block.append(lines[j].strip())
                    j += 1
                if len(block) >= 2:
                    ncols = [count_cols(r) for r in block]
                    if len(set(ncols)) != 1:
                        issues.append({
                            "level": "FAIL", "file": f,
                            "detail": "表（第 %d 行起）列数不一致: %s"
                                      % (i + 1, sorted(set(ncols))),
                        })
                    # 分隔行必须存在且列数匹配
                    if not re.match(r"^\|[\s:\-|]+\|$", block[1]):
                        issues.append({
                            "level": "WARN", "file": f,
                            "detail": "表（第 %d 行起）缺分隔行" % (i + 1),
                        })
                i = j
            else:
                i += 1
    return issues


def check_coverage(dir_path: str, manifest_path: str, pdf_path: str = "") -> list:
    """关2：覆盖关 —— 页码必须连续，断档 = 内容丢失

    为什么断档判 FAIL 而非 WARN:
    实测踩过——14 章书签在 p211、首个子书签 14.1 在 p212，父级因有子卷而不生成
    独立卷，导致 p211 正文**真的丢失**。早期把断档当 WARN，缺陷被放过。
    页码断档只有两种可能：① 书签归属间隙（补「父级引言卷」可修）；
    ② 该页确实无内容（极罕见）。无论哪种都必须人工确认，故判 FAIL。
    """
    issues = []
    if not os.path.exists(manifest_path):
        return [{"level": "FAIL", "file": "-",
                 "detail": "缺 _manifest.json，无法核对章节覆盖"}]
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not manifest:
        return [{"level": "FAIL", "file": "-", "detail": "manifest 为空"}]

    ranges = sorted((m["page"], m["end"]) for m in manifest)
    for a, b in zip(ranges, ranges[1:]):
        if b[0] > a[1] + 1:
            issues.append({
                "level": "FAIL", "file": "-",
                "detail": "页码断档: p.%d 之后直接 p.%d（缺 %d 页，内容可能丢失）"
                          % (a[1], b[0], b[0] - a[1] - 1),
            })

    # 页码重叠**本身不是缺陷**：同页多小节（如 I2C 章 11.1/11.2/11.3 都指向 p133）
    # 会产生合法的区间重叠，内容已按标题 y 切分。真正要查的是「同页卷内容是否重复」。
    return issues


def check_duplicate_content(dir_path: str, manifest_path: str,
                            min_len: int = 200) -> list:
    """关2 附加：检测「同页多卷输出相同内容」的重复。

    判据: 取每卷正文（去掉 YAML 头与本卷标题）的正文指纹；若两卷共享起始页
    且正文完全相同（长度 >= min_len），判 FAIL —— 说明同页切分失效。
    实测反例: 修复前 11.1 与 11.2 两卷正文逐字相同。
    """
    issues = []
    if not os.path.exists(manifest_path):
        return issues
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    # 按起始页分组
    by_page = {}
    for m in manifest:
        by_page.setdefault(m["page"], []).append(m)

    def body_of(fname: str) -> str:
        p = os.path.join(dir_path, fname)
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        # 去掉 YAML 头（--- 到 ---）与首个 # 标题
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                text = text[end + 4:]
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.startswith("# ") and ln.strip()]
        return "\n".join(lines).strip()

    for page, group in by_page.items():
        if len(group) < 2:
            continue
        bodies = [(m["file"], body_of(m["file"])) for m in group]
        for i in range(len(bodies)):
            for j in range(i + 1, len(bodies)):
                a_name, a_body = bodies[i]
                b_name, b_body = bodies[j]
                if len(a_body) < min_len or len(b_body) < min_len:
                    continue
                if a_body == b_body:
                    issues.append({
                        "level": "FAIL", "file": "%s / %s" % (a_name, b_name),
                        "detail": "同页 p.%d 两卷正文完全相同（%d 字符），同页切分失效"
                                  % (page, len(a_body)),
                    })
    return issues


def count_markers(dir_path: str) -> dict:
    n_agent = n_suspect = 0
    for f in _volume_files(dir_path):
        with open(os.path.join(dir_path, f), encoding="utf-8") as fh:
            t = fh.read()
        n_agent += t.count("TABLE_NEEDS_AGENT")
        n_suspect += t.count("TABLE_SUSPECT")
    return {"needs_agent": n_agent, "suspect": n_suspect}



def check_closure(dir_path: str) -> list:
    """关4 闭环关：待处理标记必须归零（用户要求「交付完美成品」）。

    为什么单独设一关（2026-10-29 用户明确要求）:
    只生成截图就交付，等于把「请人工复核」的标记留在知识库里——
    那不是成品。闭环标准: TABLE_NEEDS_AGENT / TABLE_SUSPECT 计数为 0。
    若确实无法自动修复（如框图不是表格），用 apply_fixes.py 的 action="drop"
    删除标记并保留原始文字，同样计入闭环。
    """
    issues = []
    marks = count_markers(dir_path)
    if marks["needs_agent"] > 0:
        issues.append({
            "level": "FAIL", "file": "-",
            "detail": "还有 %d 张 TABLE_NEEDS_AGENT 未处理（agent 读图后写 fixes.json 并跑 apply_fixes.py）"
                      % marks["needs_agent"],
        })
    if marks["suspect"] > 0:
        issues.append({
            "level": "FAIL", "file": "-",
            "detail": "还有 %d 处 TABLE_SUSPECT 未处理（重写或 drop）"
                      % marks["suspect"],
        })
    return issues


def main():
    ap = argparse.ArgumentParser(description="产出三关自检")
    ap.add_argument("dir", help="产出目录")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    d = os.path.abspath(args.dir)
    if not os.path.isdir(d):
        print("[FAIL] 目录不存在: %s" % d)
        return 2

    sizes = check_sizes(d, args.limit)
    tables = check_tables(d)
    cover = check_coverage(d, os.path.join(d, "_manifest.json"))
    dups = check_duplicate_content(d, os.path.join(d, "_manifest.json"))
    marks = count_markers(d)
    closure = check_closure(d)

    all_issues = sizes + tables + cover + dups + closure
    fails = [x for x in all_issues if x["level"] == "FAIL"]
    warns = [x for x in all_issues if x["level"] == "WARN"]

    if args.json:
        print(json.dumps({
            "dir": d, "limit": args.limit,
            "fails": fails, "warns": warns, "markers": marks,
            "ok": not fails,
        }, ensure_ascii=False, indent=2))
        return 0 if not fails else 1

    n_md = len(_volume_files(d))
    total = sum(os.path.getsize(os.path.join(d, f)) for f in _volume_files(d))
    print("=" * 66)
    print("产出自检: %s" % d)
    print("=" * 66)
    print("卷数      : %d" % n_md)
    print("总体积    : %d B (%.2f MB)" % (total, total / 1024 / 1024))
    print("单卷上限  : %d B" % args.limit)
    print("-" * 66)
    print("关1 体积  : %s" % ("PASS" if not [x for x in sizes if x["level"] == "FAIL"]
                                else "FAIL"))
    print("关2 覆盖  : %s" % ("PASS" if not [x for x in cover if x["level"] == "FAIL"]
                                else "FAIL"))
    print("关3 表格  : %s" % ("PASS" if not [x for x in tables if x["level"] == "FAIL"]
                                else "FAIL"))
    print("关4 闭环  : %s" % ("PASS" if not closure else "FAIL"))
    print("-" * 66)
    print("待复核表  : %d 张（TABLE_NEEDS_AGENT，需 agent 读图重写）"
          % marks["needs_agent"])
    print("疑表未出  : %d 处（TABLE_SUSPECT，无栅格几何，已保留原始文字）"
          % marks["suspect"])
    print("-" * 66)

    for x in fails:
        print("  [FAIL] %-44s %s" % (x["file"][:44], x["detail"]))
    for x in warns:
        print("  [WARN] %-44s %s" % (x["file"][:44], x["detail"]))
    if not all_issues:
        print("  无问题")

    print("-" * 66)
    if fails:
        print("结论: 有 %d 项 FAIL，修完再入库。" % len(fails))
        return 1
    print("结论: 三关全过，可以入库。"
          + ("（另有 %d 项 WARN，逐条判读）" % len(warns) if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
