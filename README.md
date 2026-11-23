# scientific-paper-translator — 使用与方法说明

面向「文献翻译：科研阅读」任务的 Agent Skill。将科研论文 PDF 翻译为**可核验**的
中文双语对照结果：术语全篇锁定、段落按语义边界切分不丢不重、数字/单位/公式编号/
文献引用与否定限定关系与原文一致，每段译文可回指原文段号与页码并说明译法依据。
译不准处明确标注——不用"看起来通顺"的译文冒充"忠实"。

## 功能特性

- **术语稳**：术语表以 `terms.tsv` 文件落盘，核验脚本逐段 grep 强制执行，
  而非写在提示里听任模型自由发挥；标题/正文/图注/表头/结论同名术语统一。
- **段落全**：`parse_pdf.py` 按版面几何恢复 1~4 栏阅读顺序（跨栏、跨页、页眉页脚剔除），
  `segment.py` 分配稳定段号 `T0001…` 并带页码/bbox；核验报告缺译/多余/重复段号。
- **关系准**：数字、单位、`[n]` 引用、公式编号 `(n)` 在翻译前被确定性抽取、翻译后被
  确定性回查，缺失即报；否定/限定关系给出软检查清单。
- **可核验**：每段译文带 `note`（译法依据）、`uncertain`（待确认）、`page`/`bbox`；
  公式/参考文献等不可译元素如实标注"保持原文"。
- **可对照**：核验输出三项可重复计算的实数指标（完整性率/术语一致率/保护保留率），
  供平台做"加载 Skill vs 基础 Agent"的公平对照，不依赖文字自述。
- **运行可靠**：内容哈希指纹缓存（合并译文不重解析 PDF）；子进程返回码/超时/损坏
  输出逐一检查；`case_id` 不符拒绝采用，杜绝跨案例串用。

## 安装与依赖

| 依赖 | 版本 | 用途 | 安装 |
|-|-|-|-|
| Python | ≥ 3.9（仅标准库） | 运行脚本 | 任意发行版 |
| Poppler | ≥ 0.86（`pdftotext`；`pdfinfo`、`pdftoppm` 可选） | 文本层/版面提取、OCR 渲染 | Debian/Ubuntu: `apt install poppler-utils`；macOS: `brew install poppler`；Windows: Poppler for Windows 加入 PATH |
| Tesseract | 任意近期版本 | （可选）扫描页 OCR | `apt install tesseract-ocr` |

无网络、无第三方 Python 运行时依赖；不依赖任何个人文件或手工中间步骤。

## 快速开始

评测/案例两阶段模式（与平台 Runtime contract 一致）：

```bash
# 阶段 1：解析 + 分段 + 保护令牌抽取
python scripts/run_case.py --input /input --output /app/output --work /app/work
# Agent 阅读 /app/output/source.md，逐段翻译，写 translation.json 与 terms.tsv
# 阶段 2：合并 + 确定性核验（复用阶段 1 缓存，不重解析）
python scripts/run_case.py --input /input --output /app/output --work /app/work \
    --translation translation.json --terms terms.tsv
```

单独核验（已有 segments.json / translation.json / terms.tsv 时）：

```bash
python scripts/verify_translation.py --segments segments.json \
    --translation translation.json --terms terms.tsv --out .
```

## 输入与输出

- 输入：科研论文 PDF（单/双/多栏、中英文、文本型/扫描/混合）+ `case.json` + `task.md`。
- 阶段 2 输入：`translation.json`（逐段译文 + 依据 + 待确认）与 `terms.tsv`（术语表）。
- 输出：`result.json`（`case_id` + `output_contract` 字段 + `result.translation` +
  `result.terminology_table` + `result.verification`）、`source.md`、`bilingual.md`、
  `verification.md`。
- 字段与状态语义见 `references/output-contract.md`；方法与阈值见
  `references/translation-method.md`；失败模式见 `references/failure-modes.md`。

## 适用场景

- 科研论文整篇或明确段落区间的中文翻译，需要术语一致、段落对齐、数字公式引用保真、
  逐段可回指原文的交付。
- 双栏/多栏论文、跨页段落、含公式与参考文献的长文、扫描件（可选 OCR）。

## 限制（如实声明）

- 翻译本体由 Agent 完成；脚本负责分段、保护令牌抽取与确定性核验，不替代模型判断译文通顺度。
- 术语核验是"命中即查"：原文含术语、译文缺锁定译名即报不一致；不判断译文其他部分是否流畅。
- 数字核验为子串级"存在性"检查，无法识别"数值正确但语义错位"（如两个数对调）。
- 否定/限定关系为软检查清单，需人工确认，不判硬失败。
- 公式保留提取符号串，上下标版面不还原，需以 bbox 回查原文。
- OCR 端到端在无 tesseract 的环境不可运行；坐标换算见 `references/translation-method.md`。
- 参考文献与公式按约定保持原文不翻译（`note` 说明）。

## 自查方法

1. 看 `result.json` 的 `status` 与 `uncertainties` 确认受限范围。
2. 读 `verification.md` 三项指标与明细；非 100% 时按段号回原文逐条核对。
3. 抽查 3 段译文，按 `page`+`bbox` 回 `source.md` 逐句比对数字/单位/引用/否定。
4. 检查 `terms.tsv` 是否覆盖正文高频术语；术语一致率是否 100%。
5. 确认 `bilingual.md` 段号连续、无缺无重，与原文段一一对应。

## 维护

- 阈值集中在 `scripts/parse_pdf.py` 顶部常量与 `scripts/segment.py`/`verify_translation.py`
  的正则；调整须重新运行核验并抽查。
- 每次运行的变更写入 `result.json` 的 `changes`。
