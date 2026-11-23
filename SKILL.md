---
name: scientific-paper-translator
description: 将科研文献 PDF 翻译为可核验的中文双语对照结果，锁定术语全篇一致、按段落语义切分不丢不重、保护数字/单位/公式编号/文献引用与否定限定关系，并逐段给出译法依据与待确认标注。Use when the task requires translating a scientific paper (PDF or extracted text) into Chinese with paragraph-level bilingual alignment, consistent domain terminology, preserved numbers/units/equations/citations, and per-segment evidence pointing back to the source. 当任务要求把科研论文翻译成中文、产出段落级双语对照、统一术语译名、保留数字单位公式引用并逐段可回指原文时使用。
---

# Scientific Paper Translator（科研文献翻译）

将科研论文 PDF 翻译为**可核验**的中文双语对照结果。核心原则：**术语稳、段落全、关系准、可核验**——译不准的地方明确标注，绝不含混改写；关键结论能回指原文页码与段落。

## 适用条件与触发场景

- 输入为科研文献 PDF（单/双/多栏、中英文、文本型或扫描件），任务要求翻译全文或明确段落区间为中文。
- 需要段落级双语对照、术语一致性、数字/单位/公式/引用保护，以及能回指原文证据的结构化输出（`result.json`）。
- 不适用：已有可靠人工译文且仅需润色、或任务只要求"大意概括"而非逐段翻译。

## 环境与依赖

- 必需：`pdftotext`（Poppler）、Python ≥ 3.9（仅标准库）。
- 可选：`pdfinfo`、`pdftoppm` + `tesseract`（扫描页 OCR，真实坐标）。
- 所有路径相对于 Skill 目录解析；脚本用 `python <skill_dir>/scripts/…` 调用，不依赖个人文件或手工中间步骤。

## 两阶段流程（务必按序执行）

评测环境（DeepSeek Harness，单案例单次执行）内，一次执行完成一个案例并确认
`/app/output/result.json` 已写出。

### 阶段 1：确定性解析与分段（脚本完成）

```
python <skill_dir>/scripts/run_case.py --input /input --output /app/output --work /app/work
```

脚本读取 `case.json`（`case_id`、`output_contract`、材料路径）与 `task.md`，定位 PDF，
运行解析器与分段器，产出 `<work>/segments.json`、`<work>/protected.json`、`<work>/source.md`，
并把**部分** `result.json` 写入 `<output>/`（此时 `status=partial`，未翻译段 `target` 为空，
**不冒充完成**）。分段器为每段分配稳定 ID（`T0001…`）与页码/bbox，并按真实阅读顺序
（含双栏跨栏）排序；同时抽取每段的"保护令牌"：数字、单位、文献引用 `[n]`、公式编号 `(n)`。

### 阶段 2：Agent 语义翻译（调用者完成）

Agent 阅读 `<output>/source.md` 与 `<output>/result.json`，按下列规则逐段翻译，
然后写 `<work>/translation.json` 与 `<work>/terms.tsv`，再合并核验：

```
python <skill_dir>/scripts/run_case.py --input /input --output /app/output --work /app/work \
    --translation <work>/translation.json --terms <work>/terms.tsv
```

合并运行**不重解析 PDF**（内容哈希指纹复用），确定性核验三段内容后产出最终
`result.json` + `bilingual.md` + `verification.md`。

## 翻译规则（阶段 2 必须遵守）

### 1. 术语稳（术语表驱动，真正执行而非写进提示）

1. 通读 `source.md`，抽取领域术语（方法、对象、指标、专有名词），建立 `terms.tsv`：
   每行 `source<TAB>target<TAB>note`，一行一条，`#` 开头为注释。格式见
   `assets/term-table-template.tsv`。
2. 同一原文术语全篇只用同一译名；标题、摘要、正文、图注、表头、结论、参考文献标题中的
   同名术语必须统一。首次出现可加括号保留原文，如「阅读顺序（reading order）」。
3. 译名一旦确定即锁定；中途改译名必须回填已译段落并在 `changes`/`note` 记录。
4. 核验脚本会逐段 grep：凡原文命中术语、译文却未出现锁定译名者判"不一致"。**不要**
   用"写在提示里的术语表"——必须落在 `terms.tsv` 文件里。

### 2. 段落全（按语义边界切分，不丢不重不错位）

1. 以 `source.md` 的 `Txxxx` 段为单位翻译，**不合并、不拆分、不改序**；段号与原文一一对应。
2. 跨页、跨栏、跨章节段落：以段号为准续译，携带上一段结论与下一段开头作上下文，
   避免重复翻译上一段结尾或漏掉跨页续接句。
3. 每个 `Txxxx` 必须有一条非空译文；漏译/重复由核验脚本报出（完整性检查）。

### 3. 关系准（保护数字/单位/公式/引用/否定限定）

1. **数字与单位**：阿拉伯数字、小数、百分数、单位符号（nm、MPa、% 等）原样保留，
   不转写为汉字、不改数值。
2. **公式**：公式本体与编号（如 `(1)`）保持原符号串不翻译；上下标版面不保证还原，
   以原文 bbox 回查。行内公式符号保留。
3. **文献引用**：`[1]`、`[3-5]`、`[Smith et al., 2020]` 等标记原样保留；正文引用的
   编号与参考文献列表编号一致。
4. **图表编号**：`Fig. 1`/`Table 2` 译为「图 1」「表 2」，编号不变。
5. **否定与限定**：not/no/without/never/only/all/none/rarely 等含义必须译出
   （不/未/无/仅/所有/没有/很少…），不得漏掉或反转；让步/因果连词（although/whereas/
   because/therefore）保持原文逻辑方向。
6. **指代**：it/they/this/前者/后者等指代译出具体对象，避免歧义。

### 4. 可核验（逐段回指原文 + 译法依据 + 待确认标注）

1. 每段译文在 `translation.json` 中带 `note`（译法依据：术语锁定、结构对应、
   数字保留、否定译出等一句说明）与 `confidence`（high/medium/low）。
2. 译不准、多义、原文歧义、OCR 存疑处写进 `uncertain` 列表并附原因，译文内用
   `⚠️[待确认：…]` 标出——**禁止**含混抹平。
3. 无法翻译的元素（参考文献条目、公式）在 `note` 说明"保持原文，不翻译"。

## 输出约定

- `result.json`：`case_id` + `output_contract` 声明字段（`status`/`result`/`sources`/
  `uncertainties`）+ `result.translation`（每段 source/target/note/uncertain/页码/bbox）
  + `result.terminology_table` + `result.verification`。字段完整定义见
  `references/output-contract.md`。
- `bilingual.md`：段落级双语对照（原文 + 译文 + 段号 + 页码 + 依据 + 待确认）。
- `verification.md`：确定性核验报告（完整性/术语一致/保护令牌/否定清单）。

## 交付物清单

| 路径 | 作用 | 使用条件 |
|-|-|-|
| `scripts/run_case.py` | 评测入口（两阶段、缓存复用、核验合并） | Python ≥ 3.9 + pdftotext |
| `scripts/parse_pdf.py` | PDF 结构化解析（阅读顺序/段落/图表/公式/文献） | 同上；OCR 需 pdftoppm + tesseract |
| `scripts/segment.py` | 分段 + 保护令牌 + source.md | 同上 |
| `scripts/verify_translation.py` | 确定性核验（完整性/术语/保护令牌） | 同上 |
| `assets/term-table-template.tsv` | 术语表模板 | 阶段 2 |
| `references/output-contract.md` | 输入输出字段约定 | 文档 |
| `references/translation-method.md` | 方法、阈值与规则细节 | 文档 |
| `references/failure-modes.md` | 已知失败模式与处置 | 文档 |
| `examples/minimal-example.md` | 最小调用示例（输出为示意） | 文档 |

## 异常处理

- 加密/损坏 PDF：`status=failed`，记录工具原始报错，不绕过权限。
- 扫描件：有 OCR 时按真实坐标恢复并标 `verified:false`；无 OCR 时对应页标
  `unreadable`/`partial`，不编造文字、不臆译。
- 公式/表格为图片：按"结构元素"保留占位并在 `uncertain` 说明，不做无依据翻译。
- 参考文献无法解析：如实标注"未解析"，逐条保留原始行。
- 原文残缺或语言混杂：残缺处标 `⚠️[原文残缺]`，混杂语段按可读部分翻译并说明。
- `output_contract` 中无法产出的字段置 `null` 并在 `uncertainties` 说明原因，不填假值。

## 最小调用示例

见 `examples/minimal-example.md`（输出为示意，不冒充正式案例结果）。
