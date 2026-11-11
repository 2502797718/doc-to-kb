# 产出目录约定与入库落点

## 一、本地产出目录（MinerU 主路径 v2）

```
<out_dir>/
├── README.md                       导航：章 ↔ 文件 ↔ 原册页码
├── _manifest.json                  卷清单（verify_md.py 依赖它）
├── _postprocess_report.json        噪音过滤与块统计（header/footer/table/image 计数）
├── _mineru_meta.json               MinerU 调用参数、版本、MINERU_HOME
├── 00_<封面与目录>.md
├── 01_<章节名>.md
├── …
├── NN_<章节名>.md
└── raw/
    ├── <name>.zip                  MinerU 原始产物
    └── unpacked/
        ├── middle_json.json        ★ page_idx + block 类型（分卷去噪依据）
        ├── markdown.md             MinerU 自带的裸 markdown（无页码，不建议直接用）
        ├── structured_content.json
        └── images/                 原图（page_NNN_table_M.jpg 等）
```

### 几何兜底路径（v1）额外产物

```
<out_dir>/_review/
├── manifest.json                    待复核表格清单
├── manifest.md                      人读版清单（表格形式）
├── fixes.json                       agent 读图后的重写结果（apply_fixes.py 输入）
└── pNNN_tYY.png                     表格区域截图（300dpi）
```

### 文件命名规则

- 前缀两位序号 `NN_`：保证目录列表有序（`01_`…`60_`）；
- 章节名净化：`\/:*?"<>|` 与控制字符替换为 `_`，空白合并为 `_`，截断 60 字符；
- **不用中文括号以外的特殊符号**（避免跨平台/编码问题）。

### 单卷 YAML 头（固定格式）

```markdown
---
chip: rn7326e
manual: RN2026E_RN7326E_V2
pages: 51-160
---

# 4 三相计量

> 原册页码：p.51–160
```

作用：
1. 回答「这个寄存器在哪」时可带页码引用；
2. OV 语义抽取能识别来源与章节；
3. 人工核对时可回溯到原册位置。

## 二、入库落点

```
viking://resources/shared/chips/<chip>/<manual>/
```

实例：

```
viking://resources/shared/chips/RN8209D-RN8209L-V5/
viking://resources/shared/chips/RN2026E-RN7326E-V2/
viking://resources/shared/chips/AT32F423/RM_V2.04_CH/
viking://resources/shared/chips/AT32F423/DS_V2.03_ZH/
```

> **v2 变更**：落点从 `shared/reference/mcu/<chip>/` 改为 `shared/chips/<chip>/`。
> 原因：芯片手册是需要独立成层的资料类型（与 `specs/` 外部标准平级），
> 不再藏在 `reference/` 下。`reference/` 只留厂商产品文档与通用技术资料。

### 为什么放 `shared/chips/`

`viking://resources/shared/` 是全库共享层，v2 起分为：

| 层 | 目录 | 放什么 |
|---|---|---|
| 标准层 | `specs/` | 国家/国际标准原文（IEC、DL/T、EN） |
| **芯片层** | `chips/` | **芯片手册（RM / DS / Errata / AN）** |
| 参考层 | `reference/` | 厂商产品文档、领域理论、网络调试、上位机通用知识 |
| 治理层 | `governance/` | 自编规则、踩坑库、审查档案 |

- 手册是**外部资料**，不是规则 → 不归 `governance/`；
- 同一颗芯片被多个项目复用（AT32F423 被 ADW610 三模块共用）→ 放 `shared` 而非某项目；
- 与 `specs/` 分开：**标准是"必须符合的规范"，手册是"描述怎么用的资料"**，
  两者检索语境不同（查限值 vs 查寄存器）。

### 芯片目录内的 README（导航页）

每个芯片根目录应有一份 `README.md`，列出该芯片已入库的所有手册：

```markdown
# AT32F423 资料库

| 手册 | 版本 | 页数 | 入库路径 | 说明 |
|---|---|---|---|---|
| 技术手册 RM | V2.04 (2025.06.03) | 495 | `RM_V2.04_CH/` | 寄存器 / 引脚复用 / 外设行为唯一权威 |
| 数据手册 DS | V2.03 | 89 | `DS_V2.03_ZH/` | 引脚定义 / 电气特性 |
| 勘误表 Errata | V2.0.5 | 11 | `ES0010_Errata_V2.0.5/` | 已知缺陷，排坑必读 |
| 入门指南 AN0165 | V2.0.3 | 29 | `AN0165_GetStarted_V2.0.3/` | 环境搭建 |
| 迁移说明 MG0023 | V2.0.0 | 19 | `MG0023_F415_to_F423/` | 从 F415 迁移 |

## 查什么去哪本

| 要查什么 | 去哪本 | 章节 |
|---|---|---|
| 引脚支持哪些复用功能 | **DS** | 表6 引脚复用清单 |
| MUX 编号是几 | **RM** | §6.2.9 表6-1/6-2/6-3 |
| 外设寄存器位域 | **RM** | 各外设章末尾「寄存器描述」 |
| IRQn 数值 / 时钟宏 | 固件库 | `at32f423.h` / `at32f423_crm.h` |
| 已知芯片缺陷 | **Errata** | 全文 |

> ⚠ MUX 编号**不可按功能列表顺序推测**，必须查 RM 表6-1。
```

## 三、入库命令与参数

```bash
# 单本手册
ov add-resource <out_dir> -p viking://resources/shared/chips/AT32F423/RM_V2.04_CH

# 多本手册（分别入库，不要混在一个目录）
for m in RM_V2.04_CH DS_V2.03_ZH ES0010_Errata_V2.0.5; do
  ov add-resource <out>/<m> -p viking://resources/shared/chips/AT32F423/$m
done
```

**参数纪律**（库内 R3 铁律）：

- **`wait=false`**（默认）：`wait=true` 极易触发 `-32001` 超时，而文件其实已写入；
- 单批 <= 100 条，超出分批；
- 入库后**必须核验**：`read` 回读 2~3 卷 + `find "<外设名> 寄存器"` 验证检索命中。
- 图片引用前缀用 `--img-prefix` 按实际入库结构调（默认 `images/`）。

## 四、入库后验证清单

| # | 检查项 | 命令 / 方式 | 通过判据 |
|---|---|---|---|
| 1 | 目录存在 | `tree viking://resources/shared/chips/<chip>/<manual>` | 卷数与本地一致 |
| 2 | 内容完整 | `read <uri>/07_xxx.md` | 与本地文件一致 |
| 3 | **表格列数正确** | `grep` 找 `\| 地址 \| 名称 \| R/W \|` | 列数与表头一致，**无空列错位** |
| 4 | 语义检索 | `find "<芯片> <外设> 寄存器"` | 能召回该目录下的卷 |
| 5 | 噪音已清 | `_postprocess_report.json` 的 `header`/`page_number` | 数量 >0 说明滤掉了（不是没滤） |
| 6 | 待复核清零 | `verify_md.py` 关4 | MinerU 主路应为 0 |

## 五、与项目文档的衔接

手册入库后，应在**消费方**更新指针（不要在手册目录里写项目信息）：

- 项目 `notes/topics/*.md` 里的「寄存器查询唯一入口」指向新入库路径；
- `at32-manual-lookup` 技能的「三条答案分别在哪」表指向 `shared/chips/at32f423/`。

**单一真源原则**：手册目录只放手册转写产物；项目侧只放指针。
