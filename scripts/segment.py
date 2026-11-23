#!/usr/bin/env python3
"""segment.py — Build translatable segments from parse_pdf.py structured output.

The parser restores reading order but returns elements grouped by kind
(sections/paragraphs/tables/figures/equations/references). For translation we
need a single, ordered list of atomic units with stable IDs, page anchors, and
the "protected tokens" (numbers, units, citations, equation numbers) that must
survive translation unchanged.

This module:
  1. re-runs the parser's own block-ordering functions to recover global
     reading order across columns (no parsing logic duplicated by hand);
  2. assembles front matter + body elements into one ordered segment list;
  3. assigns stable IDs T0001..TNNNN and extracts protected tokens per segment.

Dependencies: parse_pdf.py (same directory), poppler tools via parse_pdf.
Python >= 3.9, standard library only.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_pdf as pp  # noqa: E402

# ------------------------------------------------------------- protected tokens

NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?\b")

_WORD_UNITS = [
    r"nm", r"μm|µm|um", r"mm", r"cm", r"km",
    r"eV", r"meV", r"keV", r"MeV", r"GeV", r"TeV",
    r"Hz", r"kHz", r"MHz", r"GHz", r"THz",
    r"mol", r"mmol", r"μmol|µmol|umol",
    r"mM", r"μM|µM|uM", r"nM", r"pM",
    r"mg", r"μg|µg|ug", r"kg",
    r"ms", r"kPa", r"MPa", r"GPa", r"mL", r"μL|µL|uL",
]
_SYMBOL_UNITS = [r"%", r"‰", r"°C", r"°F"]
UNIT_RE = re.compile(
    r"\b(?:" + "|".join(_WORD_UNITS) + r")\b|" + "|".join(_SYMBOL_UNITS),
    re.IGNORECASE)

CITATION_RE = re.compile(
    r"\[\d+(?:\s*[-,–]\s*\d+)*\]"          # [3], [3-5], [3,7]
    r"|\[\d+(?:\s*,\s*\d+)*\]"              # [3,7,9]
    r"|\[[A-Z][A-Za-z]*(?:\s+et\s+al\.?)?,?\s+\d{4}\]"  # [Smith et al., 2020]
)

EQREF_RE = re.compile(r"\(\d+(?:\.\d+)?\)")


def extract_protected(text):
    """Return protected tokens (numbers/units/citations/equation numbers) that
    must be preserved verbatim in the translation."""
    numbers = [m.group(0) for m in NUM_RE.finditer(text)]
    units = [m.group(0) for m in UNIT_RE.finditer(text)]
    citations = [re.sub(r"\s+", "", m.group(0)) for m in CITATION_RE.finditer(text)]
    eqrefs = [m.group(0) for m in EQREF_RE.finditer(text)]
    return {
        "numbers": numbers,
        "units": units,
        "citations": citations,
        "equation_refs": eqrefs,
    }


# ------------------------------------------------------------ reading order

def recover_ordered_blocks(pdf_path):
    """Re-run the parser's block ordering to get a global (page, block) list in
    reading order. Returns a dict page -> [bbox_tuples] aligned by order."""
    npages, err = pp.pdf_page_count(pdf_path)
    if npages is None:
        return None, err
    pages_blocks, page_heights, page_widths = [], [], []
    for p in range(1, npages + 1):
        pw, ph, blocks, perr = pp.extract_page_blocks(pdf_path, p)
        pages_blocks.append(blocks or [])
        page_heights.append(ph if ph else 1.0)
        page_widths.append(pw if pw else 1.0)
    drop, _removed = pp.detect_running_matter(pages_blocks, page_heights, npages)
    ordered = []
    for pi, blocks in enumerate(pages_blocks):
        kept = []
        for blk in blocks:
            blk.lines = [ln for ln in blk.lines if id(ln) not in drop]
            if blk.lines:
                kept.append(blk)
        for blk in pp.order_page_blocks(kept, page_widths[pi], page_heights[pi]):
            ordered.append((pi + 1, blk))
    by_page = {}
    for idx, (pg, blk) in enumerate(ordered):
        by_page.setdefault(pg, []).append((idx, blk.bbox))
    return by_page, None


def _bbox_tuple(b):
    if isinstance(b, dict):
        return (b.get("x0"), b.get("y0"), b.get("x1"), b.get("y1"))
    return b


def _overlap(a, b, tol=1.0):
    a, b = _bbox_tuple(a), _bbox_tuple(b)
    if any(v is None for v in a) or any(v is None for v in b):
        return False
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return (max(ax0, bx0) < min(ax1, bx1) + tol and
            max(ay0, by0) < min(ay1, by1) + tol)


def order_for(page, bbox, by_page):
    """Smallest ordered-block index whose bbox overlaps the element bbox."""
    blocks = by_page.get(page)
    if not blocks or not bbox:
        return None
    for idx, bb in blocks:
        if _overlap(bbox, bb):
            return idx
    return None


# ------------------------------------------------------------- segmentation

def _meta_field(parsed, key, kind, out):
    f = (parsed.get("metadata") or {}).get(key)
    if f and f.get("value"):
        out.append({"src": f["value"], "kind": kind,
                    "page": f.get("page"), "bbox": f.get("bbox"),
                    "section": None, "pages": [f.get("page")] if f.get("page") else []})


def _is_front_matter_paragraph(p, meta):
    title = (meta.get("title") or {}).get("value")
    if title and p.get("page") == 1 and \
            p["text"].strip().lower().startswith(title.strip().lower()[:40]):
        return True
    if (p.get("section") or "").strip().lower() in ("abstract", "摘要"):
        return True
    return False


def build_segments(parsed, pdf_path=None):
    """Return list of segment dicts in reading order, each with stable id."""
    by_page = None
    if pdf_path:
        by_page, err = recover_ordered_blocks(pdf_path)
    elements = []
    meta = parsed.get("metadata") or {}
    for key, kind in (("title", "title"), ("authors", "authors"),
                      ("affiliations", "affiliation"),
                      ("abstract", "abstract"), ("keywords", "keywords")):
        _meta_field(parsed, key, kind, elements)

    for p in parsed.get("paragraphs", []):
        if _is_front_matter_paragraph(p, meta):
            continue
        elements.append({"src": p["text"], "kind": "paragraph",
                         "page": p.get("page"), "bbox": p.get("bbox"),
                         "section": p.get("section"), "pages": p.get("pages", [])})
    for s in parsed.get("sections", []):
        elements.append({"src": s["title"], "kind": "heading",
                         "page": s.get("page"), "bbox": s.get("bbox"),
                         "section": None, "pages": [s.get("page")] if s.get("page") else []})
    for t in parsed.get("tables", []):
        md = t.get("markdown") or ""
        elements.append({"src": t.get("caption", "") + ("\n" + md if md else ""),
                         "kind": "table", "page": t.get("page"),
                         "bbox": t.get("bbox"), "section": None,
                         "pages": [t.get("page")] if t.get("page") else []})
    for f in parsed.get("figures", []):
        elements.append({"src": f.get("caption", ""), "kind": "figure",
                         "page": f.get("page"), "bbox": f.get("bbox"),
                         "section": None, "pages": [f.get("page")] if f.get("page") else []})
    for e in parsed.get("equations", []):
        elements.append({"src": e.get("text", ""), "kind": "equation",
                         "page": e.get("page"), "bbox": e.get("bbox"),
                         "section": None, "pages": [e.get("page")] if e.get("page") else []})
    for r in parsed.get("references", []):
        elements.append({"src": r.get("text", ""), "kind": "reference",
                         "page": r.get("page"), "bbox": r.get("bbox"),
                         "section": None, "pages": [r.get("page")] if r.get("page") else []})

    # assign reading order
    for el in elements:
        order = None
        if by_page and el.get("page") and el.get("bbox"):
            order = order_for(el["page"], el["bbox"], by_page)
        el["order"] = order

    def sort_key(el):
        o = el["order"]
        return (o if o is not None else 10**9, el.get("page") or 0,
                (el.get("bbox") or {}).get("y0", 0) if el.get("bbox") else 0)

    elements.sort(key=sort_key)
    segments = []
    for i, el in enumerate(elements):
        segments.append({
            "id": "T%04d" % (i + 1),
            "kind": el["kind"],
            "src": el["src"],
            "page": el.get("page"),
            "pages": el.get("pages", []),
            "bbox": el.get("bbox"),
            "section": el.get("section"),
            "order": el.get("order"),
            "protected": extract_protected(el["src"]),
        })
    return segments


def render_source_md(segments):
    """Render the source with segment IDs and page anchors for the Agent."""
    lines = []
    last_page = [0]
    for s in segments:
        p = s.get("page")
        if p and p != last_page[0]:
            lines.append("\n[p.%d]\n" % p)
            last_page[0] = p
        tag = "{%s}" % s["id"]
        kind = s["kind"]
        if kind == "heading":
            lines.append("\n## [%s] %s\n" % (s["id"], s["src"]))
        elif kind in ("equation",):
            lines.append("[%s] $$\n%s\n$$\n" % (s["id"], s["src"]))
        elif kind == "reference":
            lines.append("[%s] %s\n" % (s["id"], s["src"]))
        elif kind == "table":
            lines.append("[%s] %s\n" % (s["id"], s["src"]))
        else:
            lines.append("[%s] %s\n" % (s["id"], s["src"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parse_result", help="parse_pdf.py result.json")
    ap.add_argument("--pdf", help="original PDF for reading-order recovery")
    ap.add_argument("--out", default=".", help="output directory")
    args = ap.parse_args()
    parsed = json.loads(Path(args.parse_result).read_text(encoding="utf-8"))
    segments = build_segments(parsed, args.pdf)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "segments.json").write_text(
        json.dumps({"segments": segments, "count": len(segments)},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "protected.json").write_text(
        json.dumps([{"id": s["id"], "protected": s["protected"],
                     "src": s["src"]} for s in segments],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "source.md").write_text(render_source_md(segments),
                                      encoding="utf-8")
    print("segments=%d" % len(segments))
    return 0


if __name__ == "__main__":
    sys.exit(main())
