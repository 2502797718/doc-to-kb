<div align="center">

# doc-to-kb

**Turn technical PDFs — chip manuals, IEC/GB/EN standards, protocol specs — into a searchable, chapter-split Markdown knowledge base.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![MinerU](https://img.shields.io/badge/parser-MinerU%204.x-green.svg)](https://github.com/opendatalab/MinerU)
[![Agent Skill](https://img.shields.io/badge/format-SKILL.md-purple.svg)](https://github.com/anthropics/skills)

[English](#english) · [中文](#中文)

</div>

---

## English

### Why this exists

Turning a 400-page PDF manual into something an LLM can actually query is harder than it looks. Naive PDF-to-Markdown gives you:

- **Tables that collapse.** A 6-column register table becomes a 18-column mess with 13 empty cells.
- **Header/footer noise.** Up to 19% of extracted blocks are page headers, footers, and page numbers.
- **Broken image links.** Relative paths render as dead text in most Markdown viewers.
- **One giant file** that blows past vector-DB field limits and silently loses its index.
- **No page anchoring.** You can't cite "clause 4.5 on page 14" if pages are gone.

`doc-to-kb` is an agent skill that runs a **7-step pipeline** to fix all of that, using [MinerU](https://github.com/opendatalab/MinerU) as the primary parser (VLM end-to-end) with a self-built geometric reconstruction path as fallback.

### What makes it different

| Problem | How this solves it |
|---|---|
| Table columns misaligned | MinerU VLM produces correct headers; **0 of 1513 tables** misaligned vs **291 (19%)** with geometric parsing |
| Headers/footers pollute content | Filters by MinerU's block *type* (`header`/`footer`/`page_number`), not by guessing |
| Formulas become plain text | Emitted as **LaTeX** (`$Y_{b}=\sqrt{\sum...}$`) |
| Images don't render | Rewrites every image to an absolute `viking://` URI — the only form most renderers resolve |
| Files exceed vector-DB limits | Splits by bookmark tree, hard cap **60,000 bytes/volume** (65,535 is the silent-failure limit) |
| No navigation | Generates `README.md` with chapter ↔ file ↔ page mapping |
| **Scanned PDFs with no bookmarks fail** | **Auto-falls back** to building the chapter tree from MinerU's title blocks — verified on 3 bookmark-less scanned standards |

### Quick start

```bash
# 1. Check what you have
python scripts/deps_check.py

# 2. Inspect the PDF (pages, bookmarks, text layer)
python scripts/pdf_to_md.py --probe your-manual.pdf

# 3. Parse with MinerU (zip format is required — it carries page_idx + block types)
python scripts/mineru_parse.py --pdf your-manual.pdf --out ./out

# 4. Clean noise, convert tables, split into volumes
python scripts/postprocess.py --pdf your-manual.pdf --out ./out \
  --chip MYCHIP --manual RM_V1.0 \
  --viking-base viking://resources/shared/chips/MYCHIP/RM_V1.0/

# 5. Four-gate self-check (size / coverage / table integrity / closure)
python scripts/verify_md.py ./out
```

### How it works

```
PDF
 ├─ deps_check.py        → what's installed, which path to take
 ├─ pdf_to_md.py --probe → page count, bookmarks, text layer
 ├─ mineru_parse.py      → zip + middle_json.json (page_idx + block types) + images/
 ├─ postprocess.py       → noise filter → HTML tables to Markdown → split by bookmark
 │                         (fallback: split by MinerU title blocks when no bookmarks)
 └─ verify_md.py         → 4 gates, all must pass
```

**The `middle_json.json` is the key artifact.** Plain Markdown from MinerU has no page numbers and no block types — so you cannot remove noise or split by page. Always use `--format zip`.

### Installation

```bash
# Python deps (geometric fallback path — required)
pip install pymupdf pdfplumber

# Primary path (recommended)
pip install mineru
mineru server start
mineru config set parse_server.local.mode managed
```

Then verify:

```bash
python scripts/deps_check.py
mineru-kit models show     # look for "Effective small backend"
```

> **⚠️ Note on `deps_check.py` warnings.** If you see `模型 standard ... missing key paths`, **it is usually a false alarm.** The check historically compared against *all optional repos* (torch build, GGUF VLM, etc.). What actually matters is:
> 1. `mineru-kit models show` reports `Effective small backend: onnx`
> 2. `Layout/` and `OCR/` under `MinerU-4_models_onnx/` are non-empty
>
> Missing `MFR/` (formulas), `Table/`, and `seal_*` are **optional enhancements** and do not block the main path. The `--tier standard` flag only nominally requires a GGUF model; the onnx path does not load it.

### As an agent skill

Drop the directory into your agent's skill root:

```bash
# Claude Code / Codex / DeepSeek Harness style
cp -r doc-to-kb ~/.dsh/skills/doc-to-kb        # DSH
cp -r doc-to-kb ~/.claude/skills/doc-to-kb     # Claude Code
```

Or install into [OpenViking](https://github.com/) as a shared skill:

```bash
ov skills add ./doc-to-kb -p viking://agent/skills -y
```

Once installed, phrases like *"import this standard into the knowledge base"*, *"re-import this — the last version was poor quality"*, or *"turn this PDF manual into something searchable"* will trigger it.

### Measured results

Real throughput and quality on production documents:

| Document | Pages | Nature | Volumes | Images | Time |
|---|---:|---|---:|---:|---|
| Chip RM (large) | 495 | text layer | 66 | 869 | seg 3+17+6 min (>400 pages requires segmentation) |
| Chip RM (medium) | 317 | text layer | 48 | 601 | ~6 min |
| IEC 61000-4-7 | 44 | scanned | 25 | 71 | ~158 s |
| GB/T 18216.12-2025 | 103 | **pure image, 0 bookmarks** | 33 | 15 | OCR, completed in one run |
| IEC 61000-4-30 | 70 | **scanned, 0 bookmarks** | 13 | 28 | title-block fallback |

MinerU throughput: **~1.2–1.3 pages/sec** on GPU.

### Hard limits you should know

1. **>400 pages must be segmented.** A 495-page run hung with zero output for 30 min (server log silent). Segment at ≤165 pages with `merge_segments.py`.
2. **60,000 bytes per volume.** Exceeding 65,535 bytes causes **silent de-indexing** — no error, the record just disappears.
3. **Zip output is mandatory.** Bare Markdown has no `page_idx` and no block types.
4. **Cross-page tables are not merged** — a table spanning two pages appears as two halves.
5. **Formulas render as source** in some viewers (KaTeX is bundled but not wired into Markdown body). Semantically searchable; cosmetically raw.
6. **Never `rm` and re-upload an indexed directory.** Use `mv` to preserve the index. See the incident write-up in `SKILL.md` rule 9.

### Repository layout

```
doc-to-kb/
├── SKILL.md                  entry point: 7-step flow + hard rules
├── scripts/
│   ├── deps_check.py         dual-path dependency check
│   ├── mineru_parse.py       ★ primary: MinerU to zip + middle_json
│   ├── merge_segments.py     ★ primary: merge segmented runs (>400 pages)
│   ├── postprocess.py        ★ primary: denoise + table conversion + split
│   ├── verify_md.py          four-gate self-check
│   ├── fix_img_paths.py      patch: relative → absolute image URIs
│   ├── fix_iec_img_links.py  patch: comment-style image refs → real images
│   ├── pdf_to_md.py          fallback: probe / geometric splitting
│   ├── table_rebuild.py      fallback: geometric table reconstruction
│   ├── render_crops.py       fallback: 300dpi table crops for review
│   ├── bitfield_parse.py     fallback: register bitfield table parser
│   └── apply_fixes.py        fallback: write reviewed fixes back
├── references/
│   ├── pipeline.md           algorithms and parameter rationale
│   ├── pitfalls.md           every trap hit, with measured data
│   └── output_layout.md      output conventions
├── templates/README_index.md navigation page template
├── LICENSE                   MIT
└── .gitignore
```

### Contributing

Issues and PRs welcome. If you hit a new PDF variant that breaks the pipeline, please include:

- page count, bookmark count, and whether it has a text layer
- a redacted screenshot of the failing table or section
- the `_postprocess_report.json` block statistics

### License

[MIT](LICENSE) © 2026 wangnl

---

## 中文

### 这个项目解决什么问题

把一本几百页的 PDF 手册变成 LLM 能真正检索的知识库，比看起来难得多。直接 PDF→Markdown 会遇到：

- **表格塌陷**：6 列的寄存器表变成 18 列、13 列是空的
- **页眉页脚噪音**：实测高达 19% 的块是页眉、页脚、页码
- **图片链接断掉**：相对路径在多数 Markdown 渲染器里只显示成死文字
- **单文件过大**：超过向量库字段上限会**静默失索引**（不报错，记录直接消失）
- **没有页码锚点**：页码丢了就没法引用"第 4.5 条，第 14 页"

`doc-to-kb` 是一个 agent 技能，用 **7 步流程**解决这些问题：主路走 [MinerU](https://github.com/opendatalab/MinerU)（VLM 端到端），自研几何重建作为兜底。

### 核心特性

| 问题 | 怎么解 |
|---|---|
| 表格列错位 | MinerU VLM 直接给正确表头：1513 张表 **0 处错列**，几何路径 **291 张（19%）** 错列 |
| 页眉页脚污染 | 按 MinerU 的 block **类型**过滤（`header`/`footer`/`page_number`），不靠猜 |
| 公式变纯文本 | 输出 **LaTeX**（`$Y_{b}=\sqrt{\sum...}$`） |
| 图片显示不出来 | 全部重写为绝对 `viking://` URI —— 实测渲染器只认这种形式 |
| 单卷超限失索引 | 按书签树分卷，硬上限 **60,000 字节/卷**（65,535 是静默失索引的红线） |
| 没有导航 | 自动生成 `README.md`（章 ↔ 文件 ↔ 页码对照） |
| **零书签扫描件跑不动** | **自动回退**到按 MinerU 标题块建章节树 —— 已在 3 份零书签扫描标准上验证成功 |

### 快速开始

```bash
python scripts/deps_check.py                                    # 1. 环境自检
python scripts/pdf_to_md.py --probe your.pdf                    # 2. 结构勘察
python scripts/mineru_parse.py --pdf your.pdf --out ./out       # 3. MinerU 解析
python scripts/postprocess.py --pdf your.pdf --out ./out \
  --chip MYCHIP --manual RM_V1.0 \
  --viking-base viking://resources/shared/chips/MYCHIP/RM_V1.0/ # 4. 去噪+分卷
python scripts/verify_md.py ./out                               # 5. 四关自检
```

### 关于 `deps_check.py` 的误报（重要）

看到 `模型 standard ... missing key paths` **多半是误报**。旧判据拿「全部可选 repo」比对，而实际只走 onnx 一路。正确判法是：

```bash
mineru-kit models show     # 看 "Effective small backend: onnx"
mineru-kit models verify   # 缺的若只在 MFR/Table/seal，属可选组件
```

只要 `Layout/` + `OCR/` 有文件就能跑。缺 `MFR/`（公式）、`Table/`、`seal_*` 不影响主路。`--tier standard` 名义上要多一个 GGUF 模型，但 onnx 路径不加载它。

### 作为 agent 技能安装

```bash
cp -r doc-to-kb ~/.dsh/skills/doc-to-kb          # DeepSeek Harness
cp -r doc-to-kb ~/.claude/skills/doc-to-kb       # Claude Code
ov skills add ./doc-to-kb -p viking://agent/skills -y   # OpenViking 共享
```

装上之后，说「把这个标准传进知识库」「之前入库的质量太差要重做」「把这份 PDF 手册转成能查的」就会触发。

### 实测数据

| 文档 | 页数 | 类型 | 卷数 | 图 | 耗时 |
|---|---:|---|---:|---:|---|
| 芯片 RM（大） | 495 | 有文字层 | 66 | 869 | 分段 3+17+6 分钟（>400 页必须分段） |
| 芯片 RM（中） | 317 | 有文字层 | 48 | 601 | 约 6 分钟 |
| IEC 61000-4-7 | 44 | 扫描件 | 25 | 71 | 约 158 秒 |
| GB/T 18216.12-2025 | 103 | **纯图像，0 书签** | 33 | 15 | OCR，一次跑通 |
| IEC 61000-4-30 | 70 | **扫描件，0 书签** | 13 | 28 | 标题块回退 |

MinerU 吞吐：GPU 上 **约 1.2–1.3 页/秒**。

### 必须知道的硬限制

1. **超过约 400 页必须分段**：495 页一次跑会卡死（30 分钟零输出）。按 ≤165 页分段，再用 `merge_segments.py` 合并。
2. **单卷 ≤ 60,000 字节**：超过 65,535 会**静默失索引**，不报错。
3. **必须用 zip 输出**：裸 Markdown 没有 `page_idx`、没有块类型。
4. **跨页表不合并**：一张跨页表会在相邻两卷各出现半张。
5. **公式部分阅读器显示源码**：语义检索不受影响，只是排版难看。
6. **严禁删除已索引目录重传**：用 `mv` 保留索引。详见 `SKILL.md` 规则 9 的事故记录。

### 许可证

[MIT](LICENSE) © 2026 wangnl
