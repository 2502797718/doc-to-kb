---
name: doc-to-kb
description: 把技术文档 PDF（芯片手册 RM/DS/勘误表/应用笔记、行业标准规范原文 IEC/GB/EN/DL-T、协议规范）转写成
  按章节分卷的 Markdown 并入库 OpenViking（芯片→resources/shared/chips/，标准规范→resources/shared/specs/），
  供后续按功能语义检索。触发场景：用户说"把这本手册/这份标准传进知识库""重新导入这个标准""之前入库的质量太差要重做"
  "转写这份 PDF""建芯片资料库"，或需要查外设寄存器/引脚复用表/位域定义、查标准条款与限值而库里只有原始 PDF 时。
  主路用 MinerU（VLM 端到端，表格转 Markdown、公式转 LaTeX），几何重建仅作兜底；**无书签 PDF 不再报错**，
  自动回退到按 MinerU 标题块分卷，纯扫描件走 OCR。
---

# 技术文档转写入库（doc-to-kb）

> **一句话定位**：把 PDF 文档变成「按章节分卷、表格可查、带页码引用」的 Markdown，再入库 OpenViking。
> **两种落点**：
> - 芯片手册 → `viking://resources/shared/chips/<chip>/<manual>/`
> - 标准 / 规范原文 → `viking://resources/shared/specs/<组织>-<标准号>-<版本>/`
>
> **主力是 MinerU**（VLM 端到端出表），自研几何重建降级为兜底；AI 只做抽样复核，不再全量读图。

## 何时用

- 拿到新的**芯片手册** PDF（RM/DS/勘误表/AN），想让它能被 OpenViking 语义检索；
- 拿到**标准 / 规范原文** PDF（IEC、GB/T、EN、DL/T 等），要能按条款检索；
- 库里**已有旧版但质量太差**（如逐页 OCR、无分卷、无 README、表格塌成文本），要用新管线重做替换；
- 库里只有原始 PDF，查寄存器 / 引脚复用 / 标准条款要翻页；
- 同一份资料被多个项目复用，需要一份共享副本（如 AT32F423 被 ADW610 三模块共用）。

## 不要用

- 只有一两页的零散 PDF —— 直接 `add_resource` 即可；
- ~~PDF 无书签目录 —— 依赖书签分卷，无书签会直接报错退出~~
  **（已过时，2026-11-11 更正）**：`postprocess.py` 早已内置 `build_toc_from_titles()` 回退——
  无书签时用 MinerU 的 `title` / `paragraph_title` 块自动构建章节树，并按标题编号深度
  （`4` → 1、`4.1` → 2、`4.1.1` → 3）推导层级，比 PDF 自带的 level 更可靠。
  实测成功案例：**DL/T 645-2007**（89 页、书签 0 条）、**IEC 61000-4-30**（70 页扫描件）、
  **GB/T 18216.12-2025**（103 页纯图像、书签 0 条，分出 33 卷）。
  只有当 MinerU 连标题块都没识别出来（极少见）时才需人工切分。
- 纯图片、无文字层的扫描件**可以**交给 MinerU（它带 OCR）；注意两点：
  `--ocr-mode ocr` 会更慢，且扫描件的无编号标题噪声大——脚本已用白名单关键词过滤
  （只保留 FOREWORD / INTRODUCTION / CONTENTS / Annex X / Bibliography 等，
  丢弃 IEC 广告页、版权页、页眉误判），无需人工干预。

---

## v2 变更：为什么改用 MinerU

v1 只用 `pymupdf + pdfplumber` 自研几何重建。在 **RN7326E/RN2026E V2 手册（317 页）** 上实测到两个硬伤：

| 指标 | v1 几何重建 | MinerU 4.1.3 |
|---|---|---|
| 表格列错位 | 1513 张表里 **291 张（19%）** | **0 处错列**（VLM 直接给 6 列正确表头） |
| 表头还原 | `\| \| 548H \| \| \| FESTA \| \| \| R \|…`（18 列、13 列空） | `\| 地址 \| 名称 \| R/W \| 有效字长 \| 复位值 \| 功能描述 \|` |
| 单元格折行 | 一句话被切成 3 行碎片 | **自动合并成完整句子** |
| 图片质量 | 761 张里 **634 张（83%）** 是页眉色条/logo | 版面元素带类型标记，噪音可定向过滤 |
| 公式 | 纯文本 | **LaTeX**（`$Y_{B,b}=\sqrt{\sum...}$`） |

实测同页对照（RN7326E p79 EMU 辅助寄存器表）：

```
v1: |  | 548H |  |  | FESTA |  |  | R |  |  | 4 |  |  | 0x0 |  |  | RMS合相基波视在能量寄存器 |  |
v2: | 548H | FESTA | R | 4 | 0x0 | RMS合相基波视在能量寄存器 |
```

⇒ 结论：**MinerU 主路，几何兜底**。几何路径的脚本全部保留，MinerU 不可用或结果退化时才启用。

### 两条路径怎么选

```text
                  ┌─ 有 mineru-kit 且模型就绪？ ──是──► MinerU 主路（推荐）
PDF ── deps_check ┤                                    mineru_parse.py → postprocess.py
                  └──────────────否──────────────────► 几何兜底（v1 老流程）
                                                       pdf_to_md.py（含 render_crops + apply_fixes）
```

判定命令：`python scripts/deps_check.py`。它同时探测两条路径，**MinerU 缺失只 WARN 不阻断**。

---

## 参考环境（2026-11-11 实测，Windows 11 + NVIDIA GPU）

> 开工前先看这一节，能省掉一轮「明明装了却以为没装」的折腾。
> 下表是**作者机器上的实测值**，供对照；你自己的路径用 `mineru-kit models show` 查。

| 项 | 实测值 |
|---|---|
| `mineru-kit` | `<mineru-install>\.venv\Scripts\mineru-kit.exe`，**4.1.3** |
| `mineru-kit` 查找顺序 | `--mineru-kit` 参数 → **`MINERU_KIT` 环境变量** → `PATH` → 常见安装目录兜底 |
| 模型目录 | `~/.mineru/models/MinerU-4_models_onnx/`（226MB） |
| 模型内容 | `Layout/PP-DocLayoutV2` + `OCR/paddleocr` |
| 生效后端 | **onnx**（`model.small_backend=auto` → 实际 onnx，`onnx_device=gpu`） |
| VLM 引擎 | llama-cpp（`model.vlm.engine=auto`） |
| GPU | NVIDIA RTX 5060 / 8GB（onnx 后端可用 CPU 跑，只是慢） |
| Python | 3.10+ 均可（实测 3.14.7） |
| pymupdf / pdfplumber | 1.28.2 / 0.11.10 |

### ⚠️ `deps_check.py` 的 WARN 是误报（别被吓到）

`python scripts/deps_check.py` 会打印：

```
[WARN] 模型 standard MinerU-4_models_onnx: missing key paths:
```

**这条可以忽略**，原因有三：

1. 它把**全部 4 个可选 repo**（torch 版、GGUF VLM 版等）都当判据，而本机实际只走 **onnx** 一路；
2. `mineru-kit models show` 的真实结论是 `Effective small backend: onnx`，onnx 的
   `Layout` + `OCR` 主权重**都在**（226MB 实测存在）；
3. `mineru-kit models verify` 对 onnx 报缺的是
   `MFR/`（公式）、`Table/`（表格）、`seal_*`（印章 OCR）这些**可选增强组件**，
   主路（版面 + 文本 OCR）不依赖它们。

**判定「到底能不能跑」的正确方法**（按这个顺序看，别只看 deps_check）：

```bash
mineru-kit models show     # 看 "Effective small backend" 和 "Model tiers"
mineru-kit models verify   # 看缺的是不是只在 MFR/Table/seal
```

只要 `Effective small backend: onnx` 且 `Layout/` + `OCR/` 有文件，就能跑。
**实测证据**：GB/T 18216.12-2025（103 页纯扫描件）用这套环境一次跑通 33 卷。

### `--tier standard` 与 GGUF 的关系（易误解）

`mineru_parse.py` 默认 `--tier standard`。而 `models show` 显示：

```
Model tiers:
  basic:    MinerU-4_models_onnx
  standard: MinerU-4_models_onnx, MinerU2.5-Pro-2605-1.2B-GGUF
```

`standard` 名义上多要一个 GGUF VLM 模型（本机显示 missing），但 **onnx 小模型路径并不加载那个 GGUF**，
所以 `--tier standard` 实际跑的就是 onnx。若某天真报 `quality_tier_unavailable`，再退回 `--tier basic`。

---

## 流程（7 步，逐步验证再往下）

### 步骤 1 · 依赖自检

```bash
python scripts/deps_check.py
```

输出分两段：`[几何兜底路径] 硬依赖` 与 `[MinerU 主路径] 推荐`。
只有前者缺失才阻断。MinerU 缺失时给出安装命令与 `MINERU_HOME` 提示。

### 步骤 2 · 结构勘察（只读，不写文件）

**MinerU 主路**先确认 PDF 有书签（分卷依赖它）：

```bash
python scripts/pdf_to_md.py --probe <pdf>
```

必看 3 个数字，任何一个异常就停下来问用户：

| 输出项 | 判读 |
|---|---|
| 书签条数 | **0 条不再停手**：自动回退到按 MinerU 标题块分卷（见「不要用」小节）；RN7326E 实测 582 条，走书签主路 |
| 分卷数 / 超限卷 | 超限卷必须为 0 |
| 栅格网格页 | **仅几何兜底路径关心**；MinerU 主路可忽略 |

### 步骤 3 · MinerU 解析

```bash
python scripts/mineru_parse.py --pdf <pdf> --out <out_dir> [--tier standard] [--pages all]
```

要点（都是实测结论）：

- **默认 `--format zip`**：zip 内含 `middle_json.json`（带 `page_idx` + block 类型）与 `images/`。
  裸 markdown 没有页码也没有块类型，**无法去噪音、无法按页分卷**。
- `--pages all` 是默认；调试时可 `--pages 78-80` 只跑几页（实测 3 页 2.3 秒）。
- 速度实测：**约 1.2–1.3 页/秒**（3 页 2.3s；317 页约 6 分钟；44 页约 158 秒）。
- `MINERU_HOME` 若设置会被透传，用于复用别处的模型目录。

产出：`<out_dir>/raw/unpacked/middle_json.json` + `images/`。

> ⚠️ 首次使用要先起服务：`mineru server start`，并把本地解析打开：
> `mineru config set parse_server.local.mode managed`。
> 默认是 `disabled` 且指向 `mineru.net` 远程，无 API key 会报
> `quality_tier_unavailable`。

#### 3.1 **超过约 400 页必须分段**（PIT-022）

实测 `RM_AT32F423`（495 页）一次全本解析**会卡死**：30 分钟零输出，
服务端日志 96 分钟无新行，但进程 CPU/GPU 仍显示忙碌（这两个信号**不可靠**）。

**判据**：看 mineru 服务端 stderr 日志的 mtime —— 长时间不更新 = 卡死。

分段跑法（每段 ≤165 页，实测最稳）：

```bash
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg1 --pages "1-165"
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg2 --pages "166-330"
python scripts/mineru_parse.py --pdf <pdf> --out <out>/seg3 --pages "331-495"
python scripts/merge_segments.py --out <merged> <out>/seg1 <out>/seg2 <out>/seg3
```

`merge_segments.py` 按 `page_idx` 排序合并 `middle_json` 并合并 `images/`，
产出可直接喂给 `postprocess.py` 的完整目录（会写 `_segments.json` 记录分段来源）。

实测：495 页 → seg1 3min / seg2 17min（GPIO·IOMUX·定时器表格密集区）/ seg3 6min，
合并后 page_idx 0-494 无缺口、869 图。

### 步骤 4 · 后处理与分卷

```bash
python scripts/postprocess.py --pdf <pdf> --out <out_dir> --chip <chip> --manual <manual>
```

它做四件事：

1. **按 block 类型去噪音**：丢弃 `header` / `footer` / `page_number` / `aside_text` / `page_footnote`。
   实测 3 页就滤掉 6 个 header + 3 个 page_number；试点 44 页曾达 147/762（19%）。
2. **HTML 表转 Markdown 管道表**，行列对齐，`rowspan/colspan` 做语义降级。
3. **按 PDF 书签切分**，单卷 ≤ 60,000B，超限下钻子书签、仍超则按页硬切。
4. 生成 `README.md`（章↔文件↔页码）与 `_manifest.json`。

产出：`NN_<章名>.md` + `README.md` + `_manifest.json` + `_postprocess_report.json`。

### 步骤 5 · 四关自检

```bash
python scripts/verify_md.py <out_dir>
```

| 关 | 内容 | 通过判据 |
|---|---|---|
| 关1 体积 | 每卷 UTF-8 字节 ≤ 上限 | 0 卷超限（超限会**静默失索引**） |
| 关2 覆盖 | 页码区间连续、无断档 | 无 FAIL |
| 关3 表格 | 每张 Markdown 表行内列数一致 | 无 FAIL |
| 关4 闭环 | 待处理标记计数 | MinerU 主路应为 0（无交接块） |

### 步骤 6 · 抽样复核（**从「全量读图」降级为「抽样」**）

MinerU 主路通常**不需要**逐张读图。只在下面情形抽查：

| 触发情形 | 抽查什么 |
|---|---|
| `_postprocess_report.json` 里 `table_empty > 0` | MinerU 偶发丢表，读对应页截图补 |
| 表格块数明显少于预期（对照 `--probe` 的表格页数） | 抽样 3–5 页核对 |
| 卷内出现 `<table>` 残留 | 说明 HTML 转换异常，需修脚本 |

需要截图时仍用兜底脚本：

```bash
python scripts/render_crops.py --pdf <pdf> --out <out_dir> --from-manifest <out_dir>/_review/manifest.json
```

回写用 `apply_fixes.py`（dry-run 先确认 `applied=N missed=0`，再正式写）。

> **几何兜底路径**（MinerU 不可用时）仍需 v1 的全量复核：`TABLE_NEEDS_AGENT` / `TABLE_SUSPECT`
> 两类标记逐张读图重写，闭环判据是关4 计数归零。

### 步骤 7 · 入库

```bash
# 目标：viking://resources/shared/chips/<chip>/<manual>/
ov add-resource <out_dir> -p viking://resources/shared/chips/<chip>/<manual>
```

- 单批 ≤ 100 条，超出分批；
- **`wait=false`**（库内 R3 铁律：`wait=true` 极易超时，而文件其实已写入）；
- 入库后 `read` 回读 2~3 卷核验，再 `find "<外设名> 寄存器"` 验证检索命中。

#### ⛔ 图片必须用 `viking://` 绝对 URI（2026-11-01 四写法实测）

**OpenViking Studio 的 Markdown 渲染器只认 `viking://` 绝对 URI**，
相对路径一律渲染成普通链接文字，图片显示不出来。

同一文件同一张图，四种写法实测结果：

| 写法 | 结果 |
|---|---|
| `![](images/page_28_image_10.jpg)` | ❌ 显示成 `[page_28_image_10.jpg]` |
| `![](images/images/page_28_image_10.jpg)` | ❌ 同上 |
| `![](../images/page_28_image_10.jpg)` | ❌ 同上 |
| `![](viking://resources/.../images/x.jpg)` | ✅ **正常渲染** |

**所以 `postprocess.py` 必须带 `--viking-base`**：

```bash
python scripts/postprocess.py --pdf <pdf> --out <out> \
  --chip AT32F423 --manual RM_V2.04_CH \
  --viking-base viking://resources/shared/chips/AT32F423/RM_V2.04_CH/
```

生成 `![图 1-1 系统架构](viking://resources/shared/chips/AT32F423/RM_V2.04_CH/images/page_21_image_5.jpg)`。
不给 `--viking-base` 会在 stderr 警告（相对路径在 OV 里显示不出图）。

**注意**：MinerU 的 `image_path` **自带 `images/` 前缀**，脚本已在 `_flatten_content()`
出口统一剥掉，避免出现 `images/images/` 双层。历史上因漏剥导致
AT32 RM 206 处 / RN7326E 37 处 / DS 36 处 / AN 29 处全是双层路径，图片全瞎。

#### 已入库手册的补救脚本（不用重新解析/上传）

| 脚本 | 用途 |
|---|---|
| `scripts/fix_img_paths.py` | 把 `](images/…)` → `](viking://…/images/…)`。默认 dry-run，加 `--apply` 写盘 |
| `scripts/fix_iec_img_links.py` | 把 IEC 那种 `> 原图见原册 p.N（images/x.jpg）` 注释式引用改成真图片语法，alt 自动取紧邻 Figure 标题 |

两个脚本都**直接改磁盘 md**，不走 `add-resource` → **不触发重新索引、不产生 VLM 费用**。
实测 OpenViking `read` 直接读磁盘文件，改完立即生效。

> 改前务必先备份 md 目录（脚本不改图片，只改文本，备份体积很小）。

---

## 产出目录约定

```text
<out_dir>/
├── 00_<封面与目录>.md
├── 01_<章节名>.md … NN_<章节名>.md
├── README.md                       章↔文件↔页码对照
├── _manifest.json                  卷清单（供 verify_md.py）
├── _postprocess_report.json        噪音过滤与块统计
├── _mineru_meta.json               MinerU 调用参数与版本
└── raw/
    ├── *.zip                       MinerU 原始产物
    └── unpacked/
        ├── middle_json.json        ★ page_idx + block 类型，分卷去噪的依据
        ├── markdown.md
        └── images/                 原图
```

**入库落点**（按文档类型二选一）：

| 文档类型 | 落点 | 目录命名 |
|---|---|---|
| 芯片手册 | `viking://resources/shared/chips/<chip>/<manual>/` | `<chip>` 用型号大写，`<manual>` 用 `RM_V2.04_CH` 式 |
| 标准 / 规范原文 | `viking://resources/shared/specs/<组织>-<标准号>-<版本>/` | `<组织>-<标准号>-<版本>`，如 `IEC-61000-4-7`、`GB-T-18216-12-2025` |

标准目录命名硬规则（详见 `specs/README.md`）：组织大写、标准号用**连字符**（`61000-4-7` 不用下划线）、
版本用 `-YYYY`；**禁止**下划线混用（旧 `IEC_61000-4-15`）、中文名混入、`.pdf` 后缀。
理由：语义检索里 `iec61000-4-7` 与 `IEC_61000-4-7` 不会自动归一。

---

## 硬规则（每条都有实测代价）

1. **单卷 ≤ 60,000B**。65,535B 是向量库单字段硬限，超了整条记录**静默失索引**（不报错）。
2. **优先 MinerU，不优先自研几何**。实测几何路径 19% 表格列错位，MinerU 为 0。
3. **必须用 zip 输出**。裸 markdown 无 `page_idx` / block 类型，无法去噪与分页。
4. **必须按 block 类型过滤 `header` / `footer` / `page_number`**。试点实测 19% 是这类噪音。
5. **低置信度不硬猜**（仅兜底路径）。MinerU 丢表时出 `TABLE_NEEDS_AGENT`，不伪造。
6. **脚本不进 OpenViking 库**。可执行文件一律走客户端 skill；库内只放产出的 `.md`。
7. **页码一律写原册页码**，不用 PDF 页序 —— 两者可能偏移。
8. **必须闭环**：交付前 `verify_md.py` 关4 必须 PASS（待处理标记归零）。

### ⛔ 规则 9：目录结构不对时，**只许整目录 mv，严禁 rm 重传**（2026-11-01 事故）

**事故经过**：AT32 RM 495 页手册的 869 张图 + 66 卷 md **已完整上传并索引成功**
（2,145 条目 / 5,554 条 embedding）。仅因目录多了一层（`RM_final/`、`p1..p3/`），
我选择 `ov rm --recursive` 删掉整个目录再重传 —— 浪费了全部索引工作 + 135.8MB 带宽，
并让用户承担了实际费用。**这是不可接受的浪费，不是「效率优化」。**

**必须遵守**：

| 情形 | 正确做法 | 禁止做法 |
|---|---|---|
| 目录多一层（`A/B/` 应为 `A/`） | `ov mv <A/B> <A/B_tmp>` → `ov rm <A>`（空壳）→ `ov mv <A_tmp> <A>` | ❌ `ov rm <A> --recursive` 后重传 |
| 图片散在多个目录 | **整目录** `ov mv <p1> <images>`（一次操作） | ❌ 逐文件 mv（869 个会超时）<br>❌ 删除后重传 |
| 目录名带 `-upload`/`_final` 后缀 | `ov mv` 整目录改名（**保留全部索引**） | ❌ 删除重传 |

**根本预防 —— 上传前 3 秒自检 staging 结构**：

```bash
ls <staging_dir>     # 顶层必须是 NN_*.md ... + images/，不允许有嵌套目录
```

**staging 目录名必须等于目标子目录名**：

```bash
# 目标 viking://resources/shared/chips/AT32F423/RM_V2.04_CH
staging/RM_V2.04_CH/          ← 目录名必须叫 RM_V2.04_CH
├── 00_xxx.md
└── images/
```

这样 `ov add-resource staging/RM_V2.04_CH -p .../AT32F423` **一次到位**，
不会产生 `RM_final/` 这类多余层，也就不存在「要不要删了重传」这个选项。

**决策闸门**：任何「删除已索引内容」的动作，先自问三句：

1. 这目录里的东西**是否已索引**？（已索引 = 有沉没成本，绝不能删）
2. 能否用 `ov mv` 达成同样结果？（99% 能）
3. 若只能删，**是否已向用户说明损失并获批准**？

三问答不出「必须删」，就**不许删**。

### ⛔ 规则 10：图片一律用 `viking://` 绝对 URI（2026-11-01 实测）

OpenViking 渲染器**只认 `viking://` 绝对 URI**；相对路径一律渲染成普通链接文字。
`postprocess.py` 必须带 `--viking-base`，详见「步骤 5 · 入库」小节的四写法实测表。

同时 **MinerU 的 `image_path` 自带 `images/` 前缀**，拼接前必须剥掉，否则出现
`images/images/` 双层 → 图全瞎（历史影响 AT32 RM 206 / RN7326E 37 / DS 36 / AN 29 处）。

**上传前自检**（3 秒，能省掉一轮返工）：

```bash
# 1) 有没有相对图片路径？（应为 0）
grep -c '](images/' <out_dir>/*.md
# 2) 有没有 images/images 双层？（应为 0）
grep -c 'images/images/' <out_dir>/*.md
# 3) viking 绝对引用数是否等于图片块数？
grep -c '](viking://' <out_dir>/*.md
```

**已入库的补救**：`fix_img_paths.py` / `fix_iec_img_links.py`，直接改磁盘 md，
不重新索引、不产生费用（详见「步骤 5 · 入库」）。

---

## 已知边界（诚实声明）

- **MinerU 只在两类手册上验证过**：RN7326E/RN2026E V2（317 页，文字层）与 IEC 61000-4-7（44 页，
  扫描件）。换厂商仍需先 `--probe` + 小样本试跑（`--pages 1-5`）。
- **表格偶尔丢**：MinerU 会把个别表格块标成 `table` 但 `content` 为空；本流程计入
  `table_empty` 供抽样复核，**不自动伪造**。
- **跨页表**仍未合并，会在相邻两卷各出现半张。
- ~~**无书签 PDF 不支持**，直接报错退出，需人工切分。~~
  **（2026-11-11 更正）** 无书签 PDF **已支持**：走 `build_toc_from_titles()` 标题块回退。
  实测 DL/T 645-2007、IEC 61000-4-30、GB/T 18216.12-2025 三份零书签件均成功分卷。
  只有 MinerU 连标题块都没识别出来时才需人工切分。
- **MinerU 4.x 与 OV 的 mineru 后端协议不兼容**：OV 0.5.2 的 `_convert_mineru` 期望 2.x/3.x
  同步 API 格式，4.x `/v1/file_parse` 返回二进制附件。本流程**不经过 OV 的 mineru 后端**，
  而是自己调 CLI + 落盘 + `ov add-resource`，绕开该不兼容。
- 依赖 `pymupdf + pdfplumber`（几何兜底）+ `mineru-kit`（主路），跨机使用前先跑 `deps_check.py`。

---

## 与其它 skill 的分工

| skill | 管什么 | 关系 |
|---|---|---|
| **`doc-to-kb`（本 skill）** | 手册 / 标准 PDF → 分卷 md → 入库 | 资料**生产** |
| `at32-manual-lookup` | 查 AT32 手册：MUX 编号 / IRQn / 时钟宏 | 资料**消费**；入库后它可一次 `find` 命中 |
| `mcu-module-dev` | MCU 模块开发四阶段 | 需要「找不出处」时引用上面两个 |
| `openviking-skills` | skill 的安装/共享/删除 | 本 skill 的**上传**走它 |

---

## 目录说明

```text
doc-to-kb/
├── SKILL.md                      本文件：入口 + 7 步流程 + 硬规则
├── scripts/
│   ├── deps_check.py             双路径依赖自检（MinerU + 几何）；判「实际生效后端」而非全部可选 repo
│   ├── mineru_parse.py           ★ 主路：调 MinerU，出 zip + 解包 middle_json
│   ├── merge_segments.py         ★ 主路：合并分段 middle_json + images（>400 页必用）
│   ├── postprocess.py            ★ 主路：按 block 类型去噪 + HTML 表转 MD + 分卷
│   │                               （有书签走书签树；无书签走 build_toc_from_titles 标题块回退）
│   ├── fix_img_paths.py          ★ 补丁：相对图片路径 → viking 绝对 URI（已入库手册可用）
│   ├── fix_iec_img_links.py      ★ 补丁：注释式图片引用 → 真图片语法（IEC 类标准用）
│   ├── pdf_to_md.py              兜底：勘察 / 几何分卷生成 / 表格几何重建
│   ├── table_rebuild.py          兜底：表格重建（栅格定列 + 行带定行 + 置信度打分）
│   ├── render_crops.py           兜底：表格区域 300dpi 截图（供 AI 读图）
│   ├── bitfield_parse.py         兜底：寄存器位域表专用解析器
│   ├── apply_fixes.py            兜底：闭环回写（agent 重写结果 → 写回 md）
│   └── verify_md.py              四关自检（体积 / 覆盖 / 表格结构 / 闭环）
├── references/
│   ├── pipeline.md               详细算法：两条路径的原理与参数依据
│   ├── pitfalls.md               踩过的坑（每条附实测数据）
│   └── output_layout.md          产出目录约定与 viking:// 落点规则
└── templates/
    └── README_index.md           目录导航页模板（芯片 / 标准通用）
```

---

## 已知限制（诚实声明，2026-11-01）

### 限制 1：`table` / `equation` 截图不写进 md（设计使然，非缺陷）

MinerU 会把**表格**和**公式**也截图成 JPG，但 `postprocess.py` 只把 `image` / `chart`
类块写成 `![](...)`。实测各手册引用数 ≈ `image + chart` 数量：

| 手册 | 总图 | 引用 | table | image | equation | chart |
|---|---|---|---|---|---|---|
| AT32 RM | 869 | 206 | 621 | 207 | 22 | 19 |
| RN7326E | 601 | 37 | 496 | 39 | 61 | 4 |
| DLT645 | 150 | 14 | 126 | 14 | 10 | 0 |
| IEC-4-7 | 71 | 30 | 2 | 9 | 39 | 21 |

**为什么不引用**：md 里已有结构化 Markdown 表（可向量检索）和 LaTeX 公式（AI 可读），
引用截图反而让 AI 优先看到图片描述而非表格数据；历史教训是「conf=1.00 但内容错乱的表格图」
入库后被当事实用，危害大于收益。

**这些截图保留在 `images/` 里作为人工复核的原始凭证，但不在 md 中引用。**

### 限制 2：同页多小节切分时，`image` 块可能被裁掉（已确认 1 例）

**现象**：DL/T 645-2007 的 `5.3.2 传输次序` 写着「其传输次序如图 9」，
但 `image` 块（`page_12_image_1.jpg`，图9 传输次序图）**没进 md**。

**根因**：`slice_page_blocks()` 按 bbox y 切分同页多小节时，图片块被归到了
相邻小节或被边界裁掉；`image_body` 的 `image_path` 与 `image_caption` 的图注
在 content 数组里分属两个 item，切分时可能只留一个。

**已验证 `_flatten_content()` 本身没问题**（对该块直接调用能正确返回
`('图9 传输次序图', 'page_12_image_1.jpg')`），问题在切分阶段。

**现状**：仅发现 1 例，已手工补图。**未修 `slice_page_blocks`**（改动核心算法风险高，
且该问题只在「同页多小节 + 该页有图」时触发，概率低）。

**人工兜底**：上传后扫一遍「如图 N / Figure X 但 md 里无对应 `![](` 」的段落：

```bash
# 粗筛：提到图但该卷没有图片引用的文件
grep -l '如图 \|Figure ' <out_dir>/*.md | xargs grep -L '!\['
```

### 限制 3：公式不渲染（OpenViking 渲染器限制）

OpenViking Studio 打包了 katex，但**只给 mermaid 用**，markdown 正文未接入
`remark-math` / `rehype-katex`，所以 `$...$` 显示成源码。

**对检索无影响**（向量化看文字，`\sum`/`\sqrt` 照样能匹配语义），仅影响人读排版。
2026-11-01 经用户确认：**暂不处理**。
