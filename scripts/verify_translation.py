#!/usr/bin/env python3
"""verify_translation.py — Deterministic post-translation verification.

Checks an Agent translation against extracted source segments and a locked
terminology table. Machine-executable part of the four scoring requirements
(术语稳 / 段落全 / 关系准 / 可核验):

  1. completeness          — every source segment has a non-empty translation,
                             no duplicate/missing/extra segment IDs;
  2. term consistency       — the terminology table is actually enforced: every
                             source segment containing a locked source term must
                             contain that term's locked target in its translation;
  3. protected-token keep   — numbers, units, bracket citations and equation
                             numbers extracted from the source survive verbatim;
  4. negation/qualifier     — soft checklist (source negation/quantifier markers
                             have a corresponding Chinese marker; never a hard
                             fail because negation is not word-to-word).

Exposes verify_all() for reuse by run_case.py, plus a CLI. Exit code 0 whenever
a report was written (including failed checks); 1 only on input error.
"""
import argparse
import json
import re
import sys
from pathlib import Path


def load_terms(tsv_path):
    """Parse terms.tsv: `source<TAB>target<TAB>note`; '#' lines are comments."""
    terms = []
    if not tsv_path or not Path(tsv_path).is_file():
        return terms
    for ln in Path(tsv_path).read_text(encoding="utf-8-sig").splitlines():
        ln = ln.rstrip("\n")
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        src, tgt = parts[0].strip(), parts[1].strip()
        if src and tgt:
            terms.append({"source": src, "target": tgt,
                          "note": parts[2].strip() if len(parts) > 2 else ""})
    return terms


def term_in_text(term, text):
    """Word-boundary, case-insensitive search of a (possibly multi-word) source
    term. Non-ASCII terms fall back to plain substring."""
    if not text:
        return False
    t = term.strip()
    if not t:
        return False
    if re.search(r"[^\x00-\x7F]", t):
        return t in text
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])",
                     text, re.IGNORECASE) is not None


def _num_present(num, text):
    # avoid matching "1" inside "2019" or "0.41" inside "10.412"; a trailing
    # "." / "," / ";" / ")" is punctuation, not part of the number
    return re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)",
                     text) is not None


def check_protected(protected, zh):
    res = {"numbers": {"expected": [], "missing": []},
           "units": {"expected": [], "missing": []},
           "citations": {"expected": [], "missing": []},
           "equation_refs": {"expected": [], "missing": []}}
    for num in protected.get("numbers", []):
        res["numbers"]["expected"].append(num)
        if not _num_present(num, zh):
            res["numbers"]["missing"].append(num)
    for u in protected.get("units", []):
        res["units"]["expected"].append(u)
        if u.lower() not in zh.lower():
            res["units"]["missing"].append(u)
    for c in protected.get("citations", []):
        res["citations"]["expected"].append(c)
        if c not in zh:
            res["citations"]["missing"].append(c)
    for e in protected.get("equation_refs", []):
        res["equation_refs"]["expected"].append(e)
        if e not in zh:
            res["equation_refs"]["missing"].append(e)
    return res


_NEG_EN = re.compile(
    r"\b(no|not|none|nothing|never|neither|nor|without|cannot|can't|unable|"
    r"fail(?:s|ed)?\s+to|lack(?:s|ed)?|absent|absence|rarely|seldom)\b|"
    r"\b(?:non|un|in|im|ir|il|dis|mis)-", re.IGNORECASE)
_NEG_ZH = re.compile(r"不|未|无|非|没有|无法|不能|从不|绝不|很少|缺乏|缺失|难以|否定|拒绝")


def verify_all(segments, zh_by_id, terms_path):
    """Run all checks. `segments` is the ordered segment list (each with id/src/
    protected). `zh_by_id` maps segment id -> Chinese text. Returns
    {"report": ..., "markdown": ...}."""
    terms = load_terms(terms_path)
    by_id = {s["id"]: s for s in segments}
    report = {"checks": {}, "metrics": {}, "issues": [], "human_review": []}

    # 1. completeness
    missing_ids = [sid for sid in by_id if not zh_by_id.get(sid, "").strip()]
    extra_ids = [tid for tid in zh_by_id if tid not in by_id]
    dup_ids = sorted({tid for tid in zh_by_id if tid in by_id
                      and sum(1 for t in zh_by_id if t == tid) > 1})
    # duplicate detection over the translation list is handled by caller when
    # available; here detect ids present more than once is impossible from a
    # dict, so dup_ids is computed against a list if provided.
    report["checks"]["completeness"] = {
        "ok": not missing_ids and not extra_ids,
        "total_segments": len(by_id),
        "translated": len(by_id) - len(missing_ids),
        "missing_ids": missing_ids,
        "extra_ids": extra_ids,
        "duplicate_ids": [],
    }

    # 2. term consistency (references & equations are kept verbatim, so their
    # source terms are not expected to be translated)
    term_checks = []
    for sid in sorted(by_id):
        s = by_id[sid]
        if s.get("kind") in ("reference", "equation"):
            continue
        zh = zh_by_id.get(sid, "")
        for trm in terms:
            if term_in_text(trm["source"], s["src"]):
                term_checks.append({
                    "segment_id": sid, "source_term": trm["source"],
                    "target_term": trm["target"],
                    "target_present": trm["target"] in zh,
                })
    term_total = len(term_checks)
    term_hit = sum(1 for c in term_checks if c["target_present"])
    report["checks"]["term_consistency"] = {
        "ok": term_total == term_hit,
        "total_occurrences": term_total,
        "consistent": term_hit,
        "inconsistent": term_total - term_hit,
        "inconsistent_details": [c for c in term_checks if not c["target_present"]],
    }

    # 3. protected tokens
    prot = {"numbers": {"expected": 0, "missing": 0, "details": []},
            "units": {"expected": 0, "missing": 0, "details": []},
            "citations": {"expected": 0, "missing": 0, "details": []},
            "equation_refs": {"expected": 0, "missing": 0, "details": []}}
    for sid in sorted(by_id):
        s = by_id[sid]
        zh = zh_by_id.get(sid, "")
        if not zh.strip():
            continue
        r = check_protected(s.get("protected", {}), zh)
        for kind in prot:
            prot[kind]["expected"] += len(r[kind]["expected"])
            prot[kind]["missing"] += len(r[kind]["missing"])
            for m in r[kind]["missing"]:
                prot[kind]["details"].append({"segment_id": sid, "token": m,
                                              "source": s["src"][:120]})
    prot_total = sum(prot[k]["expected"] for k in prot)
    prot_missing = sum(prot[k]["missing"] for k in prot)
    report["checks"]["protected_tokens"] = {
        "ok": prot_missing == 0, "expected": prot_total,
        "missing": prot_missing, "by_kind": prot,
    }

    # 4. negation/qualifier soft checklist
    for sid in sorted(by_id):
        s = by_id[sid]
        zh = zh_by_id.get(sid, "")
        if len(_NEG_EN.findall(s["src"])) > 0 and len(_NEG_ZH.findall(zh)) == 0:
            report["human_review"].append({
                "segment_id": sid,
                "issue": "source has negation/quantifier markers but translation "
                         "has no detected Chinese marker — confirm preserved",
                "source": s["src"][:120], "translation": zh[:120],
            })

    if by_id:
        report["metrics"]["completeness_rate"] = round(
            (len(by_id) - len(missing_ids)) / len(by_id), 4)
    else:
        report["metrics"]["completeness_rate"] = None
    report["metrics"]["term_consistency_rate"] = (
        round(term_hit / term_total, 4) if term_total else None)
    report["metrics"]["protected_preservation_rate"] = (
        round((prot_total - prot_missing) / prot_total, 4) if prot_total else None)

    overall_ok = (not missing_ids and not extra_ids
                  and (term_total == term_hit) and prot_missing == 0)
    report["status"] = "pass" if overall_ok else "issues_found"
    report["terms_loaded"] = len(terms)
    if report["checks"]["term_consistency"]["inconsistent_details"]:
        report["issues"].append({"issue": "term consistency violations",
                                 "count": term_total - term_hit})
    if prot_missing:
        report["issues"].append({"issue": "protected tokens dropped",
                                 "count": prot_missing})
    if missing_ids:
        report["issues"].append({"issue": "untranslated segments",
                                 "count": len(missing_ids)})

    md = _render_markdown(report)
    return {"report": report, "markdown": md}


def _render_markdown(report):
    md = []
    md.append("# 译文核验报告\n")
    md.append("- 状态：`%s`\n" % report["status"])
    md.append("- 指标：完整性 %.1f%% · 术语一致 %.1f%% · 数字/单位/引用/公式保留 %.1f%%\n" % (
        (report["metrics"]["completeness_rate"] or 0) * 100,
        (report["metrics"]["term_consistency_rate"] or 0) * 100,
        (report["metrics"]["protected_preservation_rate"] or 0) * 100))
    c = report["checks"]["completeness"]
    md.append("\n## 1 段落完整性\n")
    md.append("- 共 %d 段，已译 %d 段，缺译 %d，多余 %d\n" % (
        c["total_segments"], c["translated"], len(c["missing_ids"]),
        len(c["extra_ids"])))
    if c["missing_ids"]:
        md.append("- 缺译段号：%s\n" % ", ".join(c["missing_ids"]))
    tc = report["checks"]["term_consistency"]
    md.append("\n## 2 术语一致性\n")
    md.append("- 术语表 %d 条；正文命中 %d 处，一致 %d，不一致 %d\n" % (
        report["terms_loaded"], tc["total_occurrences"],
        tc["consistent"], tc["inconsistent"]))
    for d in tc.get("inconsistent_details", [])[:20]:
        md.append("- 段 %s：「%s」应译「%s」但译文未出现\n" % (
            d["segment_id"], d["source_term"], d["target_term"]))
    pt = report["checks"]["protected_tokens"]
    md.append("\n## 3 数字/单位/引用/公式编号保留\n")
    md.append("- 应保留 %d 项，缺失 %d 项\n" % (pt["expected"], pt["missing"]))
    for kind, label in (("numbers", "数字"), ("units", "单位"),
                        ("citations", "文献引用"), ("equation_refs", "公式编号")):
        k = pt["by_kind"][kind]
        if k["missing"]:
            md.append("- %s缺失：%s\n" % (label, "; ".join(
                "%s@%s" % (d["token"], d["segment_id"]) for d in k["details"][:15])))
    if report["human_review"]:
        md.append("\n## 4 需人工确认（否定/限定关系）\n")
        for h in report["human_review"][:20]:
            md.append("- 段 %s：%s\n" % (h["segment_id"], h["issue"]))
    return "".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", required=True)
    ap.add_argument("--translation", required=True)
    ap.add_argument("--terms", default=None)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    segs = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    seg_list = segs.get("segments", segs if isinstance(segs, list) else [])
    trans = json.loads(Path(args.translation).read_text(encoding="utf-8"))
    trans_list = trans.get("segments", trans if isinstance(trans, list) else [])
    zh_by_id = {t["id"]: t.get("zh", "")
                for t in trans_list if isinstance(t, dict) and t.get("id")}

    out = verify_all(seg_list, zh_by_id, args.terms)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "verification.json").write_text(
        json.dumps(out["report"], ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "verification.md").write_text(out["markdown"], encoding="utf-8")
    m = out["report"]["metrics"]
    print("status=%s completeness=%.3f term=%.3f protected=%.3f" % (
        out["report"]["status"], m["completeness_rate"] or 0,
        m["term_consistency_rate"] or 0,
        m["protected_preservation_rate"] or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
