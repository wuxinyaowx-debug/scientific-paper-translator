#!/usr/bin/env python3
"""parse_pdf.py — Parse a scientific paper PDF into verifiable structured output.

Version 2. Pipeline:
  1. page classification (text / sparse / no-text-layer) per page
  2. pdftotext -bbox-layout word boxes; scanned pages -> OCR (Tesseract TSV,
     real pixel coordinates converted to PDF points at 72/300)
  3. running-header/footer removal (cross-page repetition)
  4. N-column detection (left-edge clustering) + band-based reading order
  5. structure recognition: headings, figure/table captions, table grids,
     numbered equations, references
  6. metadata extraction (title/authors/affiliation/abstract/keywords/DOI/
     journal-year), every field with page+bbox+confidence or null
  7. per-paragraph bbox computed from the paragraph's own lines, with
     cross-page paragraph support

Dependencies: pdftotext (Poppler). Optional: pdftoppm + tesseract (OCR).
Python >= 3.9, standard library only.

Outputs:
  <out>/result.json   structured result with per-item page/bbox evidence
  <out>/fulltext.md   full text in restored reading order with [p.N] anchors

Exit codes: 0 success/partial (see status field), 1 hard failure (no result).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SPARSE_PAGE_WORDS = 20         # 1..19 words => sparse (figure/label page?), 0 => no text layer
WIDE_BLOCK_FRAC = 0.62         # block width fraction of page => spans all columns
HEADER_TOP_FRAC = 0.085        # y-center above this => header candidate
FOOTER_BOT_FRAC = 0.925        # y-center below this => footer candidate
RUNNING_MIN_PAGE_FRAC = 0.4    # repeated on >= this fraction of pages => running matter
COL_X0_TOL_FRAC = 0.05         # column left-edge clustering tolerance (fraction of page width)
COL_MIN_COUNT_FRAC = 0.2       # secondary columns need >= this fraction of the largest cluster
COL_SEP_FRAC = 0.12            # min separation between column left edges (fraction of page width)
MAX_COLUMNS = 4
OCR_DPI = 300
MAX_TABLE_LINES = 80

SEC_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.{0,90})$")
SEC_NAME_RE = re.compile(
    r"^(abstract|摘要|introduction|引言|conclusions?|结论|references|bibliography|"
    r"参考文献|acknowledg(e)?ments?|致谢|appendix|附录|keywords?|key words|关键词)\b\.?:?\s*$",
    re.IGNORECASE)
FIG_CAP_RE = re.compile(
    r"^\s*((?:figure|fig\.?)\s*[0-9IVX]+\s*[:.：]|图\s*[0-9IVX]+)",
    re.IGNORECASE)
TAB_CAP_RE = re.compile(
    r"^\s*((?:table|tab\.?)\s*[0-9IVX]+\s*[:.：]"
    r"|(?:table|tab\.?)\s*[0-9IVX]+\s*\((?:continued|续)\)"
    r"|表\s*[0-9IVX]+)",
    re.IGNORECASE)
TAB_CONT_RE = re.compile(r"continued|续表|（续）|\(续\)", re.IGNORECASE)
EQN_NUM_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*[)）]\s*$")
REF_ITEM_RE = re.compile(r"^\s*(\[\d+\]|\d+\s*[.)])\s+")
MATH_SIGNAL_RE = re.compile(
    r"[=∑∫√∏≤≥±×÷∂∇∞≈≠∈⊂⊆∪∩→←α-ωΑ-Ω∀∃∈ℝℕℤ]|\^|_|\\(?:frac|sum|int|alpha|beta|gamma)")
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
KEYWORDS_RE = re.compile(r"^(keywords?|key words|关键词|關鍵詞)\s*[:：]?\s*", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
AFFIL_HINT_RE = re.compile(
    r"universit|institut|department|school|laborator|academ|college|center|centre|"
    r"大学|学院|研究所|研究院|实验室|中心|医院|公司", re.IGNORECASE)
SENT_END = tuple(".!?。！？：:")
NUM_TOKEN_RE = re.compile(r"^[+-]?[\d.,%]+$")
CJK_RE = re.compile(r"[一-鿿]")


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class Line:
    words: list = field(default_factory=list)

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self):
        return (min(w.x0 for w in self.words), min(w.y0 for w in self.words),
                max(w.x1 for w in self.words), max(w.y1 for w in self.words))


@dataclass
class Block:
    lines: list = field(default_factory=list)
    page: int = 0
    source: str = "text"          # "text" | "ocr"

    @property
    def bbox(self):
        bs = [ln.bbox for ln in self.lines]
        return (min(b[0] for b in bs), min(b[1] for b in bs),
                max(b[2] for b in bs), max(b[3] for b in bs))

    @property
    def text_lines(self):
        return [ln.text for ln in self.lines if ln.text.strip()]


def bbox_dict(b):
    return {"x0": round(b[0], 1), "y0": round(b[1], 1),
            "x1": round(b[2], 1), "y1": round(b[3], 1)}


def union_bbox(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


# ---------------------------------------------------------------- tool layer

def ascii_safe_tmpdir():
    """Writable temp dir with a pure-ASCII path (some Poppler builds cannot
    create output files under non-ASCII paths)."""
    cands = [tempfile.gettempdir(),
             os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp"),
             r"C:\Windows\Temp", "/tmp", "."]
    for c in cands:
        try:
            if c and os.path.isdir(c):
                c.encode("ascii")
                return c
        except (UnicodeEncodeError, OSError):
            continue
    return tempfile.gettempdir()


def stage_ascii(path):
    """Copy *path* into the ASCII temp dir when its path is non-ASCII."""
    try:
        str(path).encode("ascii")
        return Path(path), (lambda: None)
    except UnicodeEncodeError:
        pass
    tmp = Path(tempfile.mkdtemp(prefix="pdfstage_", dir=ascii_safe_tmpdir()))
    dst = tmp / ("input%s" % Path(path).suffix)
    shutil.copy2(path, dst)
    return dst, (lambda: shutil.rmtree(tmp, ignore_errors=True))


def run_tool(cmd):
    """Run an external tool, return (ok, stdout, stderr). stdout/stderr are
    captured via anonymous temp files (works where named pipes are forbidden)."""
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as out_f, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as err_f:
            rc = subprocess.run(cmd, stdout=out_f, stderr=err_f, timeout=300).returncode
            out_f.seek(0)
            err_f.seek(0)
            return rc == 0, out_f.read(), err_f.read()
    except FileNotFoundError:
        return False, "", "tool not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return False, "", "timeout running: %s" % cmd[0]


def pdf_page_count(pdf):
    ok, out, err = run_tool(["pdfinfo", str(pdf)])
    if ok:
        m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
        if m:
            return int(m.group(1)), None
    # fallback: probe pages with pdftotext
    n = 0
    while n < 2000:
        ok2, _, err2 = run_tool(["pdftotext", "-f", str(n + 1), "-l", str(n + 1),
                                 str(pdf), "-"])
        if not ok2:
            if n == 0:
                return None, (err2 or err).strip() or "cannot read PDF"
            break
        n += 1
    return (n or None), (None if n else (err.strip() or "pdfinfo failed"))


# ------------------------------------------------------- text-layer extraction

def extract_page_blocks(pdf, page_no):
    """(page_width, page_height, [Block], error) via pdftotext -bbox-layout."""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False,
                                     dir=ascii_safe_tmpdir()) as tf:
        tmp = tf.name
    try:
        ok, _, err = run_tool(["pdftotext", "-bbox-layout", "-f", str(page_no),
                               "-l", str(page_no), str(pdf), tmp])
        if not ok:
            return None, None, None, err.strip()
        raw = Path(tmp).read_text(encoding="utf-8", errors="replace")
    finally:
        Path(tmp).unlink(missing_ok=True)
    raw = re.sub(r"<!DOCTYPE[^>]*>", "", raw)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, None, None, "bbox XML parse error: %s" % e

    def lname(tag):
        return tag.rsplit("}", 1)[-1]  # strip XHTML namespace if present

    page_el = None
    for el in root.iter():
        if lname(el.tag) == "page":
            page_el = el
            break
    if page_el is None:
        return None, None, None, "no <page> element in bbox output"
    pw = float(page_el.get("width"))
    ph = float(page_el.get("height"))
    blocks = []
    for b_el in page_el.iter():
        if lname(b_el.tag) != "block":
            continue
        blk = Block(page=page_no)
        for l_el in b_el:
            if lname(l_el.tag) != "line":
                continue
            ln = Line()
            for w_el in l_el:
                if lname(w_el.tag) != "word":
                    continue
                ln.words.append(Word(
                    text=w_el.text or "",
                    x0=float(w_el.get("xMin")), y0=float(w_el.get("yMin")),
                    x1=float(w_el.get("xMax")), y1=float(w_el.get("yMax")),
                ))
            if ln.words:
                ln.words.sort(key=lambda w: w.x0)
                blk.lines.append(ln)
        if blk.lines:
            blk.lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
            blocks.append(blk)
    return pw, ph, blocks, None


def classify_page(nwords):
    if nwords == 0:
        return "empty"     # no text layer (scanned/image-only)
    if nwords < SPARSE_PAGE_WORDS:
        return "sparse"    # figure/label page or partially recognized
    return "text"


# --------------------------------------------------------------------- OCR

def parse_tesseract_tsv(tsv_text, pw, ph, dpi=OCR_DPI, with_conf=False):
    """Convert Tesseract TSV (pixel coords at *dpi*) into Blocks with real
    PDF-point coordinates. Pure function — unit-testable without tesseract.
    With with_conf=True returns (blocks, {"words": n, "mean_conf": c})."""
    scale = 72.0 / dpi
    groups = {}  # (block, par) -> {(line): [Word]}
    confs = []
    for row in tsv_text.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        level, bnum, pnum, lnum = cols[0], cols[2], cols[3], cols[4]
        text = cols[11].strip()
        if level != "5" or not text:
            continue
        try:
            left, top = float(cols[6]), float(cols[7])
            width, height = float(cols[8]), float(cols[9])
            conf = float(cols[10])
        except ValueError:
            continue
        w = Word(text=text, x0=left * scale, y0=top * scale,
                 x1=(left + width) * scale, y1=(top + height) * scale)
        groups.setdefault((bnum, pnum), {}).setdefault(lnum, []).append(w)
        if conf >= 0:
            confs.append(conf)
    blocks = []
    for key in sorted(groups, key=lambda k: (int(k[0]), int(k[1]))):
        blk = Block(source="ocr")
        for lkey in sorted(groups[key], key=int):
            words = sorted(groups[key][lkey], key=lambda w: w.x0)
            blk.lines.append(Line(words=words))
        if blk.lines:
            blocks.append(blk)
    if with_conf:
        stats = {"words": sum(len(ln.words) for b in blocks for ln in b.lines),
                 "mean_conf": round(sum(confs) / len(confs), 1) if confs else None}
        return blocks, stats
    return blocks


def ocr_page(pdf, page_no, pw, ph):
    """OCR one scanned page: pdftoppm render + tesseract TSV with real
    coordinates converted to PDF points.
    Returns ([Block], error, stats) — stats has words/mean_conf or None."""
    tmpdir = Path(tempfile.mkdtemp(prefix="ocr_", dir=ascii_safe_tmpdir()))
    try:
        imgbase = tmpdir / "page"
        ok, _, err = run_tool(["pdftoppm", "-f", str(page_no), "-l", str(page_no),
                               "-r", str(OCR_DPI), "-png", "-singlefile",
                               str(pdf), str(imgbase)])
        img = Path(str(imgbase) + ".png")
        if not ok or not img.is_file():
            return None, "pdftoppm failed: %s" % err.strip(), None
        ok, out, err = run_tool(["tesseract", str(img), "stdout",
                                 "--psm", "3", "tsv"])
        if not ok:
            return None, "tesseract failed: %s" % err.strip(), None
        blocks, stats = parse_tesseract_tsv(out, pw, ph, with_conf=True)
        if not blocks:
            return None, "tesseract produced no words", None
        return blocks, None, stats
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------- running matter

def normalize_running(text):
    t = unicodedata.normalize("NFKC", text).strip().lower()
    return re.sub(r"\d+", "#", t)


def detect_running_matter(pages_blocks, page_heights, npages):
    seen = {}
    for pi, blocks in enumerate(pages_blocks):
        ph = page_heights[pi]
        for blk in blocks:
            for ln in blk.lines:
                yc = (ln.bbox[1] + ln.bbox[3]) / 2.0
                if yc < HEADER_TOP_FRAC * ph or yc > FOOTER_BOT_FRAC * ph:
                    key = normalize_running(ln.text)
                    if key:
                        seen.setdefault(key, []).append((pi, ln))
    drop = set()
    removed = []
    thresh = max(2, int(round(RUNNING_MIN_PAGE_FRAC * npages)))
    for key, occ in seen.items():
        pages_hit = len({pi for pi, _ in occ})
        if pages_hit >= thresh:
            for pi, ln in occ:
                drop.add(id(ln))
            removed.append({"text": occ[0][1].text,
                            "pages": sorted({pi + 1 for pi, _ in occ})})
    return drop, removed


# ------------------------------------------------------------ reading order

def order_page_blocks(blocks, pw, ph):
    """Restore reading order for 1..N-column pages with full-width elements.

    Columns are detected by clustering block left edges (x0); blocks matching
    no column cluster on a multi-column page (title, author lines, wide
    equations) are treated as full-width band breakers. Bands are split at
    full-width blocks; within a band columns run left to right."""
    wide, narrow = [], []
    for b in blocks:
        x0, y0, x1, y1 = b.bbox
        (wide if (x1 - x0) > WIDE_BLOCK_FRAC * pw else narrow).append(b)
    if not narrow:
        return sorted(wide, key=lambda b: b.bbox[1])

    tol = COL_X0_TOL_FRAC * pw
    clusters = []  # [x0_sum, count, [blocks]]
    for b in sorted(narrow, key=lambda b: b.bbox[0]):
        if clusters and abs(b.bbox[0] - clusters[-1][0] / clusters[-1][1]) <= tol:
            clusters[-1][0] += b.bbox[0]
            clusters[-1][1] += 1
            clusters[-1][2].append(b)
        else:
            clusters.append([b.bbox[0], 1, [b]])
    clusters.sort(key=lambda c: -c[1])
    top = clusters[0][1]
    accepted = []
    for c in clusters:
        if len(accepted) >= MAX_COLUMNS:
            break
        if c[1] < max(2, COL_MIN_COUNT_FRAC * top):
            break  # a real column has >= 2 blocks; singletons are not columns
        mean = c[0] / c[1]
        if all(abs(mean - a) > COL_SEP_FRAC * pw for a in accepted):
            accepted.append(mean)
    accepted.sort()
    ncols = len(accepted)

    if ncols <= 1:
        return sorted(wide + narrow, key=lambda b: b.bbox[1])

    # column intervals from accepted clusters
    intervals = []
    for mean in accepted:
        members = [b for b in narrow if abs(b.bbox[0] - mean) <= tol]
        if not members:  # cluster lost members (should not happen)
            intervals.append((mean, mean))
            continue
        intervals.append((min(b.bbox[0] for b in members),
                          max(b.bbox[2] for b in members)))
    # assign blocks: member of a column, or full-width band breaker.
    # A block whose center sits near the page centre but whose left edge
    # matches no column (title, author lines, centred wide equations) is a
    # band breaker even if its center falls inside a wide column interval.
    cols = [[] for _ in accepted]
    for b in narrow:
        x0 = b.bbox[0]
        xc = (x0 + b.bbox[2]) / 2.0
        edge_hit = None
        for ci, mean in enumerate(accepted):
            if abs(x0 - mean) <= tol:
                edge_hit = ci
                break
        if edge_hit is not None:
            cols[edge_hit].append(b)
            continue
        if abs(xc - pw / 2.0) < 0.12 * pw:
            wide.append(b)
            continue
        hit = None
        for ci, (lo, hi) in enumerate(intervals):
            if lo - tol <= xc <= hi + tol:
                hit = ci
                break
        if hit is None:
            wide.append(b)
        else:
            cols[hit].append(b)

    # bands split by full-width blocks
    cuts = sorted({0.0, ph} | {w.bbox[1] for w in wide} | {w.bbox[3] for w in wide})
    bands = [(a, b_) for a, b_ in zip(cuts, cuts[1:]) if b_ - a > 1e-6]
    band_items = []
    used = set()
    for (a, b_) in bands:
        items = []
        for ci in range(ncols):
            in_band = [n for n in cols[ci]
                       if a - 1e-6 <= (n.bbox[1] + n.bbox[3]) / 2.0 < b_ - 1e-6]
            items.extend(sorted(in_band, key=lambda n: n.bbox[1]))
            used.update(id(n) for n in in_band)
        if items:
            band_items.append((a, items))
    events = [(w.bbox[1], 0, w) for w in wide] + [(a, 1, items) for a, items in band_items]
    events.sort(key=lambda e: (e[0], e[1]))
    ordered = []
    for _, kind, obj in events:
        ordered.extend([obj] if kind == 0 else obj)
    for ci in range(ncols):
        for n in cols[ci]:
            if id(n) not in used and id(n) not in {id(b) for b in ordered}:
                ordered.append(n)
    return ordered


# ------------------------------------------------------------- text helpers

def dehyphenate(texts):
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if out.endswith("-") and t[:1].islower():
            out = out[:-1] + t
        else:
            out = (out + " " + t) if out else t
    return out.strip()


def heading_word_ok(w):
    """Heading words contain letters (Latin/CJK) but no digits. 'LSTM-based'
    passes; 'a2', '+1.0' do not."""
    return bool(re.search(r"[A-Za-z一-鿿]", w)) and not re.search(r"\d", w)


def heading_level(t):
    """Return heading level (1-based) or None. Guards against numbered
    sentences ('3 results show ...') and numeric table cells ('91.2 +1.0',
    '10 A a2')."""
    m = SEC_NUM_RE.match(t)
    if m:
        rest = m.group(2)
        words = rest.split()
        if len(words) > 10 or t.rstrip().endswith(SENT_END):
            return None
        c0 = rest[0]
        if not c0.isalpha():
            return None  # numeric/sign-led rest => data cell, not a heading
        if c0.islower():
            return None  # numbered sentence, not a heading
        if not all(heading_word_ok(w) for w in words):
            return None  # digit-bearing word => table data, not a heading
        return m.group(1).count(".") + 1
    if SEC_NAME_RE.match(t):
        return 1
    return None


def is_prose_line(t):
    """Heuristic: a line of running text (used to end table capture)."""
    words = t.split()
    if len(words) < 8:
        return False
    digits = sum(1 for w in words if NUM_TOKEN_RE.match(w))
    alpha = sum(1 for w in words if re.search(r"[A-Za-z一-鿿]", w))
    return digits <= 1 and alpha >= len(words) * 0.8


def is_mathy_short(t):
    words = t.split()
    return (len(words) <= 8 and not t.rstrip().endswith(SENT_END)
            and bool(MATH_SIGNAL_RE.search(t)))


# -------------------------------------------------------------- table grid

def split_cells(ln, char_w):
    """Split a Line's words into cells at large horizontal gaps."""
    words = sorted(ln.words, key=lambda w: w.x0)
    cells = []
    cur = [words[0]]
    gap_thr = max(10.0, 2.0 * char_w)
    for w in words[1:]:
        if w.x0 - cur[-1].x1 > gap_thr:
            cells.append(cur)
            cur = [w]
        else:
            cur.append(w)
    cells.append(cur)
    return [(c[0].x0, c[-1].x1, " ".join(w.text for w in c)) for c in cells]


def build_grid(line_objs):
    """Reconstruct a table grid from captured (page, Line) pairs.

    Returns (grid_dict, markdown, uncertain_flags). Never invents cell
    membership: ambiguous rows are kept raw and flagged."""
    char_w = 6.0
    widths = []
    heights = []
    for _, ln in line_objs:
        heights.append(ln.bbox[3] - ln.bbox[1])
        for w in ln.words:
            if w.text:
                widths.append((w.x1 - w.x0) / max(1, len(w.text)))
    if widths:
        widths.sort()
        char_w = max(2.0, widths[len(widths) // 2])
    heights.sort()
    row_tol = 0.5 * (heights[len(heights) // 2] if heights else 12.0)

    # merge extractor-split fragments of the same visual row (y overlap)
    visual_rows = []  # [ycenter, [Word]]
    for _, ln in sorted(line_objs, key=lambda pl: (pl[1].bbox[1], pl[1].bbox[0])):
        bb = ln.bbox
        yc = (bb[1] + bb[3]) / 2.0
        if visual_rows and abs(yc - visual_rows[-1][0]) <= row_tol:
            visual_rows[-1][1].extend(ln.words)
        else:
            visual_rows.append([yc, list(ln.words)])
    rows_cells = []
    for _, words in visual_rows:
        rows_cells.append(split_cells(Line(words=sorted(words, key=lambda w: w.x0)),
                                      char_w))

    # cluster cell left edges -> column boundaries
    col_centers = []
    for cells in rows_cells:
        for x0, _, _ in cells:
            for cc in col_centers:
                if abs(x0 - cc[0] / cc[1]) <= 12.0:
                    cc[0] += x0
                    cc[1] += 1
                    break
            else:
                col_centers.append([x0, 1])
    col_x = sorted(c[0] / c[1] for c in col_centers if c[1] >= 1)
    ncol = len(col_x)
    uncertain = []
    if ncol < 2 or not line_objs:
        raw = [ln.text for _, ln in line_objs]
        return ({"n_columns": ncol, "headers": [], "rows": [],
                 "raw_lines": raw, "uncertain": True},
                "\n".join(raw), ["fewer than two columns detected; kept raw lines"])

    # column x-boundaries (midpoints between cluster centers)
    bounds = [float("-inf")] + [(a + b) / 2.0 for a, b in zip(col_x, col_x[1:])] + [float("inf")]

    def col_of(x0):
        for i in range(ncol):
            if bounds[i] <= x0 < bounds[i + 1]:
                return i
        return ncol - 1

    rows, merged = [], []
    for ri, cells in enumerate(rows_cells):
        row = [""] * ncol
        for x0, x1, text in cells:
            ci = col_of(x0)
            span = 1
            while ci + span < ncol and x1 > bounds[ci + span] + 6.0:
                span += 1
            if span > 1:
                merged.append({"row": ri, "col": ci, "colspan": span, "text": text})
            if row[ci]:
                row[ci] += " " + text
                uncertain.append("row %d col %d held two cell fragments; joined" % (ri, ci))
            else:
                row[ci] = text
        rows.append(row)

    # header rows: leading rows until the first mostly-numeric row
    def numeric_frac(row):
        vals = [c for c in row if c]
        if not vals:
            return 0.0
        return sum(1 for c in vals if NUM_TOKEN_RE.match(c)) / len(vals)

    n_head = 0
    for row in rows:
        if numeric_frac(row) >= 0.5:
            break
        n_head += 1
    n_head = min(n_head, max(1, len(rows) - 1)) if rows else 0
    headers = rows[:n_head]
    body = rows[n_head:]

    md = []
    if headers:
        md.append("| " + " | ".join(" / ".join(h) for h in zip(*headers)) + " |")
    else:
        md.append("| " + " | ".join("c%d" % (i + 1) for i in range(ncol)) + " |")
    md.append("|" + "---|" * ncol)
    for row in body:
        md.append("| " + " | ".join(row) + " |")

    grid = {"n_columns": ncol, "n_header_rows": n_head, "headers": headers,
            "rows": body, "merged_cells": merged,
            "uncertain": bool(uncertain)}
    return grid, "\n".join(md), uncertain


# ---------------------------------------------------------------- metadata

def extract_metadata(ordered, sections, paragraphs, removed_running, page1_text):
    meta = {}

    def field(value, page, bbox, confidence):
        return {"value": value, "page": page, "bbox": bbox,
                "confidence": confidence}

    # ---- title / authors / affiliation: page-1 blocks above the first section
    prelim = []  # blocks before Abstract/first section on page 1
    for page, blk in ordered:
        if page != 1:
            break
        first_txt = blk.text_lines[0].strip() if blk.text_lines else ""
        if SEC_NAME_RE.match(first_txt) or SEC_NUM_RE.match(first_txt):
            break
        prelim.append(blk)

    def med_height(blk):
        hs = sorted(ln.bbox[3] - ln.bbox[1] for ln in blk.lines)
        return hs[len(hs) // 2] if hs else 0.0

    if prelim:
        # title = largest-type block (line height ~ font size); small blocks
        # (< 60% of title height, e.g. journal masthead lines) are excluded
        # from author/affiliation candidates
        title_blk = max(prelim, key=med_height)
        title_h = med_height(title_blk)
        meta["title"] = field(dehyphenate(title_blk.text_lines), 1,
                              bbox_dict(title_blk.bbox), "medium")
        authors, affils = [], []
        for blk in prelim:
            if blk is title_blk or med_height(blk) < 0.6 * title_h:
                continue
            txt = dehyphenate(blk.text_lines)
            if AFFIL_HINT_RE.search(txt):
                affils.append((txt, blk))
            elif len(txt.split()) <= 12:
                authors.append((txt, blk))
        if authors:
            b = union_bbox([blk.bbox for _, blk in authors])
            meta["authors"] = field("; ".join(t for t, _ in authors), 1,
                                    bbox_dict(b), "low")
        else:
            meta["authors"] = None
        if affils:
            b = union_bbox([blk.bbox for _, blk in affils])
            meta["affiliations"] = field("; ".join(t for t, _ in affils), 1,
                                         bbox_dict(b), "low")
        else:
            meta["affiliations"] = None
    else:
        meta["title"] = None
        meta["authors"] = None
        meta["affiliations"] = None

    # ---- abstract: paragraphs under an Abstract section
    abs_secs = [s for s in sections if s["title"].strip().lower().startswith(("abstract", "摘要"))]
    if abs_secs:
        abs_paras = [p for p in paragraphs if p.get("section") == abs_secs[0]["title"]]
        if abs_paras:
            b = union_bbox([(p["bbox"]["x0"], p["bbox"]["y0"],
                             p["bbox"]["x1"], p["bbox"]["y1"]) for p in abs_paras])
            meta["abstract"] = field(" ".join(p["text"] for p in abs_paras)[:2000],
                                     abs_paras[0]["page"], bbox_dict(b), "high")
        else:
            meta["abstract"] = None
    else:
        meta["abstract"] = None

    # ---- keywords
    kw = None
    for p in paragraphs[:80]:
        m = KEYWORDS_RE.match(p["text"])
        if m:
            kw = field(p["text"][m.end():].strip()[:500], p["page"], p["bbox"], "high")
            break
    meta["keywords"] = kw

    # ---- DOI
    doi = None
    m = DOI_RE.search(page1_text)
    if m:
        doi = field(m.group(0).rstrip("."), 1, None, "high")
    meta["doi"] = doi

    # ---- journal / year from running matter or page-1 footer area
    journal = None
    cands = [r["text"] for r in removed_running]
    for c in cands:
        if YEAR_RE.search(c) and re.search(r"[A-Za-z]{4,}", c):
            journal = c
            break
    if journal:
        y = YEAR_RE.search(journal)
        meta["journal"] = {"value": journal, "page": None, "bbox": None,
                           "confidence": "low (from running header/footer)"}
        meta["year"] = {"value": y.group(0) if y else None, "page": None,
                        "bbox": None, "confidence": "low"}
    else:
        meta["journal"] = None
        meta["year"] = None
    return meta


# ------------------------------------------------------------- main parse

def parse_document(pdf, log):
    """Returns (result_dict, markdown_text, fatal_error)."""
    uncertainties = []
    pdf, cleanup = stage_ascii(pdf)
    try:
        return _parse_document_staged(pdf, uncertainties)
    finally:
        cleanup()


def _parse_document_staged(pdf, uncertainties):
    npages, err = pdf_page_count(pdf)
    if npages is None:
        return None, None, "cannot read PDF (encrypted or corrupt?): %s" % err

    pages_blocks, page_heights, page_widths = [], [], []
    page_class, empty_pages, sparse_pages, ocr_pages = [], [], [], []
    for p in range(1, npages + 1):
        pw, ph, blocks, perr = extract_page_blocks(pdf, p)
        if blocks is None:
            uncertainties.append({"page": p, "issue": "page extraction failed",
                                  "detail": perr or ""})
            pages_blocks.append([])
            page_heights.append(1.0)
            page_widths.append(1.0)
            page_class.append("error")
            continue
        nwords = sum(len(ln.words) for blk in blocks for ln in blk.lines)
        cls = classify_page(nwords)
        page_class.append(cls)
        if cls == "empty":
            empty_pages.append(p)
        elif cls == "sparse":
            sparse_pages.append(p)
        pages_blocks.append(blocks)
        page_heights.append(ph)
        page_widths.append(pw)

    # OCR for no-text-layer pages only (sparse pages are usually figure pages)
    ocr_detail = []
    if empty_pages:
        if shutil.which("pdftoppm") and shutil.which("tesseract"):
            for p in list(empty_pages):
                blocks, oerr, ostats = ocr_page(pdf, p, page_widths[p - 1],
                                                page_heights[p - 1])
                if blocks:
                    pages_blocks[p - 1] = blocks
                    empty_pages.remove(p)
                    ocr_pages.append(p)
                    ocr_detail.append({"page": p, "source": "tesseract TSV",
                                       "words": ostats["words"] if ostats else None,
                                       "mean_conf": ostats["mean_conf"] if ostats else None,
                                       "verified": False})
                else:
                    uncertainties.append({"page": p, "issue": "OCR failed",
                                          "detail": oerr or ""})
            if ocr_pages:
                uncertainties.append({
                    "issue": "pages recovered by OCR (300dpi + tesseract TSV, "
                             "real coordinates); text NOT human-verified",
                    "pages": ocr_pages, "ocr": True,
                    "detail": ocr_detail})
        else:
            uncertainties.append({
                "issue": "no text layer and no OCR tools (pdftoppm/tesseract missing)",
                "pages": empty_pages,
                "action": "marked unreadable; no text fabricated"})
    if sparse_pages:
        uncertainties.append({
            "issue": "low-text pages (figure/label pages or partial extraction)",
            "pages": sparse_pages,
            "action": "kept as-is; not treated as unreadable"})

    drop_lines, removed_running = detect_running_matter(
        pages_blocks, page_heights, npages)

    ordered = []  # (page, block) in reading order
    for pi, blocks in enumerate(pages_blocks):
        kept = []
        for blk in blocks:
            blk.lines = [ln for ln in blk.lines if id(ln) not in drop_lines]
            if blk.lines:
                kept.append(blk)
        for blk in order_page_blocks(kept, page_widths[pi], page_heights[pi]):
            ordered.append((pi + 1, blk))

    # ------------------------------------------------ structure recognition
    sections, paragraphs, tables, figures, equations, references = [], [], [], [], [], []
    in_refs = False
    cur_ref = None
    sec_stack = []
    md_parts = []
    last_page = [0]
    pending = []          # list of (page, Line) forming the current paragraph
    capture = None        # active table entry being filled
    pending_cont = None   # table closed at a page boundary, awaiting continuation

    def emit_page_anchor(p):
        if p != last_page[0]:
            md_parts.append("\n[p.%d]\n" % p)
            last_page[0] = p

    def flush_pending():
        if not pending:
            return
        text = dehyphenate([ln.text for _, ln in pending])
        if not text:
            pending.clear()
            return
        pages = []
        for p, _ in pending:
            if p not in pages:
                pages.append(p)
        box = union_bbox([ln.bbox for _, ln in pending])
        emit_page_anchor(pages[0])
        paragraphs.append({
            "text": text, "page": pages[0], "pages": pages,
            "bbox": bbox_dict(box),
            "page_bboxes": {str(p): bbox_dict(union_bbox(
                [ln.bbox for pp, ln in pending if pp == p])) for p in pages},
            "n_lines": len(pending),
            "cross_page": len(pages) > 1,
            "section": sec_stack[-1]["title"] if sec_stack else None})
        md_parts.append(text + "\n")
        pending.clear()

    def open_section(title, level, page, bbox):
        while sec_stack and sec_stack[-1]["level"] >= level:
            sec_stack.pop()
        sec = {"title": title, "level": level, "page": page,
               "bbox": bbox_dict(bbox),
               "path": [s["title"] for s in sec_stack] + [title]}
        sections.append(sec)
        sec_stack.append(sec)
        md_parts.append("\n%s %s\n" % ("#" * min(level + 1, 6), title))

    def close_table(next_page):
        nonlocal capture
        if capture is None:
            return
        capture.pop("_last_page", None)
        if capture["line_objs"]:
            grid, md_tbl, unc = build_grid(capture.pop("line_objs"))
            capture["grid"] = grid
            capture["markdown"] = md_tbl
            if unc:
                capture["uncertain"] = True
                capture["uncertainty_detail"] = unc
            md_parts.append(md_tbl + "\n")
        else:
            capture.pop("line_objs")
            capture["note"] = "caption only; no grid lines captured"
        tables.append(capture)
        capture = None

    for page, blk in ordered:
        lines = blk.lines
        if not lines:
            continue

        # page boundary during table capture: close the grid at the page end
        # (rows are never merged across pages) and arm continuation detection
        if capture is not None and page != capture.get("_last_page", page):
            closed_entry = capture
            close_table(page)
            closed_entry["note_page_break"] = (
                "capture stopped at page boundary; rows not merged across pages")
            pending_cont = closed_entry

        # ---------------- references section: split per line
        if in_refs:
            emit_page_anchor(page)
            for ln in lines:
                t = ln.text.strip()
                if not t:
                    continue
                m = REF_ITEM_RE.match(t)
                if m:
                    cur_ref = {"index": m.group(1).strip(" .)[]"), "text": t,
                               "page": page, "bbox": bbox_dict(ln.bbox)}
                    references.append(cur_ref)
                elif cur_ref is not None:
                    cur_ref["text"] += " " + t
                else:
                    cur_ref = {"index": None, "text": t, "page": page,
                               "bbox": bbox_dict(ln.bbox)}
                    references.append(cur_ref)
                md_parts.append(t + "\n")
            continue

        i = 0
        while i < len(lines):
            ln = lines[i]
            t = ln.text.strip()
            i += 1
            if not t:
                continue
            # heading number split onto its own line ("2.1" / "Layout extraction").
            # Merge only when the next line is on the SAME visual row (real split
            # headings) — a number and the next line at a different y belong to
            # different table rows and must never merge into a fake heading.
            nxt_txt = lines[i].text.strip() if i < len(lines) else ""
            same_row = False
            if nxt_txt:
                yc1 = (ln.bbox[1] + ln.bbox[3]) / 2.0
                nb = lines[i].bbox
                yc2 = (nb[1] + nb[3]) / 2.0
                same_row = abs(yc1 - yc2) < 0.5 * max(ln.bbox[3] - ln.bbox[1], 1.0)
            if re.fullmatch(r"\d+(?:\.\d+)*\.?", t) and nxt_txt and same_row \
                    and (nxt_txt[0].isupper() or CJK_RE.match(nxt_txt)) \
                    and all(heading_word_ok(w) for w in nxt_txt.split()):
                t = "%s %s" % (t, nxt_txt)
                merged_box = union_bbox([ln.bbox, lines[i].bbox])
                i += 1
            else:
                merged_box = ln.bbox

            # ---------------- table continuation after a page break
            if pending_cont is not None and capture is None:
                tm_c = TAB_CAP_RE.match(t)
                if tm_c and TAB_CONT_RE.search(t):
                    flush_pending()
                    emit_page_anchor(page)
                    capture = {"label": tm_c.group(1), "caption": t, "page": page,
                               "bbox": bbox_dict(merged_box), "continued": True,
                               "continuation_of": pending_cont["label"],
                               "note": "explicit continuation caption; grid kept "
                                       "separate per page",
                               "line_objs": [], "_last_page": page}
                    pending_cont = None
                    md_parts.append("**%s**\n" % t)
                    continue
                if not heading_level(t) and not FIG_CAP_RE.match(t) \
                        and not is_prose_line(t):
                    flush_pending()
                    capture = {"label": pending_cont["label"] + " (cont.)",
                               "caption": pending_cont["caption"],
                               "page": page, "bbox": bbox_dict(merged_box),
                               "continued": True,
                               "continuation_of": pending_cont["label"],
                               "uncertain": True,
                               "note": "no repeated caption; continuation inferred "
                                       "from page-break position (uncertain)",
                               "line_objs": [], "_last_page": page}
                    pending_cont = None
                    # fall through: capture-mode branch below consumes this line
                else:
                    pending_cont = None

            # ---------------- table capture mode
            if capture is not None:
                lvl = heading_level(t)
                if lvl or FIG_CAP_RE.match(t) or TAB_CAP_RE.match(t) \
                        or is_prose_line(t) \
                        or len(capture["line_objs"]) >= MAX_TABLE_LINES:
                    close_table(page)
                    # fall through: process this line normally below
                else:
                    capture["line_objs"].append((page, ln))
                    capture["_last_page"] = page
                    capture["bbox"] = bbox_dict(union_bbox([
                        (capture["bbox"]["x0"], capture["bbox"]["y0"],
                         capture["bbox"]["x1"], capture["bbox"]["y1"]),
                        ln.bbox]))
                    continue

            # ---------------- headings
            lvl = heading_level(t)
            if lvl:
                flush_pending()
                emit_page_anchor(page)
                open_section(t, lvl, page, merged_box)
                if SEC_NAME_RE.match(t) and SEC_NAME_RE.match(t).group(1).lower() in (
                        "references", "bibliography", "参考文献"):
                    flush_pending()
                    in_refs = True
                continue

            # ---------------- captions
            fm = FIG_CAP_RE.match(t)
            tm = TAB_CAP_RE.match(t)
            if fm or tm:
                flush_pending()
                emit_page_anchor(page)
                if fm:
                    figures.append({"label": fm.group(1), "caption": t,
                                    "page": page, "bbox": bbox_dict(merged_box)})
                    md_parts.append("**%s**\n" % t)
                else:
                    capture = {"label": tm.group(1), "caption": t,
                               "page": page, "bbox": bbox_dict(merged_box),
                               "continued": bool(TAB_CONT_RE.search(t)),
                               "line_objs": [], "_last_page": page}
                    if capture["continued"]:
                        capture["note"] = "continuation of a table from a previous page"
                    md_parts.append("**%s**\n" % t)
                continue

            # ---------------- numbered equations (require math signal)
            em = EQN_NUM_RE.search(t)
            if em and len(t) < 200 and MATH_SIGNAL_RE.search(t):
                eq_lines = [(page, ln)]
                while pending and is_mathy_short(pending[-1][1].text):
                    eq_lines.insert(0, pending.pop())
                box = union_bbox([l_.bbox for _, l_ in eq_lines])
                text = dehyphenate([l_.text for _, l_ in eq_lines])
                flush_pending()
                equations.append({"text": text, "number": em.group(1),
                                  "page": page, "bbox": bbox_dict(box),
                                  "multiline": len(eq_lines) > 1,
                                  "uncertain": False,
                                  "note": "symbols as extracted; super/subscript "
                                          "layout not preserved — verify via bbox"})
                emit_page_anchor(page)
                md_parts.append("$$\n%s\n$$\n" % text)
                continue

            pending.append((page, ln))

        # block end: keep pending across blocks/pages unless the sentence closed
        if capture is None and pending:
            joined = dehyphenate([l_.text for _, l_ in pending])
            if joined.rstrip().endswith(SENT_END):
                flush_pending()

    close_table(ordered[-1][0] if ordered else npages)
    flush_pending()

    # -------------------------------------------------------------- metadata
    page1_text = " ".join(ln.text for pg, blk in ordered if pg == 1
                          for ln in blk.lines)
    metadata = extract_metadata(ordered, sections, paragraphs,
                                removed_running, page1_text)

    n_words = sum(len(p["text"].split()) for p in paragraphs)
    if empty_pages and len(empty_pages) == npages:
        status = "unreadable"
    elif not paragraphs:
        status = "partial"
    elif empty_pages or "error" in page_class:
        status = "partial"
    else:
        status = "success"

    ocr_blocks = sum(1 for pg, blk in ordered if blk.source == "ocr")
    result = {
        "status": status,
        "input": {"file": str(pdf), "pages": npages},
        "metadata": metadata,
        "sections": sections,
        "paragraphs": paragraphs,
        "tables": tables,
        "figures": figures,
        "equations": equations,
        "references": references,
        "removed_running_matter": removed_running,
        "uncertainties": uncertainties,
        "stats": {"pages": npages, "paragraphs": len(paragraphs),
                  "sections": len(sections), "tables": len(tables),
                  "figures": len(figures), "equations": len(equations),
                  "references": len(references), "body_words": n_words,
                  "page_classes": {"text": page_class.count("text"),
                                   "sparse": page_class.count("sparse"),
                                   "empty": page_class.count("empty")},
                  "ocr_pages": ocr_pages, "sparse_pages": sparse_pages,
                  "empty_pages": empty_pages, "ocr_detail": ocr_detail},
        "changes": [],
    }
    return result, "\n".join(md_parts), None


def main():
    ap = argparse.ArgumentParser(
        description="Parse a scientific PDF into structured JSON + Markdown")
    ap.add_argument("pdf", help="path to input PDF")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--case-id", default=None, help="case id to embed in result.json")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    if not pdf.is_file():
        print("error: input not found: %s" % pdf, file=sys.stderr)
        return 1
    if shutil.which("pdftotext") is None:
        print("error: pdftotext (Poppler) not found on PATH; install poppler-utils",
              file=sys.stderr)
        return 1

    log = []
    result, md, fatal = parse_document(pdf, log)
    if fatal:
        print("error: %s" % fatal, file=sys.stderr)
        return 1

    result["runtime"] = {"tool": "pdftotext -bbox-layout (Poppler)",
                         "ocr": "pdftoppm + tesseract TSV @ %ddpi" % OCR_DPI,
                         "seconds": round(time.time() - t0, 2)}
    if args.case_id:
        result["case_id"] = args.case_id

    (outdir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "fulltext.md").write_text(md or "", encoding="utf-8")
    print("status=%s pages=%s paragraphs=%s sections=%s tables=%s refs=%s -> %s" % (
        result["status"], result["stats"]["pages"], result["stats"]["paragraphs"],
        result["stats"]["sections"], result["stats"]["tables"],
        result["stats"]["references"], outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
