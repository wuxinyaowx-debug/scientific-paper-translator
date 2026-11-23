# 最小调用示例

> 本示例仅说明接口与完整使用流程；其中译文为**示意输出**，并非正式案例结果。
> 正式测试案例由平台提供，参赛者不另交正式答案。

## 输入

- `paper.pdf`：一篇双栏英文论文（文本型 PDF）。
- `case.json`：`{"case_id":"demo-001","output_contract":["status","result","sources","uncertainties"]}`
- `task.md`：翻译任务说明。

## 阶段 1：解析与分段（确定性）

```bash
python scripts/run_case.py --input /input --output /app/output --work /app/work
# status=partial case_id=demo-001 segments=27 translated=0
```

产出 `source.md`（示意）：

```text
[p.1]
[T0007] Parsing scientific PDFs requires correct reading order. Double column
layouts are common in journals ...
[T0008] Our pipeline instead recovers column geometry from word bounding boxes ...
```

## 阶段 2：Agent 翻译 + 术语表 + 合并核验

`terms.tsv`（示意）：

```text
reading order	阅读顺序	全篇锁定
bounding box	边界框	全篇锁定
```

`translation.json`（示意）：

```json
{"segments": [
  {"id": "T0007", "zh": "解析科研 PDF 需要正确的阅读顺序。双栏版面在期刊与会议论文中很常见。……",
   "note": "「reading order→阅读顺序」「column→栏」「layout→版面」锁定", "confidence": "high"}
]}
```

```bash
python scripts/run_case.py --input /input --output /app/output --work /app/work \
    --translation translation.json --terms terms.tsv
# status=success case_id=demo-001 segments=27 translated=27
```

## 产出

- `result.json`：`case_id` + `status` + `result.translation`（逐段 source/target/note/页码/bbox）
  + `result.terminology_table` + `result.verification` + `sources` + `uncertainties`。
- `bilingual.md`：段落级双语对照（原文 + 译文 + 段号 + 页码 + 依据 + 待确认）。
- `verification.md`：核验报告。

`verification.md`（示意）：

```text
# 译文核验报告
- 状态：`pass`
- 指标：完整性 100.0% · 术语一致 100.0% · 数字/单位/引用/公式保留 100.0%
```

## 说明

- 数字/单位/公式编号/文献引用由脚本抽取并回查，缺失即报，不靠人工肉眼。
- 扫描件且无 OCR 时，对应页标 `unreadable`/`partial`，不产出猜测译文。
- 输出字段完整定义见 `references/output-contract.md`。
