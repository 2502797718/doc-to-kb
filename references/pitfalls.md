# 踩坑清单

> PIT-001 ~ PIT-015 来自 **AT32F423 RM V2.04（495 页）** 几何重建路径的实际调试。
> PIT-016 ~ PIT-021 来自 **v2 引入 MinerU** 后的实测（RN7326E 317 页 / IEC 61000-4-7 44 页）。
> 下次转新手册前先扫一遍，能省大量调试时间。
>
> **先看这条结论**：能用 MinerU 就用 MinerU。几何路径的 PIT-002/003/004/005/007 这些问题
> 在 MinerU 路径下**根本不存在**（VLM 直接理解表格结构，不依赖画线推断）。

---

# 第一部分：MinerU 主路径（v2 新增，优先读）

## PIT-016 · MinerU 默认走远程、本地解析是关的

**现象**：直接 `mineru parse <pdf>` 报
`quality_tier_unavailable: No basic, standard, or advanced engine available`。

**根因**：`mineru config show` 显示 `parse_server.local.mode = disabled`，
`parse_server.remote.url = https://mineru.net/api` 但没有 `api_key`。
即：本地解析未启用，远程又没凭证。

**对策**：
```bash
mineru server start                        # 先起本地服务
mineru config set parse_server.local.mode managed
```
设完 `config show` 应显示 `local.mode = managed  [override]`。

---

## PIT-017 · 模型不完整时启动会静默失败

**现象**：`config set parse_server.local.mode managed` 报
`Local managed tier 'standard' requires model files that are not ready`，
并列出缺 `Table/slanet-plus.onnx`、`MFR/pp_formulanet_plus_m/...`、
`OCR/.../seal_PP-OCRv4_det_infer.onnx` 等。

**实测**：`<MINERU_HOME>/models/MinerU-4_models_onnx` 当时只有 Layout + 2 个 OCR 文件，
**Table（表格识别）和 MFR（公式识别）整个缺失** —— 而这两个正是我们要的能力。

**对策**：
```bash
mineru-kit models download MinerU-4_models_onnx --source modelscope
mineru-kit models verify --tier standard      # 必须两个仓库都"正常"
```
只下缺的那个仓库可以复用已有的 GGUF（实测 GGUF 1.2GB 已在本地，无需重下）。

---

## PIT-018 · 多个 mineru 服务争抢同一端口，端点文件错乱

**现象**：反复出现
`The discovered endpoint belongs to a different MinerU server instance`，
`config show` 也读不出来。

**根因**：多次 `server start` + `restart` 留下 **多个 `mineru.doclib.app` 进程**
（实测同时存在 4 个：PID 90012/85512/88540/134352，其中 134352 占着 15980 端口），
端点文件 `doclib.endpoint.json` 的 `pid` 与真实监听者不一致。

**对策**：全杀干净再单独起一个：
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'mineru|doclib' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item <MINERU_HOME>\doclib.endpoint.json,<MINERU_HOME>\doclib.lock -Force
mineru server start
```
**判据**：`Get-NetTCPConnection -LocalPort 15980` 的 `OwningProcess`
必须等于 `doclib.endpoint.json` 里的 `pid`。

---

## PIT-019 · 裸 markdown 输出无法分卷、无法去噪

**现象**：`mineru-kit parse --format markdown` 出来的 md 里**没有页码、没有块类型**，
既不能按书签对齐，也不能区分正文与页眉。

**实测**：3 页 markdown 输出里 `Page \d+` 匹配 0 次、`![]` 图片引用 0 次。

**对策**：**必须用 `--format zip`**。zip 内含：
- `middle_json.json`：`pages[].page_idx`（真实页码）+ `pages[].blocks[].type`
- `images/`：原图

有了 `page_idx` 就不再需要「在裸文本里猜页码」这类启发式。

---

## PIT-020 · 页眉/页脚/logo 占图片总数 83%

**现象**：v1 路径下 RN7326E 产出 761 张 PNG，逐张看发现大量重复。

**实测**：
- `page17_img1.png` = **2024×29 px / 1260 B** 的紫色装饰条 → 317 张
- `page17_img2.png` = **346×114 px / 5001 B** 的「锐能微 RENERGY」logo → 317 张
- 合计 **634 张（83%）** 是版面噪音，真正有内容的图只有 127 张

**对策**：MinerU 的 `middle_json` 会把它们标成 `header` / `page_number` 块，
直接按类型过滤即可（实测 3 页滤掉 6 header + 3 page_number）。

---

## PIT-021 · `content` 是多形态的，直接 `.strip()` 会崩

**现象**：`AttributeError: 'list' object has no attribute 'strip'`。

**根因**：`middle_json` 里 `blocks[].content` 不是字符串，而是**列表**，且元素形态不一：
```jsonc
// header：一段文字
[{"type":"text","content":"锐能微RENERGY"}]
// table：HTML 字符串
[{"type":"table","index":2,"bbox":[...],"image_path":"images/x.jpg","content":"<table>...</table>"}]
// 个别 table 是嵌套列表（实际是"表格块外壳+普通正文"）
[{"type":"table","index":4,"content":[{"type":"text","content":"写入条件:..."}]}]
```
另外 `content` 为空的 table 块确实存在（MinerU 偶发丢表）。

**对策**：写一个递归 `_flatten_content()` 统一归一并顺带取 `image_path`；
对空 content 的 table 计入 `table_empty` 交抽样复核，**不要伪造**。

---

# 第二部分：几何兜底路径（v1，MinerU 不可用时）

> 以下 PIT-001 ~ PIT-015 是几何路径特有的问题；走 MinerU 时不必关心。

---

## PIT-001 · 直传 PDF 会灌爆 media

**现象**：把 PDF 直接 `add_resource`，OV 的 PDFParser 把 `page.images` 逐个 crop 渲染成 PNG。

**实测**：全书 `page.images` = **25,806** 个；p95 单页 **1,410** 个，
其中 bbox 仅 `0.5×0.1pt` —— 这些是 Word 表格线/下划线的矢量碎片，不是插图。
渲染一张 300dpi PNG 约 1.7KB，25806 张 ≈ 40MB 级 + 25806 条图片引用。

**对策**：**不导图片**。正文留 `> 原图见原册 p.NNN 图N-N` 占位。

---

## PIT-002 · `extract_tables()` 在这类 PDF 上返回 0 张表

**现象**：`pdfplumber.extract_tables()` 与 `pymupdf.find_tables()` 对关键页全部返回 0 张表。

**实测**：p95 / p96 / p173 / p50 四页，两个库都是 **0 张**。

**对策**：必须自行重建（见 `table_rebuild.py`）。
**不要**因为「库说没有表」就认为该页无表 —— 它是认不出来，不是不存在。

---

## PIT-003 · 网格线是栅格碎片，不是矢量线

**现象**：`get_drawings()` 找竖线返回 **0 条**，看起来「表格没有竖线」。

**实测**：p95 的 1410 张图片里，**1152 张**是 `0.5×2.4pt` 细高碎片，
x 坐标聚类后恰好 8 条：`87.1, 134.5, 195.8, 257.1, 318.3, 388.4, 441.0, 511.1`。

**对策**：用 `get_image_info()` 的 bbox 判断「细高碎片」定竖线、「细宽碎片」定横线。
**判据**：宽 <2.0pt 且高 >=2.0pt → 竖线碎片；高 <2.0pt 且宽 >=2.0pt → 横线碎片。

---

## PIT-004 · 8 条竖线是「内部边界」，不是 8 列

**现象**：8 条竖线，若直接当 8 列会整体错一列。

**实测**：该表**无外框线**。8 条内部边界 → **9 列**，需补左右外边界
（取区域内文字的 x 覆盖范围 ±6pt）。

**对策**：`grid_col_bounds()` = `[left] + inner + [right]`。

---

## PIT-005 · 表格缺外框线导致表头并进首行

**现象**：首条横线在 y=57.5（正文顶），第 2 条在 y=131.9。
首块 `[57.5, 131.9]` 高 **74pt**，同时容纳「表标题 / 表头 / PD0」三行；
而该表数据行高仅 **23pt**。

**后果**：表头与首行数据混在一起，置信度判据（首行应横跨多列）失效。

**对策**：组内首块高度 > 中位行高 **1.8 倍** 时，按中位行高从底部向上重切，
再剥掉空行与表标题行（`fix_oversized_first_band` + `strip_leading_non_table_bands`）。

---

## PIT-006 · 折行拼接写成 `and/or` 三目，永远失败

**错误写法**：

```python
text = text + (cond and "" or " ") + t     # ← 空串 falsy，永远落回空格
```

**后果**：`SPI2_CS/I2S` + `2_WS` 拼成 `SPI2_CS/I2S 2_WS`（多一个空格），
表格内容与手册原文不一致。

**正确写法**：

```python
sep = "" if _join_hint(text, t) else " "
text = text + sep + t
```

---

## PIT-007 · 续行合并判据过松导致相邻数据行粘连

**现象**：p95 输出 `PD4 PD5` 粘在同一行。

**根因**：早期判据是「首列短（<=5字符）或空，且非空格数 <=2」。
`PD5` 只有 3 字符，命中「首列短」，被当成 PD4 的续行。

**对策**：续行**必要条件必须是「首列为空」**。首列有值 = 本行独立数据。

---

## PIT-008 · 正文与重建表重复输出

**现象**：首版既输出整页文字、又输出重建表，体积翻倍。
**实测**：55 卷里 **9 卷**因此超限（最大 78.7KB）。

**对策**：先用 TableBlock 的 y 区间算出「表格占用区」，
再用 `page.get_text("blocks")` 逐块过滤掉与表格区间重叠的块。

---

## PIT-009 · 按章切分必超 65,535B 硬限

**实测**：

| 粒度 | 结果 |
|---|---|
| 1 级章 | TIMER **236KB**、OTGFS **187KB**、I2C **70KB** |
| 2 级 | 6 块超 45KB（TMR1 72KB、OTGFS 寄存器 102KB…） |
| 最深书签 | 363 块，仅 1 块超限（封面+目录 116KB） |

**对策**：按最深书签递归切分。**不要**按章切。

---

## PIT-010 · end 页推导错误导致虚假超限

**现象**：4 个卷报超限，最大 746KB。
**根因**：末节点 end 一律取「全书末页」。
**实测**：`11.7.11 发送寄存器（I2C_TXDT）` 只有 1 页，被算成 p164–495。

**对策**：末节点 end = **父节点的 end**（继承父范围）。

---

## PIT-011 · 「收紧上限重跑」迭代无法收敛

**实测**：迭代 4 轮，超限卷 6 → 1 → 2 → 5，总卷数 55 → 185，始终无法收敛。

**根因**：书签已切到叶子，再收紧上限只增加卷数，不减小单卷体积。

**对策**：叶子卷超限时**按页区间硬切**（贪心多装页），只做一轮。

---

## PIT-012 · 「疑有表格」判据过松制造大量噪音

**实测**：松判据（>=3 列、>=6 词）产出 **670** 处 `TABLE_SUSPECT`，
真正需要复核的只有个位数 —— 噪音淹没信号。

**对策**：保守判据（>=4 列起始簇、>=3 行、<=60 行、横线 >=3 条、
每行词数 <=8）。宁可漏标，不制造噪音。

---

## PIT-013 · 截图 bbox 参数顺序

**错误**：`page.get_pixmap(clip=Rect(y0, y1, x0, x1))`
**报错**：`FzErrorArgument: code=4: Invalid bandwriter header dimensions/setup`

**对策**：`pymupdf.Rect` 顺序是 `(x0, y0, x1, y1)`，与 pdfplumber 的 bbox 语义一致。

---

## PIT-014 · 页码 ≠ PDF 页序

**实测**：AT32F423 RM 的正文从 PDF 第 22 页开始（前 21 页是封面/目录/前言）。
手册内印刷页码与 PDF 页序**偏移 21 页**。

**对策**：`pymupdf.get_toc()` 返回的页码是 PDF 页序（1-based），
产出里写「原册页码」时需注明口径。本流程统一用 PDF 页序，并在 README 声明。

---

## PIT-015 · 无书签 PDF 直接失败

**现象**：`build_toc_tree()` 返回空 → 脚本报 `[FAIL] 该 PDF 无书签` 并退出。

**为什么这样设计**：无书签时无法确定章节边界，硬切出来的 md 目录结构是「页」而非「章」，
入库后语义检索价值极低。**宁可失败并提示人工切分**，也不产出垃圾。

---

# 第三部分：v2 实战新增（2026-11-01，RN7326E / AT32F423 全量实测）

## PIT-022 · 大手册一次全本解析会卡死（495 页 / 30 分钟无输出）

**现象**：`RM_AT32F423_V2.04_CH.pdf`（495 页）用 `mineru_parse.py --pages all` 一次跑，
进程 CPU 持续增长、GPU 显示 95%，但**输出目录 30 分钟仍是 0 文件**，
mineru 服务端 stderr 日志从启动后 96 分钟**再无一行新输出**。

**判据（区分「在算」vs「卡死」）**：

| 信号 | 在算 | 卡死 |
|---|---|---|
| 进程 CPU | 持续增长 | 也增长（**不可靠**） |
| GPU 利用率 | 90%+ | 仍显示 95%（**不可靠**） |
| 服务端日志 mtime | 持续更新 | **长时间不更新** ← 决定性 |
| 输出目录文件数 | 逐步增加 | 恒为 0 |

**对策：分段解析**。每段 165 页：

```bash
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg1 --pages "1-165"
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg2 --pages "166-330"
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg3 --pages "331-495"
python scripts/merge_segments.py --out <merged> <out>/seg1 <out>/seg2 <out>/seg3
```

实测结果：seg1 约 3 分钟、seg2 约 17 分钟（p166-330 是 GPIO/IOMUX/定时器表格密集区）、
seg3 约 6 分钟；合并后 495 页完整（`page_idx` 0-494 无缺口）、869 图。

> 对比：RN7326E 317 页一次跑 6.2 分钟正常完成。**阈值大概在 400 页以上**，
> 超过就先分段，不要赌。

---

## PIT-023 · 截断后仍超限：提示语自己也占字节

**现象**：`verify_md.py` 关1 报 `28_11_I2C接口.md 60036 B > 上限 60000 B`，
超限只有 **36 字节**。

**根因**：截断逻辑先按 `args.limit` 切字节，然后**再拼接提示语**：

```python
safe = encoded[:args.limit].decode(...)     # 已经用满 60000
content = safe + "\n\n> ⚠️ 本卷超出单卷上限…\n"   # 又加了 36 字节 → 60036
```

**对策**：给提示语预留预算，并加硬切兜底：

```python
NOTE = "\n\n> ⚠️ 本卷超出单卷上限，已在完整行边界截断\n"
budget = args.limit - len(NOTE.encode("utf-8"))
safe = encoded[:budget].decode("utf-8", "ignore")
content = safe + NOTE
if len(content.encode("utf-8")) > args.limit:      # 兜底
    content = encoded[:args.limit].decode("utf-8", "ignore")
```

> 教训：凡是「先切到上限、再追加内容」的地方，都要把追加部分计入预算。
> 同类风险点：分卷头（YAML + H1 + 页码引用）也是先拼后写，但目前只拼一次、未超。

---

## PIT-024 · 分批上传会造出多余目录层

**现象**：`ov add-resource <staging_dir> -p <target>` 会把 **staging 目录名**当作子目录：

```
chips/AT32F423/RM_V2.04_CH/RM_md/       ← 我传 md 时的 staging 名
chips/AT32F423/RM_V2.04_CH/b1..b5/      ← 我传图片 5 批的 staging 名
```

导致图片散在 5 个目录、md 多一层，且 `images/` 引用路径对不上。

**对策（任选其一）**：

1. **让 staging 目录名 == 最终子目录名**（最省事）：
   `staging/RM_V2.04_CH/` 上传后正好落在 `chips/AT32F423/RM_V2.04_CH/`；
2. 传完用 `ov mv <旧目录> <新目录>` 整目录改名（**整目录移动是一次操作**，
   不要逐文件 mv —— 869 个文件逐条 mv 会超时）；
3. 分批不可避免时，先把 md 与 images 分别传到**同一层**再用 `ov mv` 归位。

> 本流程约定：staging 顶层目录名 = 目标目录名，避免事后搬移。

---

## PIT-025 · 扫描件 OCR 把广告页/页眉/正文句误判成 `paragraph_title`，分卷爆炸

**现象**（2026-11-03 实测 IEC 61000-4-30:2021，70 页纯扫描件）：

- `postprocess.py` 无书签回退到「按标题块分卷」
- 结果切出 **171 卷**，但 70 页平均每卷 < 0.5 页
- 最小卷只有 **142 B**，正文只有标题、没有内容：

```markdown
# A.3 Transducers          ← 整卷就这一行
```

**根因**：扫描件经 OCR 后，MinerU 把大量非章节内容识别为 `paragraph_title`：

| 类型 | 实测条数（共 362 标题） |
|---|---|
| **有编号**（真章节，如 `5.1 Power frequency`） | **125** |
| **无编号** | **237** ← 全是噪音 |

无编号那 237 条包括：
- IEC 官网广告页条目（`About the IEC` / `IEC Just Published` / `Electropedia` / `webstore.iec.ch`）
- 版权页（`THIS PUBLICATION IS COPYRIGHT PROTECTED`）
- 页眉（`INTERNATIONAL ELECTROTECHNICAL COMMISSION`）
- 正文句子被误判（`Same as Class A.` 之类）

而原逻辑「无编号一律 `depth=1`」，导致每个噪音块都成一级卷。

**对策**：无编号标题只在**白名单关键词**命中时才保留，其余丢弃：

```python
KEEP_UNNUM = re.compile(
    r"^(?:FOREWORD|INTRODUCTION|CONTENTS|Annex\s+[A-Z]|"
    r"Bibliography|Index|Figure\s+[A-Z]?\d+|Table\s+[A-Z]?\d+)\b", re.I)
DROP_UNNUM = re.compile(
    r"^(?:About\s+the\s+IEC|IEC\s+Just\s+Published|Electropedia|"
    r"A\s+propos|webstore\.iec\.ch|THIS\s+PUBLICATION|Copyright|"
    r"INTERNATIONAL\s+ELECTROTECHNICAL\s+COMMISSION|"
    r"ELECTROMAGNETIC\s+COMPATIBILITY|Part\s+\d+:)", re.I)

def depth_of(text):
    m = re.match(r"(\d+(?:\.\d+)*)\s", text) or re.match(r"(\d+(?:\.\d+)*)$", text)
    return 1 + m.group(1).count(".") if m else None   # None = 无编号

for t in merged:
    d = depth_of(t["text"])
    if d is not None:                    raw.append((d, t["text"], t["page"]))
    elif DROP_UNNUM.match(t["text"]):    continue      # 噪音，丢
    elif KEEP_UNNUM.match(t["text"]):    raw.append((1, t["text"], t["page"]))
    else:                                continue      # 其余无编号，丢
```

**效果**：171 卷 → **13 卷**，完全按标准章节组织：

```
00_CONTENTS  01_FOREWORD  02_INTRODUCTION  03_1_Scope
04_2_Normative_references  05_3_Terms_and_definitions
06_4_General  07_5_Power_quality_parameters
08_6_Performance_verification  09~12_Annex_A~D
```

**判据（什么时候需要担心）**：分卷数 > 页数 × 1.5，或中位卷体积 < 1 KB，就是标题误判了。

---

## PIT-026 · OpenViking 只认 `viking://` 绝对 URI，相对路径图片全瞎

**现象**（2026-11-01 四写法实测，同一文件同一张图）：

| 写法 | 渲染结果 |
|---|---|
| `![](images/page_28_image_10.jpg)` | ❌ 显示成 `[page_28_image_10.jpg]` 链接文字 |
| `![](images/images/page_28_image_10.jpg)` | ❌ 同上 |
| `![](../images/page_28_image_10.jpg)` | ❌ 同上 |
| `![](viking://resources/.../images/x.jpg)` | ✅ **正常渲染出图** |

**根因**：OpenViking Studio 的 Markdown 渲染器**只认 `viking://` 绝对 URI**，
相对路径一律当普通链接处理。

**连带坑**：MinerU 的 `image_path` 字段**自带 `images/` 前缀**
（实测 RN7326E / DLT645 / AT32 / IP101GRI 均如此），若再拼一次就成 `images/images/`。
历史影响：AT32 RM 206 处 / RN7326E 37 / DS 36 / AN 29 全是双层路径，图全瞎。

**对策**：

1. `postprocess.py` 带 `--viking-base`（必带）：
   ```bash
   --viking-base viking://resources/shared/chips/AT32F423/RM_V2.04_CH/
   ```
   脚本会生成 `![alt](viking://.../images/x.jpg)`。

2. `_flatten_content()` 出口统一剥 `images/` 前缀，杜绝双层。

3. 已入库手册补救（**直接改磁盘 md，不重新索引、不产生 VLM 费用**）：
   - `fix_img_paths.py`：相对路径 → viking 绝对 URI
   - `fix_iec_img_links.py`：`> 原图见原册 p.N（images/x.jpg）` 注释式 → 真图片语法

**上传前自检（3 秒）**：

```bash
grep -c '](images/' <out_dir>/*.md       # 应为 0
grep -c 'images/images/' <out_dir>/*.md  # 应为 0
grep -c '](viking://' <out_dir>/*.md     # 应等于图片块数
```

---

## 附：验证过的数字（供新手册对照）

### AT32F423 RM V2.04（几何兜底路径，2026-10-29）

| 指标 | 值 |
|---|---|
| 页数 | 495 |
| 文字层 | 699,435 字符 / 1,142,469 字节 UTF-8 |
| 书签 | 738 条（1级26 / 2级122 / 3级354 / 4级236） |
| 分卷 | 60 卷，0 超限，最大 <=60,000B |
| 栅格网格页 | 31 页（MUX 表 p89~99 全在内） |
| `page.images` 总数 | 25,806（p95 单页 1,410） |
| 表格重建 | MUX 表 conf=1.00，9 列 17 行全对 |

### AT32F423 RM V2.04（MinerU 主路，2026-11-01 重做）

| 指标 | 值 |
|---|---|
| 分段 | 3×165 页（全本卡死，见 PIT-022） |
| 合并后页数 | 495（page_idx 0-494 无缺口） |
| 图片 | 869 张 |
| 噪音过滤 | header 990 / footer 989 / page_number 496 = **2,475 块** |
| 表格块 | 621 |
| 分卷 | **66 卷**，0 超限（关1 修了 PIT-023 后 PASS） |
| 四关 | 全 PASS |

### RN7326E/RN2026E V2（MinerU 主路，2026-11-01）

| 指标 | 值 |
|---|---|
| 页数 | 317（一次跑通，6.2 分钟） |
| 噪音过滤 | header 634 / page_number 316 = **950 块** |
| 表格块 | 496；图片块 39（磁盘 601 张，含表格裁剪图） |
| 分卷 | **86 卷**，0 超限 |
| 四关 | 全 PASS |

### 扫描件 OCR 类（2026-11-03 实测）

| 手册 | 页数 | 大小 | 书签 | 分卷 | 噪音 | 备注 |
|---|---|---|---|---|---|---|
| **EN 50160:2022** | 53 | 22.58 MB | 0 | **49 卷** | 110 | 文本层 0 字节，OCR 质量良好；53 页约 3~5 分钟 |
| **IEC 61000-4-30:2021** | 70 | 46.06 MB | 0 | **13 卷** | 79 | 同上；标题误判见 PIT-025（修前 171 卷） |

### 有书签文字版（2026-11-03 实测）

| 手册 | 页数 | 书签 | 分卷 | 噪音 | 表格 |
|---|---|---|---|---|---|
| **IP101GRI Datasheet** | 66 | 56（10/31/15） | **10 卷** | 330 | 90 表 / 23 图 |
