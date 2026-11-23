# 输出约定（输入输出字段说明）

每次运行产出 `result.json`（评测模式在 `<output>/result.json`）以及
`source.md` / `bilingual.md` / `verification.md`。

## 输入

评测模式输入目录（默认 `/input`）：

| 文件 | 说明 |
|-|-|
| `case.json` | `{"case_id": "...", "output_contract": ["status","result","sources","uncertainties"], ...材料路径}`。`output_contract` 是**字段名列表**；脚本保证这些字段在 `result.json` 顶层存在 |
| `task.md` | 任务说明（翻译要求、范围、约束） |
| 材料 PDF | 由 `case.json` 的 `pdf`/`pdf_path`/`file`/`materials` 字段给出，或从输入目录搜索 `.pdf` |

## 阶段 2 输入（Agent 产物）

### terms.tsv（术语表）

每行 `source<TAB>target<TAB>note`；`#` 开头为注释行。示例见
`assets/term-table-template.tsv`。核验脚本按词边界（大小写不敏感）在原文中命中
`source`，并要求 `target` 出现在对应译文段。

### translation.json（译文）

```json
{"segments": [
  {"id": "T0007", "zh": "译文……", "note": "译法依据……",
   "uncertain": ["待确认项……"], "confidence": "high"}
]}
```

`id` 必须与 `segments.json` 中的段号一致；`zh` 必填，`note`/`uncertain`/`confidence`
选填（`note` 是评分要求的"译法依据"）。

## 顶层 result.json 字段

| 字段 | 类型 | 说明 |
|-|-|-|
| `case_id` | string | 来自 `case.json`（缺失则 `unknown`） |
| `status` | string | `success` / `partial` / `failed` / `unreadable` |
| `input` | object | PDF 路径与页数 |
| `task` | string | task.md 前 2000 字符 |
| `result` | object | 翻译主结果（见下） |
| `sources` | array | 证据定位路径（段号/页码/bbox/术语/核验） |
| `uncertainties` | array | 无法判定/未核验/未产出项，含原因 |
| `changes` | array | 变更记录（缓存复用、合并译文等） |
| `output_contract_raw` | any | case.json 中 output_contract 原文 |

`output_contract` 声明的每个字段保证存在于顶层；无法产出者置 `null` 并在
`uncertainties` 说明原因，不填假值。

## result.translation[*]

| 字段 | 说明 |
|-|-|
| `id` | 段号 `Txxxx`（稳定，与 source.md/segments.json 一一对应） |
| `kind` | `title`/`authors`/`affiliation`/`abstract`/`keywords`/`heading`/`paragraph`/`table`/`figure`/`equation`/`reference` |
| `page` / `pages` | 物理页码；跨页段落给多页 |
| `bbox` | PDF 页面坐标（pt，原点左上），可回查原文 |
| `section` | 所属章节（无则 null） |
| `source` | 原文（逐字，去连字符） |
| `target` | 译文（未译时为空串） |
| `note` | 译法依据 |
| `uncertain` | 待确认项列表 |

## result.terminology_table[*]

`source` / `target` / `note`，与 `terms.tsv` 一致。

## result.verification

确定性核验报告（`verify_translation.py` 输出）：

- `checks.completeness`：总段数 / 已译 / 缺译 / 多余 / 重复段号。
- `checks.term_consistency`：术语命中处数 / 一致 / 不一致及明细。
- `checks.protected_tokens`：应保留与缺失的数字/单位/引用/公式编号，按类型分列。
- `human_review`：否定/限定关系软检查清单（需人工确认）。
- `metrics`：完整性率 / 术语一致率 / 保护保留率（供与基础 Agent 公平对照）。

## 状态语义

- `success`：全部段落已译且核验无硬性问题。
- `partial`：部分段落未译，或核验发现硬性问题（术语不一致/保护令牌缺失/缺译），
  `uncertainties` 说明范围与可人工接管位置。
- `unreadable`：全部页面无文字层且 OCR 无结果；不输出猜测译文。
- `failed`：无法读取输入或解析器硬失败；`reason` 给出原始报错。

## 证据定位

- 每段 `bbox` 为 PDF 页面坐标；`source.md` 以 `[p.N]` 锚点标明第 N 物理页。
- 译文每段有 `note`（译法依据）与 `uncertain`（待确认），可据此回原文逐句核验。
