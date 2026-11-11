#!/usr/bin/env python3
"""chip-manual-to-kb · 表格修复回写（闭环最后一步）

=== 为什么需要本脚本（用户明确要求「主动闭环，交付完美成品」）===
转写流程会产生两类待处理标记：
  · `TABLE_NEEDS_AGENT` —— 有几何但程序置信度不足
  · `TABLE_SUSPECT`     —— 无几何，程序不敢出表
若只生成截图就结束，交付的是**半成品**：知识库里留着「请人工复核」的标记。

闭环必须由 agent 完成「读图 → 重写 → 写回」：
  1. `render_crops.py` 生成截图与清单（`_review/manifest.json`）
  2. **agent 逐条读 PNG**，把表格重写成 Markdown
  3. agent 把结果写进 `_review/fixes.json`（本脚本的输入格式）
  4. 本脚本按「页码 + y 区间」精确定位标记块，替换成 agent 重写的内容
  5. `verify_md.py` 复检，待处理数必须归零（否则不算闭环）

=== fixes.json 格式 ===
```json
[
  {
    "page": 173,
    "y0": 582.8,
    "kind": "needs_agent",          // 或 "suspect"
    "action": "replace",            // replace=替换为表格 / drop=删标记留原文
    "markdown": "| 序号 | Kbps | ... |\\n| --- | ... |\\n| 1 | 2.4 | ... |",
    "note": "原册 8 列，程序判 3 列故拒绝；已按截图重写"
  }
]
```

- `action="replace"`：用 `markdown` 替换整个标记块（含标记注释、提示文字、原始文字块）
- `action="drop"`：只删除标记块，保留其后的原始文字（适用于「该处其实不是表格」，
  如框图、时序图、正文段落）

=== 定位规则（精确，避免误替换）===
按 `page` + `y0` 匹配标记里的 `y0=` 字段（浮点比较容差 0.5pt）。
同一文件内多个标记互不干扰；替换是「整块」替换，从标记注释行到其后的
```text``` 原始文字块结束。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ---- 标记块正则：从标记注释开始，到**下一个标记注释之前**或文件结束。
# 关键: 必须「遇到下一个 <!-- TABLE_xxx --> 即停」，
# 否则贪婪匹配会吞掉后续多个标记块（实测 p278 的 SUSPECT 块吞掉了
# p284 的 NEEDS_AGENT 块，导致 p284 定位失败）。
_MARKER_BLOCK = re.compile(
    r"<!--\s*(TABLE_NEEDS_AGENT|TABLE_SUSPECT):\s*([^>]*?)-->\n"   # 标记注释行
    r"(?:\n>.*?\n)?"                                               # 可选 > 提示段（首行）
    r"((?:\n?>.*?\n|\n)*)"                                         # 其余 > 提示行
    r"(?:\n`	ext\n(.*?)\n`\n)?"                               # 可选原始文字块（到此为止）
    , re.S,
)


def _kv(s: str) -> dict:
    return dict(re.findall(r"(\w+)=([^\s]+)", s))


def _find_block(text: str, page: int, y0: float,
                kind: str = "") -> tuple:
    """在文本中定位指定 page/y0 的标记块，返回 (start, end, kind, raw_attrs)"""
    for m in _MARKER_BLOCK.finditer(text):
        kv = _kv(m.group(2))
        try:
            p = int(kv["page"])
            y = float(kv["y0"])
        except (KeyError, ValueError):
            continue
        if p != page or abs(y - y0) > 0.5:
            continue
        if kind and m.group(1).lower().replace("table_", "") != kind:
            continue
        return (m.start(), m.end(), m.group(1), kv)
    return None


def apply_one(text: str, fix: dict) -> tuple:
    """应用单条修复，返回 (新文本, 是否成功)"""
    page = int(fix["page"])
    y0 = float(fix["y0"])
    kind = (fix.get("kind") or "").lower()
    found = _find_block(text, page, y0, kind)
    if not found:
        return text, False

    start, end, marker_kind, kv = found
    action = (fix.get("action") or "replace").lower()

    if action == "drop":
        # 删标记块，但保留原始文字（去掉 `>` 提示与标记，保留 ```text``` 内容）
        block = text[start:end]
        m_txt = re.search(r"```text\n(.*?)\n```", block, re.S)
        replacement = ""
        if m_txt:
            replacement = "```text\n%s\n```\n" % m_txt.group(1)
        new_text = text[:start] + replacement + text[end:]
        return new_text, True

    # replace：用 agent 重写的 markdown 替换整块
    md = (fix.get("markdown") or "").rstrip()
    if not md:
        return text, False
    note = fix.get("note")
    head = ""
    if note:
        head = "> 来源：原册 p.%d（agent 读图重写：%s）\n\n" % (page, note)
    replacement = head + md + "\n"
    return text[:start] + replacement + text[end:], True


def main():
    ap = argparse.ArgumentParser(description="表格修复回写（闭环）")
    ap.add_argument("out_dir", help="产出目录（含 md 与 _review/）")
    ap.add_argument("--fixes", default=None,
                    help="fixes.json 路径（默认 <out>/_review/fixes.json）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = ap.parse_args()

    out = os.path.abspath(args.out_dir)
    fixes_path = args.fixes or os.path.join(out, "_review", "fixes.json")
    if not os.path.exists(fixes_path):
        print("[FAIL] 找不到 fixes.json：%s" % fixes_path)
        print("       agent 需先读图并把重写结果写入该文件（格式见脚本头注释）。")
        return 2

    with open(fixes_path, encoding="utf-8") as fh:
        fixes = json.load(fh)
    if not isinstance(fixes, list):
        print("[FAIL] fixes.json 必须是数组")
        return 2

    # 按文件分组应用（每条修复都重新扫描全目录，找它所在的 md）
    md_files = [f for f in os.listdir(out)
                if f.endswith(".md") and re.match(r"^\d\d_", f)]

    applied = 0
    missed = []
    for fix in fixes:
        hit = False
        for fname in md_files:
            path = os.path.join(out, fname)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            new_text, ok = apply_one(text, fix)
            if ok:
                if not args.dry_run:
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(new_text)
                applied += 1
                hit = True
                print("  [OK]   p.%-4d y=%-7.1f %-10s -> %s"
                      % (fix["page"], fix["y0"],
                         (fix.get("action") or "replace"), fname))
                break
        if not hit:
            missed.append(fix)

    print()
    print("APPLY_DONE applied=%d missed=%d%s"
          % (applied, len(missed), " (dry-run)" if args.dry_run else ""))
    for f in missed:
        print("  [MISS] p.%-4d y=%.1f kind=%s（未找到匹配标记，检查 page/y0）"
              % (f["page"], f["y0"], f.get("kind", "")))
    return 0 if not missed else 1


if __name__ == "__main__":
    sys.exit(main())
