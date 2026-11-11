# 转写流水线算法详解

> 本文件解释 `scripts/` 下各脚本**为什么这样设计**。
> §零 是 v2 新增的 MinerU 主路径；§一~§五 是几何兜底路径（v1），数值来自
> AT32F423 RM V2.04（495 页）实测。

---

# 零、MinerU 主路径（v2，默认走这条）

## 0.1 为什么换

v1 的几何重建在 RN7326E（317 页）上实测两个硬伤：

| 指标 | v1 几何 | MinerU 4.1.3 |
|---|---|---|
| 表格列错位 | 1513 张里 **291 张（19%）** | **0** |
| 表头还原 | 18 列、13 列空 | 6 列正确表头 |
| 单元格折行 | 一句话切成 3 行 | 自动合并成完整句 |
| 装饰图 | 761 张里 634（83%）是页眉/logo | 可按键类型过滤 |

v1 那张错列表的实际样子（p79 EMU 辅助寄存器）：
```
|  | 548H |  |  | FESTA |  |  | R |  |  | 4 |  |  | 0x0 |  |  | RMS合相基波视在能量寄存器 |  |
```
MinerU 对同一张表给的是：
```
| 548H | FESTA | R | 4 | 0x0 | RMS合相基波视在能量寄存器 |
```

## 0.2 两步链路

```
PDF
 ├─ mineru-kit parse --format zip
 │     ├─ middle_json.json   ★ pages[].page_idx + blocks[].type
 │     └─ images/
 ↓
 ├─ postprocess.py
 │     ① 按 block 类型丢 header/footer/page_number/aside_text/page_footnote
 │     ② HTML 表 → Markdown 管道表
 │     ③ 按 PDF 书签分卷，单卷 ≤60,000B
 │     ④ 生成 README + _manifest.json
 ↓
 └─ verify_md.py 四关自检
 ↓
 viking://resources/shared/chips/<chip>/<manual>/
```

## 0.3 为什么必须 `--format zip`

裸 markdown 没有 `page_idx`，也没有块类型：

| 需求 | 裸 markdown | zip 里的 middle_json |
|---|---|---|
| 按页对齐书签 | ❌ 只能靠锚点猜测 | ✅ `pages[].page_idx` 直接给 |
| 过滤页眉/页码 | ❌ 混在正文里 | ✅ `blocks[].type == "header"` |
| 判断"这是表还是正文" | ❌ 看不出来 | ✅ `blocks[].type == "table"` |
| 定位图片 | ❌ 只剩相对引用 | ✅ `image_path` 字段 |

实测代价：3 页 zip = 774KB（含图片），可接受。

## 0.4 block 类型与处理策略

实测 RN7326E 3 页的块类型分布：`text` 22、`paragraph_title` 7、`table` 6、
`header` 6、`page_number` 3。

| 类型 | 处理 |
|---|---|
| `header` / `footer` / `page_number` / `aside_text` / `page_footnote` | **丢弃**（噪音） |
| `title` / `doc_title` / `paragraph_title` | 转 `#` 标题，用 `level` 决定层级 |
| `table` | `content` 若为 HTML → 转 Markdown 管道表；为空则计入 `table_empty` 待抽查 |
| `image` / `figure` | `![alt](images/xxx)` + 图题斜体 |
| `equation` | `$$ ... $$` |
| 其余 | 按正文 |

## 0.5 content 的多形态（PIT-021）

`blocks[].content` 不是字符串而是列表，元素形态有三种（文字 / 图片 / HTML 表格），
个别 table 还是嵌套列表。`_flatten_content()` 用递归统一：

```python
def _flatten_content(content):
    if isinstance(content, str):  return _maybe_html_table(content.strip()), ""
    for item in content:                      # list[dict]
        img  ← item["image_path"] / item["img_path"]
        text += _maybe_html_table(item["content"])      # str 形态
        text += _flatten_content(item["content"])       # 嵌套 list 形态
```

## 0.6 分卷与体积

沿用 v1 的「按最深书签递归切分」，因为这条结论与解析器无关：
按章切会产出 236KB 的单卷（超 65,535B 硬限 3.6 倍），按最深书签只 1 块超限。

唯一区别：v1 用 `page_bytes()` 逐页取 `doc[i].get_text()` 估体积；
v2 直接数**已生成 Markdown** 的字节数，更准（不再估）。

---

# 一、几何兜底路径（v1）

以下为 MinerU 不可用时启用的老流程。

## 整体链路

```
PDF
 ├─ pymupdf    → 书签树（738 条）/ 文字层 / 矢量线 / 图片碎片 bbox
 └─ pdfplumber → 逐词坐标（按空白切分，比 pymupdf 的 span 更细）
        ↓
   ① 分卷：按最深书签递归切分，单卷 <=60,000B
        ↓
   ② 表格：栅格碎片定列 + 矢量横线定行 → Markdown 表 + 置信度
        ↓
   ③ 截图：低置信度表渲染 300dpi PNG，交 AI 复核
        ↓
   ④ 自检：体积 / 覆盖 / 表格结构 三关
        ↓
   viking://resources/shared/chips/<chip>/<manual>/
```

## 二、分卷算法（pdf_to_md.py）

### 2.1 为什么按「最深书签」而不是「章」

实测按不同粒度切分的后果：

| 粒度 | 块数 | 超 65,535B 硬限的块 |
|---|---|---|
| 1 级书签（26 章） | 26 | **3 块**：TIMER 236KB、OTGFS 187KB、I2C 70KB |
| 2 级书签 | 84 | 6 块（TMR1 72KB、OTGFS 寄存器 102KB…） |
| **最深书签** | **363** | **1 块**（p1~21 封面+目录 116KB） |

结论：只有按最深书签递归切分，才能天然落在安全区。

### 2.2 end 页的推导规则（易错点）

```
节点 end = 下一个同级节点起始页 - 1
末节点 end = 父节点的 end（继承父范围）
顶层末节点 = 全书末页
```

**踩过的坑**：早期版本让末节点一律取全书末页，导致 `11.7.11 发送寄存器（I2C_TXDT）`
被算成 p164–495（746KB）。实测 4 个卷因此虚假超限。

### 2.3 叶子卷仍超限怎么办

按页区间**硬切**（贪心尽量多装页）。

**为什么不做「收紧上限重跑」迭代**：书签已切到叶子，再收紧上限只会增加卷数而不减小
单卷体积。实测迭代 4 轮：超限卷从 6 → 1 → 2 → 5，总卷数从 55 → 185，始终无法收敛。

## 三、表格重建算法（table_rebuild.py）

### 3.1 排版真相（这是全部设计的前提）

| 观察 | 实测数据 | 算法含义 |
|---|---|---|
| 网格线不是矢量线段 | p95 `get_drawings()` 竖线 = **0 条** | 不能用矢量线定列 |
| 网格线是**栅格图片碎片** | p95 `get_image_info()` = 1410 张，其中 1152 张是 0.5×2.4pt 细高碎片 | 用碎片 bbox 聚类定列 |
| 碎片 x 聚类 = 8 条 | `87.1, 134.5, 195.8, 257.1, 318.3, 388.4, 441.0, 511.1` | 8 条**内部**边界 → **9 列**（无外框线，需补左右边界） |
| 表格无外框线 | 首条横线 y=57.5 在正文顶，第 2 条 y=131.9 | 表标题/表头会落进首块 |
| 首块高 74pt，数据行高 23pt | p95 | 首块需按中位行高重切 |
| 单元格会折行 | `SPI2_CS/I2S2_WS` 被拆成 `SPI2_CS/I2S` + `2_WS` | 同格多段按 `_join_hint` 拼接 |
| 「功能」列垂直偏移 | `位25` y=232.6，其功能文字从 y=215.1 起 | 按行带归属，不按文字行 |

### 3.2 两条路径（v6 核心决策）

```
该页有栅格几何？
 ├─ 是（全书 31 页）→ mode="grid"  → 精确重建 → 算置信度 → >=0.85 出表
 │                                                    └─ <0.85 出 TABLE_NEEDS_AGENT
 └─ 否（全书 464 页）→ mode="suspect" → **不出表** → 出 TABLE_SUSPECT + 原始文字
```

**为什么无栅格的页一律不出表**：v3/v4 试过用「表头文字 x 聚类」定列，结果是
在「功能列垂直偏移」和「单元格折行」上系统性错位，且置信度**虚高到 1.00**。
错表比没表危害大得多 —— 入库后会被检索到并当作事实使用。

### 3.3 行带算法

```
矢量横线 → 两两配对成行带（间隔 8~100pt）
  ↓
首尾相接的行带并成组（= 一张表）
  ↓
组内首块高度 > 中位行高 1.8 倍 → 按中位行高从底部向上重切
  ↓
剥掉空行与表标题行（让表头成为 rows[0]）
```

### 3.4 折行拼接规则 `_join_hint(prev, nxt)`

命中任一即**无空格拼接**：
- `prev` 以 `/ _ - ： : ,` 结尾；
- `nxt` 以数字或小写字母开头；
- `nxt` 长度 <= 5 且 `prev` 以大写字母或数字结尾。

实测效果：
- `SPI2_CS/I2S` + `2_WS` → `SPI2_CS/I2S2_WS` ✅
- `USART3_RT` + `S_DE` → `USART3_RTS_DE` ✅
- `TMR2_CH1` + `TMR2_EXT` → 保留空格（两个独立功能）✅

**踩过的坑**：写成 `cond and "" or " "` —— 空串是 falsy，会永远落回空格。

### 3.5 跨行带续行合并 `merge_wrapped_rows`

续行**必要条件 = 首列为空 且 非空格数 <= 2**。

反例（必须不合并）：p95 的 `PD5` + `USART2_TX` —— 首列有值（`PD5`），是本行独立数据。
早期判据写成「首列短或空」导致 `PD4 PD5` 粘连。

### 3.6 置信度打分（grid 路径）

| 项 | 权重 | 判据 |
|---|---|---|
| a 首列稳定 | 0.26 | 首列非空率 >=85% 得 1.0 |
| b 行填充稳定 | 0.28 | 各行非空列数的标准差 <=1.5 得 1.0 |
| c 表头横跨多列 | 0.20 | 表头非空列数/总列数 >=0.6 得 1.0 |
| d 列宽规律 | 0.14 | 最宽列 <=50% 总宽得 1.0 |
| e 列数合理 | 0.12 | >16 列得 0.3（多半折行误拆） |

阈值 `CONF_ACCEPT = 0.85`。

## 四、截图算法（render_crops.py）

- **300 dpi**：寄存器小字在 150dpi 下不可读；
- **bbox 四周留白 6pt**：避免切掉线框与边缘字；
- **单张高度上限 1200 px**：超过则纵向分段（重叠 4pt），避免 AI 读图时被压缩到看不清；
- **x 范围固定 60~560pt**：正文列区（AT32 手册正文在 88~560）。

**踩过的坑**：`pymupdf.get_pixmap(clip=Rect(...))` 的 `Rect` 顺序是 `(x0, y0, x1, y1)`。
早期把 bbox 传成 `(y0, y1, x0, x1)`，报
`FzErrorArgument: Invalid bandwriter header dimensions/setup`。

## 五、自检判据（verify_md.py）

| 关 | FAIL 判据 | 为什么 |
|---|---|---|
| 关1 体积 | 单卷 > 上限 | 65,535B 超限会**静默失索引**，不报错 |
| 关2 覆盖 | manifest 缺 / 页码断档 | 断档 = 漏页，内容不完整 |
| 关3 表格 | 同一张表内列数不一致 | 列数不齐 = 渲染必然错乱 |

WARN 项（>92% 上限、缺分隔行）需逐条判读，不阻断。
