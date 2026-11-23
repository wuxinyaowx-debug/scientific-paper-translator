#!/usr/bin/env python3
"""run_case.py — Harness entry point for the scientific-paper-translator skill.

Two-phase design (see SKILL.md):

  Phase 1 (deterministic, this script): read <input>/case.json + task.md,
  locate the PDF, run parse_pdf.py (structured extraction) and segment.py
  (ordered translation units + protected tokens). Writes a partial result.json
  (translation not yet done) plus source.md / segments.json / protected.json so
  the Agent has everything it needs to translate. No translation happens in the
  script.

  Phase 2 (Agent + script): the Agent builds terms.tsv (terminology table) and
  translation.json (per-segment Chinese + note + uncertain), then re-runs this
  script with --translation/--terms. The script verifies deterministically
  (completeness / term consistency / protected-token preservation), renders
  bilingual.md and verification.md, and assembles the final result.json honoring
  case.json's output_contract.

Defaults match the evaluation contract: --input /input --output /app/output
--work /app/work. Exit code 0 whenever a (possibly partial) result.json was
written; 1 only when no result could be produced at all.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import segment as segmod           # noqa: E402
import verify_translation as ver   # noqa: E402

PARSER = HERE / "parse_pdf.py"


def find_pdf(*dirs):
    for d in dirs:
        if d and d.is_dir():
            pdfs = sorted(d.rglob("*.pdf"))
            if pdfs:
                return pdfs[0]
    return None


def locate_pdf(case, indir, workdir):
    for key in ("pdf", "pdf_path", "file", "document", "material"):
        v = case.get(key)
        if isinstance(v, str):
            cand = Path(v)
            if not cand.is_absolute():
                cand = indir / v
            if cand.is_file():
                return cand
    materials = case.get("materials")
    if isinstance(materials, list):
        for m in materials:
            if isinstance(m, str) and m.lower().endswith(".pdf"):
                cand = Path(m)
                if not cand.is_absolute():
                    cand = indir / m
                if cand.is_file():
                    return cand
    return find_pdf(indir, workdir)


def contract_fields(case):
    contract = case.get("output_contract")
    fields = []
    if isinstance(contract, dict):
        fields = list(contract.get("fields", contract.keys()))
    elif isinstance(contract, list):
        fields = [f if isinstance(f, str) else (f.get("name") if isinstance(f, dict) else None)
                  for f in contract]
    return [f for f in fields if isinstance(f, str)]


def render_bilingual(segments, zh_by_id, notes, uncerts):
    lines = ["# 双语对照译文\n", "> 每段译文可回指原文段号（Txxxx）与页码；译法依据见每段“依据”。\n"]
    last_page = [0]
    for s in segments:
        p = s.get("page")
        if p and p != last_page[0]:
            lines.append("\n[p.%d]\n" % p)
            last_page[0] = p
        sid = s["id"]
        zh = zh_by_id.get(sid, "")
        lines.append("\n### %s · %s%s\n" % (sid, s["kind"],
                                            (" · 第 %s 页" % s["page"]) if s.get("page") else ""))
        lines.append("**原文**\n\n%s\n" % s["src"].strip())
        if zh.strip():
            lines.append("\n**译文**\n\n%s\n" % zh.strip())
        else:
            lines.append("\n**译文**\n\n（未译）\n")
        note = notes.get(sid, "")
        if note:
            lines.append("\n**依据**：%s\n" % note)
        uc = uncerts.get(sid, [])
        if uc:
            lines.append("\n**待确认**：%s\n" % "；".join(uc))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="/input")
    ap.add_argument("--output", default="/app/output")
    ap.add_argument("--work", default="/app/work")
    ap.add_argument("--translation", default=None)
    ap.add_argument("--terms", default=None)
    args = ap.parse_args()

    indir, outdir, workdir = Path(args.input), Path(args.output), Path(args.work)
    outdir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    changes, uncertainties = [], []

    case = {}
    case_path = indir / "case.json"
    if case_path.is_file():
        try:
            case = json.loads(case_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            changes.append("case.json parse error: %s" % e)
    case_id = case.get("case_id") or case.get("id") or "unknown"
    task_text = ""
    task_path = indir / "task.md"
    if task_path.is_file():
        task_text = task_path.read_text(encoding="utf-8-sig", errors="replace")

    pdf = locate_pdf(case, indir, workdir)
    if pdf is None:
        result = {"case_id": case_id, "status": "failed",
                  "reason": "no PDF material found in input directory",
                  "uncertainties": [{"issue": "missing input PDF"}],
                  "sources": [], "changes": changes,
                  "runtime": {"seconds": round(time.time() - t0, 2)}}
        (outdir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("status=failed reason=no-pdf")
        return 1

    # fingerprint so a --translation run reuses phase-1 without re-parsing
    fp_current = {
        "case_id": case_id,
        "pdf": str(pdf.resolve()), "pdf_size": pdf.stat().st_size,
        "pdf_md5": hashlib.md5(pdf.read_bytes()).hexdigest(),
        "case_md5": hashlib.md5(case_path.read_bytes()).hexdigest()
        if case_path.is_file() else None,
        "task_md5": hashlib.md5(task_path.read_bytes()).hexdigest()
        if task_path.is_file() else None,
    }
    fp_path = workdir / "phase1_fingerprint.json"
    seg_path = workdir / "segments.json"

    segments = None
    parsed = None
    phase1_ran = False
    if seg_path.is_file() and fp_path.is_file():
        try:
            old = json.loads(fp_path.read_text(encoding="utf-8"))
            if old == fp_current:
                seg_data = json.loads(seg_path.read_text(encoding="utf-8"))
                segments = seg_data.get("segments")
                parsed = json.loads((workdir / "parse_result.json").read_text(
                    encoding="utf-8"))
                changes.append("phase-1 reused from work cache (PDF not re-parsed)")
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            segments, parsed = None, None

    if segments is None:
        phase1_ran = True
        for stale in ("result.json", "fulltext.md", "source.md"):
            (workdir / stale).unlink(missing_ok=True)
        parser_stdout = workdir / "parser.stdout.log"
        parser_stderr = workdir / "parser.stderr.log"
        with parser_stdout.open("w", encoding="utf-8") as so, \
                parser_stderr.open("w", encoding="utf-8") as se:
            run = subprocess.run(
                [sys.executable, str(PARSER), str(pdf), "--out", str(workdir),
                 "--case-id", str(case_id)],
                stdout=so, stderr=se, timeout=600)
        if run.returncode != 0:
            result = {"case_id": case_id, "status": "failed",
                      "reason": "parser failed (code %d)" % run.returncode,
                      "parser_stderr_tail": parser_stderr.read_text(
                          encoding="utf-8", errors="replace")[-1000:],
                      "uncertainties": [{"issue": "parser failure"}],
                      "sources": [], "changes": changes,
                      "runtime": {"seconds": round(time.time() - t0, 2)}}
            (outdir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("status=failed reason=parser")
            return 1
        parsed = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
        if parsed.get("case_id") != case_id:
            result = {"case_id": case_id, "status": "failed",
                      "reason": "parser case_id mismatch", "uncertainties": [],
                      "sources": [], "changes": changes,
                      "runtime": {"seconds": round(time.time() - t0, 2)}}
            (outdir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print("status=failed reason=case-id-mismatch")
            return 1
        # keep parser result for later merge
        (workdir / "parse_result.json").write_text(
            json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
        segments = segmod.build_segments(parsed, str(pdf))
        (workdir / "segments.json").write_text(
            json.dumps({"segments": segments, "count": len(segments)},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        (workdir / "protected.json").write_text(
            json.dumps([{"id": s["id"], "protected": s["protected"],
                         "src": s["src"]} for s in segments],
                       ensure_ascii=False, indent=2), encoding="utf-8")
        (workdir / "source.md").write_text(
            segmod.render_source_md(segments), encoding="utf-8")
        uncertainties.extend(parsed.get("uncertainties", []))
        fp_path.write_text(json.dumps(fp_current, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    # ------------------------------------------------------------- phase 2
    zh_by_id, notes, uncerts = {}, {}, {}
    if args.translation and Path(args.translation).is_file():
        try:
            trans = json.loads(Path(args.translation).read_text(encoding="utf-8-sig"))
            for t in trans.get("segments", trans if isinstance(trans, list) else []):
                if isinstance(t, dict) and t.get("id"):
                    zh_by_id[t["id"]] = t.get("zh", "")
                    if t.get("note"):
                        notes[t["id"]] = t["note"]
                    if t.get("uncertain"):
                        uncerts[t["id"]] = t["uncertain"]
            changes.append("merged translation.json (%d segments)" % len(zh_by_id))
        except json.JSONDecodeError as e:
            uncertainties.append({"issue": "translation.json parse error",
                                  "detail": str(e)})

    # verification (deterministic) when translation + terms are available
    verification = None
    if zh_by_id:
        ver_result = ver.verify_all(segments, zh_by_id, args.terms)
        verification = ver_result["report"]
        (workdir / "verification.json").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
        (workdir / "verification.md").write_text(ver_result["markdown"],
                                                 encoding="utf-8")
        changes.append("verification executed (completeness/term/protected-token)")

    # ---------------------------------------------------------- assemble
    terms = ver.load_terms(args.terms) if args.terms else []
    translation_summary = {
        "total_segments": len(segments),
        "translated": len(zh_by_id),
        "untranslated": [s["id"] for s in segments if s["id"] not in zh_by_id],
    }
    result_obj = {
        "translation": [
            {"id": s["id"], "kind": s["kind"], "page": s.get("page"),
             "bbox": s.get("bbox"), "section": s.get("section"),
             "source": s["src"], "target": zh_by_id.get(s["id"], ""),
             "note": notes.get(s["id"], ""),
             "uncertain": uncerts.get(s["id"], [])}
            for s in segments],
        "terminology_table": terms,
        "verification": verification,
        "translation_summary": translation_summary,
    }

    status = parsed.get("status", "partial") if parsed else "partial"
    if parsed and parsed.get("status") == "unreadable" and not segments:
        # whole document has no text layer and no OCR result: keep the honest
        # unreadable signal rather than downgrading it to a translation "partial"
        status = "unreadable"
    elif status != "failed":
        if not zh_by_id:
            status = "partial"
        elif translation_summary["untranslated"]:
            status = "partial"
        elif verification and verification.get("status") == "issues_found":
            status = "partial"   # translation done but verification found issues
        else:
            status = "success"

    sources = [
        {"item": "metadata", "locator": "result.translation[*].page/.bbox (title/authors/abstract)"},
        {"item": "segments", "locator": "result.translation[*].id/.page/.bbox/.section"},
        {"item": "terminology", "locator": "result.terminology_table[*].source/.target"},
        {"item": "verification", "locator": "result.verification.checks"},
    ]
    if verification and verification.get("issues"):
        uncertainties.extend(verification["issues"])

    final = {
        "case_id": case_id,
        "status": status,
        "input": {"pdf": str(pdf), "pages": (parsed or {}).get("input", {}).get("pages")},
        "task": task_text[:2000] if task_text else None,
        "result": result_obj,
        "sources": sources,
        "uncertainties": uncertainties,
        "changes": changes,
        "runtime": {"parser": (parsed or {}).get("runtime"),
                    "total_seconds": round(time.time() - t0, 2)},
    }
    # honor output_contract: ensure each declared field exists at top level
    for f in contract_fields(case):
        if f not in final:
            if f == "result":
                final["result"] = result_obj
            elif f == "sources":
                final["sources"] = sources
            elif f == "uncertainties":
                final["uncertainties"] = uncertainties
            elif f == "status":
                final["status"] = status
            else:
                final[f] = None
                uncertainties.append(
                    {"issue": "output_contract field not produced", "field": f})
    final["output_contract_raw"] = case.get("output_contract")

    (outdir / "result.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "source.md").write_text(
        segmod.render_source_md(segments), encoding="utf-8")
    if zh_by_id:
        (outdir / "bilingual.md").write_text(
            render_bilingual(segments, zh_by_id, notes, uncerts), encoding="utf-8")
        if (workdir / "verification.md").is_file():
            (outdir / "verification.md").write_text(
                (workdir / "verification.md").read_text(encoding="utf-8"),
                encoding="utf-8")

    print("status=%s case_id=%s segments=%d translated=%d" % (
        final["status"], case_id, len(segments), len(zh_by_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
