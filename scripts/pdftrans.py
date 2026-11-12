#!/usr/bin/env python3
"""pdftrans - helper CLI for the pdf-paper-translation skill (single file, cross-platform).

Subcommands
  extract  PDF OUTDIR                 page text + manifest  -> OUTDIR/_work/
  figures  PDF OUTDIR                 auto-locate and crop every figure -> OUTDIR/figures/
  render   PDF OUTDIR --page N --clip x0,y0,x1,y1 --name fig03   manual crop (PDF points)
  pages    PDF OUTDIR --pages 3,5-7 [--grid]                     page renders for review
  trim     IMG [IMG ...] [--out PATH]  trim white margins + cap pixel budget (in place)
  check    PATH                        validate translation(s); PATH = paper folder or library root
  cards    ROOT [--md]                 build ROOT/cards.jsonl (+ cards.md) from YAML headers

Image budget (what the reading model can see without downscaling) is configurable:
  --max-px / --max-side, or env PDFTRANS_MAX_PX / PDFTRANS_MAX_SIDE.
  Default 1150000 px and 1568 px long side: safe for Claude, GPT-4o class and most readers.

Requires: PyMuPDF (extract/figures/render/pages), Pillow (figures/render/pages/trim; check uses it
for image sizes if present). PyYAML is optional (stricter front-matter validation).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

VERSION = "2.0"
WORK = "_work"
MAX_PX = int(os.environ.get("PDFTRANS_MAX_PX") or 1_150_000)
MAX_SIDE = int(os.environ.get("PDFTRANS_MAX_SIDE") or 1568)
CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef"
SKIP_DIRS = {"node_modules", "__pycache__", "重复文件", "重复", "_调试文件", "_debug"}


# ----------------------------------------------------------------------------- utils
def _safe_console() -> None:
    """Never crash on a console that cannot encode a character (e.g. GBK pipes on Windows)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(f"ERROR: {msg}\n")
    raise SystemExit(code)


def need_fitz():
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
    except ImportError:
        try:
            import fitz  # type: ignore  # older PyMuPDF
        except ImportError:
            die("需要 PyMuPDF：pip install pymupdf", 3)
    return fitz


def need_pil():
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: F401
        import PIL.Image
        return PIL
    except ImportError:
        die("需要 Pillow：pip install pillow", 3)


def work_dir(outdir: str) -> str:
    d = os.path.join(outdir, WORK)
    os.makedirs(d, exist_ok=True)
    return d


def load_manifest(outdir: str) -> dict:
    p = os.path.join(outdir, WORK, "manifest.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_manifest(outdir: str, data: dict) -> None:
    p = os.path.join(work_dir(outdir), "manifest.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def parse_pages(spec: str, n: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    bad = [p for p in out if not 1 <= p <= n]
    if bad:
        die(f"页码超出范围 1..{n}：{bad}")
    return sorted(set(out))


def join_lines(text: str) -> str:
    """Join the visual lines of one text block into one paragraph line."""
    out = ""
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if not out:
            out = ln
        elif re.search(f"[{CJK}]$", out) and re.match(f"[{CJK}]", ln):
            out += ln
        else:
            out += " " + ln
    return out


# ----------------------------------------------------------------------------- text + captions
CAP_RE = re.compile(
    r"^\s*(?P<label>Extended\s+Data\s+Fig(?:ure)?s?\.?|Supplementary\s+Fig(?:ure)?s?\.?|"
    r"Fig(?:ure)?s?\.?|FIG(?:URE)?S?\.?|Scheme|SCHEME|Table|TABLE|Chart|Plate|"
    r"图|表|圖|図|Abb\.|Abbildung|Tab\.|Tabelle)\s*"
    r"(?P<num>S?\d{1,3}[A-Za-z]?|[IVX]{1,5})(?!\d)\s*"
    r"(?:[.:|：．、—–-]|\s+(?=[^a-z\s])|$)"
)
BODY_AFTER_RE = re.compile(
    r"^\s*(?:[A-Za-z](?:[,–-]?[A-Za-z])?\s+)?(?:and|or|to|show|shows|showed|shown|illustrat\w*|demonstrat\w*|"
    r"reveal\w*|indicat\w*|present\w*|display\w*|depict\w*|confirm\w*|suggest\w*|summari\w*|compar\w*)\b",
    re.IGNORECASE)


def caption_kind(label: str) -> str:
    l = label.lower()
    if l.startswith("extended"):
        return "extfig"
    if l.startswith("supplementary"):
        return "suppfig"
    if l.startswith(("table", "tab", "表")):
        return "table"
    if l.startswith("scheme"):
        return "scheme"
    return "fig"


def fig_name(kind: str, num: str) -> str:
    m = re.match(r"(S?)(\d+)([A-Za-z]?)$", num)
    if m:
        s, d, suf = m.groups()
        core = f"{s}{int(d):02d}{suf.lower()}"
    else:
        core = num
    if kind == "suppfig" and not core.startswith("S"):
        core = "S" + core
    return {"fig": "fig", "suppfig": "fig", "extfig": "extfig", "scheme": "scheme", "table": "table"}[kind] + core


class TB:
    """A text block on a page."""

    __slots__ = ("rect", "text", "nchars", "nlines", "cap")

    def __init__(self, rect, text: str):
        self.rect = rect
        self.text = text
        self.nchars = len(re.sub(r"\s+", "", text))
        self.nlines = text.count("\n") + 1
        self.cap = None

    @property
    def para(self) -> bool:
        return self.nchars >= 200 or (self.nlines >= 3 and self.nchars / self.nlines >= 45)


SUP = str.maketrans("0123456789+-−–=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁻⁼⁽⁾ⁿ")
SUB = str.maketrans("0123456789+-−=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎")


def _script(text: str, table: dict) -> str | None:
    t = text.strip()
    if t and all(ord(ch) in table for ch in t):
        return text.translate(table)
    return None


def page_blocks(fitz, page) -> list[TB]:
    """Text blocks with superscripts / subscripts restored as Unicode (10⁸, mL⁻¹, H₂O₂).

    PDF text layers put raised digits on the baseline ("10 8" -> "108"), which silently changes
    numbers by orders of magnitude. Small spans raised above / dropped below the line are mapped."""
    flags = 0
    for f in ("TEXT_DEHYPHENATE", "TEXT_MEDIABOX_CLIP"):
        flags |= getattr(fitz, f, 0)
    out: list[TB] = []
    for b in page.get_text("dict", flags=flags).get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        sizes = sorted(sp["size"] for ln in b["lines"] for sp in ln["spans"] if sp["text"].strip())
        if not sizes:
            continue
        body = sizes[len(sizes) // 2]
        lines = []
        for ln in b["lines"]:
            spans = [sp for sp in ln["spans"] if sp["text"]]
            if not spans:
                continue
            big = [sp for sp in spans if sp["size"] >= 0.9 * body] or spans
            base = sorted(sp["origin"][1] for sp in big)[len(big) // 2]
            txt = ""
            for sp in spans:
                t = sp["text"]
                if sp["size"] < 0.85 * body and t.strip():
                    dy = sp["origin"][1] - base
                    conv = None
                    if (sp.get("flags", 0) & 1) or dy < -0.15 * body:
                        conv = _script(t, SUP)
                    elif dy > 0.1 * body:
                        conv = _script(t, SUB)
                    if conv is not None:
                        txt = txt.rstrip(" ") + conv.strip()
                        continue
                txt += t
            lines.append(txt)
        text = "\n".join(lines)
        if text.strip():
            out.append(TB(fitz.Rect(b["bbox"]), text))
    return out


def reading_order(blocks: list[TB], prect) -> list[TB]:
    """Column-aware order: full-width blocks split the page into bands; inside a band, left column first."""
    mid = prect.x0 + prect.width / 2

    def spans(b: TB) -> bool:
        return b.rect.x0 < mid - 5 and b.rect.x1 > mid + 5

    full = sorted([b for b in blocks if spans(b)], key=lambda b: (b.rect.y0, b.rect.x0))
    rest = [b for b in blocks if not spans(b)]
    out: list[TB] = []
    top = -1e9
    for s in full + [None]:
        lim = s.rect.y0 if s else 1e9
        band = [b for b in rest if top <= (b.rect.y0 + b.rect.y1) / 2 < lim]
        left = sorted([b for b in band if (b.rect.x0 + b.rect.x1) / 2 < mid], key=lambda b: (b.rect.y0, b.rect.x0))
        right = sorted([b for b in band if (b.rect.x0 + b.rect.x1) / 2 >= mid], key=lambda b: (b.rect.y0, b.rect.x0))
        out += left + right
        if s:
            out.append(s)
            top = s.rect.y0
    return out


def find_captions(pno: int, ordered: list[TB], seen: set) -> list[dict]:
    caps = []
    for i, b in enumerate(ordered):
        flat = re.sub(r"\s+", " ", b.text).strip()
        m = CAP_RE.match(flat[:60])
        if not m or BODY_AFTER_RE.match(flat[m.end():m.end() + 60]):
            continue
        if len(flat) < 30 and i + 1 < len(ordered):  # label split from its text
            flat = flat + " " + re.sub(r"\s+", " ", ordered[i + 1].text).strip()
        kind = caption_kind(m.group("label"))
        num = m.group("num")
        key = (kind, num)
        cap = {"page": pno, "kind": kind, "num": num, "bbox": [round(v, 1) for v in b.rect],
               "text": flat[:300], "continued": key in seen}
        seen.add(key)
        b.cap = cap
        caps.append(cap)
    return caps


REF_MARK_RE = re.compile(r"(?m)^[ \t]*(?:\[(\d{1,4})\]|\((\d{1,4})\)|(\d{1,4})\.(?=\s)|(\d{1,4})(?=[ \t]+[A-Z]))")


def estimate_refs(raw_text: str) -> int | None:
    """Longest run 1,2,3,... of line-start markers of one style ([n] / (n) / n. / n Name).

    Works without a 'References' heading (Wiley/ACS put none, or prefix it with a symbol).
    One missing number is tolerated (page breaks, text-layer glitches)."""
    marks: dict[int, list[int]] = {0: [], 1: [], 2: [], 3: []}
    for m in REF_MARK_RE.finditer(raw_text):
        for style in range(4):
            if m.group(style + 1):
                marks[style].append(int(m.group(style + 1)))
    best = 0
    for seq in marks.values():
        starts = [i for i, n in enumerate(seq) if n == 1][-6:]
        for s in starts:
            last = 1
            for n in seq[s + 1:]:
                if n == last + 1 or n == last + 2:
                    last = n
            best = max(best, last)
    return best if best >= 8 else None


def cmd_extract(a) -> int:
    fitz = need_fitz()
    pdf, out = os.path.abspath(a.pdf), os.path.abspath(a.outdir)
    if not os.path.isfile(pdf):
        die(f"找不到 PDF：{pdf}")
    wd = work_dir(out)
    pages_txt, raw_all, counts, captions, scanned = [], [], [], [], []
    seen: set = set()
    with fitz.open(pdf) as doc:
        if doc.needs_pass:
            die("PDF 已加密，需要密码", 3)
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        for pno, page in enumerate(doc, 1):
            ordered = reading_order(page_blocks(fitz, page), page.rect)
            captions += find_captions(pno, ordered, seen)
            paras = [join_lines(b.text) for b in ordered]
            pages_txt.append("\n\n".join(p for p in paras if p))
            raw = page.get_text("text")
            raw_all.append(raw)
            c = len(re.sub(r"\s+", "", raw))
            counts.append(c)
            if c < 50 and page.get_images():
                scanned.append(pno)
        npages = doc.page_count
    with open(os.path.join(wd, "source.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for i, t in enumerate(pages_txt, 1):
            fh.write(f"<<<PAGE {i}>>>\n{t}\n\n")
    refs = estimate_refs("\n".join(raw_all))
    figs = sorted({(c["kind"], c["num"]) for c in captions if c["kind"] != "table" and not c["continued"]})
    tabs = sorted({c["num"] for c in captions if c["kind"] == "table" and not c["continued"]})
    old = load_manifest(out)
    manifest = {"tool": f"pdftrans {VERSION}", "pdf": pdf, "pages": npages, "metadata": meta,
                "chars_per_page": counts, "scanned_pages": scanned, "references_estimate": refs,
                "captions": captions, "figures": old.get("figures", [])}
    save_manifest(out, manifest)
    print(f"pages={npages}  chars={sum(counts)}  figures={len(figs)}  tables={len(tabs)}  "
          f"references~{refs if refs else '?'}")
    print(f"-> {os.path.join(wd, 'source.txt')}")
    if scanned:
        print(f"WARNING: 疑似扫描页（几乎无文字层）：{scanned[:20]}{' ...' if len(scanned) > 20 else ''}")
        if len(scanned) == npages:
            print("整份 PDF 没有文字层：需要先 OCR，不要凭图片臆测内容。")
            return 4
    return 0


# ----------------------------------------------------------------------------- rendering
def bg_box(img, thr: int = 12):
    """Bounding box of non-background pixels (background = median colour of the border)."""
    PIL = need_pil()
    from PIL import ImageChops, ImageStat
    im = img.convert("RGB")
    w, h = im.size
    r = max(1, min(4, w // 8, h // 8))
    strips = [im.crop((0, 0, w, r)), im.crop((0, h - r, w, h)), im.crop((0, 0, r, h)), im.crop((w - r, 0, w, h))]
    med = [ImageStat.Stat(s).median for s in strips]
    bg = tuple(int(sum(m[i] for m in med) / 4) for i in range(3))
    diff = ImageChops.difference(im, PIL.Image.new("RGB", im.size, bg)).convert("L")
    return diff.point(lambda v: 255 if v > thr else 0).getbbox()


def zoom_for(rect, max_px: int, max_side: int) -> float:
    w, h = max(rect.width, 1.0), max(rect.height, 1.0)
    return max(0.5, min(math.sqrt((max_px - 2000) / (w * h)), (max_side - 2) / max(w, h), 600 / 72))


def render_clip(fitz, page, rect, path: str, max_px: int, max_side: int, trim: bool = True, pad_px: int = 8):
    """Render rect at the zoom that fills the pixel budget; optionally re-render on the trimmed box."""
    PIL = need_pil()
    import io
    rect = fitz.Rect(rect) & page.rect
    if rect.is_empty or rect.width < 4 or rect.height < 4:
        raise ValueError(f"裁切框无效：{tuple(round(v, 1) for v in rect)}")
    z = zoom_for(rect, max_px, max_side)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=rect, alpha=False)
    if trim:
        img = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
        box = bg_box(img)
        if box:
            pad = pad_px / z
            nr = fitz.Rect(rect.x0 + box[0] / z - pad, rect.y0 + box[1] / z - pad,
                           rect.x0 + box[2] / z + pad, rect.y0 + box[3] / z + pad) & rect
            if nr.width * nr.height < rect.width * rect.height * 0.97 and nr.width > 8 and nr.height > 8:
                rect = nr
                z = zoom_for(rect, max_px, max_side)
                pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=rect, alpha=False)
    img = PIL.Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return rect, img.size


def in_margin(r, pr, frac: float = 0.08) -> bool:
    """Entirely inside the top/bottom running-header band, or a thin vertical side strip."""
    band = frac * pr.height
    return r.y1 < pr.y0 + band or r.y0 > pr.y1 - band or (r.width < 15 and (r.x0 > pr.x1 - 0.06 * pr.width or r.x1 < pr.x0 + 0.06 * pr.width))


def graphic_rects(fitz, page) -> list:
    pr = page.rect
    parea = pr.width * pr.height
    rects = []
    try:
        log = page.get_bboxlog()
        for kind, bb in log:
            if kind in ("fill-path", "stroke-path", "fill-image", "fill-imgmask", "fill-shade"):
                rects.append(fitz.Rect(bb))
    except Exception:
        for info in page.get_image_info():
            rects.append(fitz.Rect(info["bbox"]))
        for d in page.get_drawings():
            rects.append(fitz.Rect(d["rect"]))
    out = []
    for r in rects:
        r = r & pr
        if r.is_empty and not (r.width > 0 or r.height > 0):
            continue
        if r.width * r.height > 0.8 * parea:
            continue  # page background / frame
        if (r.height < 2 and r.width > 0.85 * pr.width) or (r.width < 2 and r.height > 0.85 * pr.height):
            continue  # page-wide rules
        if in_margin(r, pr):
            continue  # running header / footer logos and rules
        out.append(r)
    return out


def _grow_with_labels(fitz, r, blocks, lo, hi, pr):
    """Pull in short text blocks (axis labels, panel letters, legends) touching the region."""
    for _ in range(3):
        grow = fitz.Rect(r.x0 - 8, r.y0 - 8, r.x1 + 8, r.y1 + 8)
        for b in blocks:
            if b.para or b.cap or not b.rect.intersects(grow) or in_margin(b.rect, pr):
                continue
            if b.rect.y0 >= lo - 2 and b.rect.y1 <= hi + 2:
                r |= b.rect
    return r


def _union(fitz, rects):
    r = fitz.Rect(rects[0])
    for g in rects[1:]:
        r |= g
    return r


def locate(fitz, page, cap: dict, blocks: list[TB], graphics: list, prev=None):
    """Return (rect, method, page_index_offset) for the figure/table that belongs to caption `cap`.

    Search order: graphics between the caption and the nearest paragraph above (below for tables),
    then the other side, then an unclaimed full figure on the previous page (journals that put the
    figure on one page and its caption at the top of the next), then the empty band above."""
    pr = page.rect
    mid = pr.x0 + pr.width / 2
    c = fitz.Rect(cap["bbox"])
    if c.x0 < mid - 5 and c.x1 > mid + 5:
        sx0, sx1 = pr.x0, pr.x1
    elif (c.x0 + c.x1) / 2 < mid:
        sx0, sx1 = pr.x0, mid
    else:
        sx0, sx1 = mid, pr.x1

    def touches(r, x0=None, x1=None) -> bool:
        x0 = sx0 if x0 is None else x0
        x1 = sx1 if x1 is None else x1
        ov = min(r.x1, x1) - max(r.x0, x0)
        return ov > 0.3 * min(max(r.width, 0.1), x1 - x0)

    barriers = [b for b in blocks if (b.para or b.cap) and b.cap is not cap and touches(b.rect)]

    def search(direction: str):
        if direction == "above":
            lim = max([b.rect.y1 for b in barriers if b.rect.y1 <= c.y0 + 1] + [pr.y0])
            cands = [g for g in graphics if touches(g) and g.y0 >= lim - 2 and g.y1 <= c.y0 + 3]
            return cands, lim, c.y0
        lim = min([b.rect.y0 for b in barriers if b.rect.y0 >= c.y1 - 1] + [pr.y1])
        cands = [g for g in graphics if touches(g) and g.y0 >= c.y1 - 3 and g.y1 <= lim + 2]
        return cands, c.y1, lim

    order = ("below", "above") if cap["kind"] == "table" else ("above", "below")
    for direction in order:
        cands, lo, hi = search(direction)
        if not cands or sum(g.width * g.height for g in cands) < 400:
            continue
        r = _grow_with_labels(fitz, _union(fitz, cands), blocks, lo, hi, pr)
        x0, x1 = (pr.x0, pr.x1) if (r.x0 < mid - 20 and r.x1 > mid + 20) else (sx0, sx1)
        r = fitz.Rect(max(r.x0 - 2, x0 - 6), max(r.y0 - 2, lo), min(r.x1 + 2, x1 + 6), min(r.y1 + 2, hi)) & pr
        return r, direction, 0

    if prev is not None and cap["kind"] != "table":
        ppage, pblocks, pgraphics, claimed = prev
        ppr = ppage.rect
        band = 0.07 * ppr.height
        free = [g for g in pgraphics
                if g.height >= 3 and g.y1 > ppr.y0 + band and g.y0 < ppr.y1 - band
                and not any(g.intersects(cl) for cl in claimed)
                and not any(b.para and b.rect.intersects(g) for b in pblocks)]
        if free:
            r = _union(fitz, free)
            if r.width * r.height > 0.15 * ppr.width * ppr.height:
                r = _grow_with_labels(fitz, r, pblocks, ppr.y0 + band, ppr.y1 - band, ppr)
                return fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2) & ppr, "prev-page", -1

    cands, lo, hi = search("above")
    if hi - lo > 80:
        return fitz.Rect(sx0, lo, sx1, hi), "band", 0
    return None, "none", 0


def make_overview(entries: list[dict], outdir: str, max_px: int) -> list[str]:
    PIL = need_pil()
    from PIL import ImageDraw, ImageFont
    files = []
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()
    chunk = 12
    for k in range(0, len(entries), chunk):
        part = entries[k:k + chunk]
        wide = sum(1 for e in part if e.get("size") and e["size"][0] > 1.6 * e["size"][1]) > len(part) / 2
        cols = min(2 if wide else 4, len(part))
        rows = math.ceil(len(part) / cols)
        cw, ch, lab = (760, 420, 28) if wide else (420, 420, 28)
        sheet = PIL.Image.new("RGB", (cols * cw, rows * (ch + lab)), "white")
        d = ImageDraw.Draw(sheet)
        for i, e in enumerate(part):
            x, y = (i % cols) * cw, (i // cols) * (ch + lab)
            try:
                im = PIL.Image.open(os.path.join(outdir, e["file"])).convert("RGB")
                im.thumbnail((cw - 10, ch - 10))
                sheet.paste(im, (x + 5, y + lab))
                d.rectangle([x + 4, y + lab - 1, x + 5 + im.width, y + lab + im.height], outline=(200, 0, 0))
            except Exception:
                pass
            d.text((x + 6, y + 4), f"{e['name']}  p{e['page']}  [{e['method']}]", fill=(0, 0, 0), font=font)
        if sheet.width * sheet.height > max_px:
            s = math.sqrt(max_px / (sheet.width * sheet.height))
            sheet = sheet.resize((int(sheet.width * s), int(sheet.height * s)), PIL.Image.LANCZOS)
        p = os.path.join(outdir, WORK, f"figures_overview_{k // chunk + 1}.png")
        sheet.save(p, optimize=True)
        files.append(p)
    return files


def cmd_figures(a) -> int:
    fitz = need_fitz()
    need_pil()
    pdf, out = os.path.abspath(a.pdf), os.path.abspath(a.outdir)
    if not os.path.isfile(pdf):
        die(f"找不到 PDF：{pdf}")
    figdir = os.path.join(out, "figures")
    man = load_manifest(out)
    kinds = {"fig", "suppfig", "extfig", "scheme"} | ({"table"} if a.tables else set())
    only = {s.strip() for s in a.only.split(",")} if a.only else None
    keep = [e for e in man.get("figures", []) if e.get("method") == "manual"]
    keep_names = {e["name"] for e in keep}
    entries: list[dict] = []
    seen: set = set()
    with fitz.open(pdf) as doc:
        pages = parse_pages(a.pages, doc.page_count) if a.pages else range(1, doc.page_count + 1)
        ctx: dict[int, list] = {}  # pno -> [page, blocks, graphics, claimed rects]
        for pno in range(1, doc.page_count + 1):  # walk all pages so continuation numbering is right
            page = doc[pno - 1]
            blocks = reading_order(page_blocks(fitz, page), page.rect)
            caps = find_captions(pno, blocks, seen)
            ctx[pno] = [page, blocks, None, []]
            ctx.pop(pno - 2, None)
            if pno not in pages:
                continue
            for cap in caps:
                if cap["kind"] not in kinds:
                    continue
                name = fig_name(cap["kind"], cap["num"])
                n, base = 2, name
                while any(e["name"] == name for e in entries):
                    name = f"{base}_part{n}"
                    n += 1
                if (only and name not in only) or (name in keep_names and not a.force):
                    continue
                if ctx[pno][2] is None:
                    ctx[pno][2] = graphic_rects(fitz, page)
                prev = None
                if pno - 1 in ctx:
                    pc = ctx[pno - 1]
                    if pc[2] is None:
                        pc[2] = graphic_rects(fitz, pc[0])
                    prev = (pc[0], pc[1], pc[2], pc[3])
                rect, method, dp = locate(fitz, page, cap, blocks, ctx[pno][2], prev)
                fpno = pno + dp
                e = {"name": name, "file": f"figures/{name}.png", "page": fpno, "kind": cap["kind"],
                     "num": cap["num"], "method": method, "caption": cap["text"][:160]}
                if rect is None:
                    e["file"] = None
                    print(f"  {name:<14} p{pno:<3} NOT FOUND - 用 pages --grid 看页面后 render --clip 手动出图")
                else:
                    ctx[fpno][3].append(rect)
                    r, size = render_clip(fitz, doc[fpno - 1], rect, os.path.join(out, e["file"]),
                                          a.max_px, a.max_side)
                    e["bbox"] = [round(v, 1) for v in r]
                    e["size"] = list(size)
                    flag = "  <- 请目视确认" if method in ("band", "below", "prev-page") and cap["kind"] != "table" else ""
                    print(f"  {name:<14} p{fpno:<3} {size[0]}x{size[1]}  [{method}]{flag}")
                entries.append(e)
    merged = {e["name"]: e for e in man.get("figures", [])}
    if not only and not a.pages:
        merged = {k: v for k, v in merged.items() if v.get("method") == "manual"}
    for e in entries:
        merged[e["name"]] = e
    man.setdefault("pdf", pdf)
    man["figures"] = sorted(merged.values(), key=lambda e: (e["page"], e["name"]))
    save_manifest(out, man)
    ok = [e for e in man["figures"] if e.get("file")]
    if ok and not a.no_overview:
        for p in make_overview(ok, out, a.max_px):
            print(f"overview -> {p}")
    nman = sum(1 for e in man["figures"] if e.get("method") == "manual" and e["name"] not in {x["name"] for x in entries})
    print(f"done: 自动 {len(entries)} 张" + (f" + 手动保留 {nman} 张" if nman else "") + f"  (预算 {a.max_px} px / 长边 {a.max_side})")
    print("总览图只用于核对裁切边界和分图是否齐全；小字是否清楚请逐张打开 figures/*.png")
    return 0


def cmd_render(a) -> int:
    fitz = need_fitz()
    out = os.path.abspath(a.outdir)
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", a.name):
        die("--name 只能含字母数字、下划线、连字符，如 fig03 / fig03-c / figS01")
    try:
        clip = [float(v) for v in a.clip.split(",")] if a.clip else None
    except ValueError:
        die("--clip 格式：x0,y0,x1,y1（PDF 点坐标）")
    with fitz.open(os.path.abspath(a.pdf)) as doc:
        if not 1 <= a.page <= doc.page_count:
            die(f"--page 须在 1..{doc.page_count}")
        page = doc[a.page - 1]
        rect = fitz.Rect(clip) if clip else page.rect
        rel = f"figures/{a.name}.png"
        r, size = render_clip(fitz, page, rect, os.path.join(out, rel), a.max_px, a.max_side, trim=not a.no_trim)
    man = load_manifest(out)
    figs = [e for e in man.get("figures", []) if e["name"] != a.name]
    figs.append({"name": a.name, "file": rel, "page": a.page, "kind": "manual", "method": "manual",
                 "bbox": [round(v, 1) for v in r], "size": list(size)})
    man["figures"] = sorted(figs, key=lambda e: (e["page"], e["name"]))
    save_manifest(out, man)
    print(f"{rel}  {size[0]}x{size[1]}  bbox={tuple(round(v, 1) for v in r)}")
    return 0


def cmd_pages(a) -> int:
    fitz = need_fitz()
    PIL = need_pil()
    from PIL import ImageDraw, ImageFont
    import io
    out = os.path.abspath(a.outdir)
    pdir = os.path.join(work_dir(out), "pages")
    os.makedirs(pdir, exist_ok=True)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    with fitz.open(os.path.abspath(a.pdf)) as doc:
        for pno in parse_pages(a.pages, doc.page_count):
            page = doc[pno - 1]
            z = zoom_for(page.rect, a.max_px, a.max_side)
            img = PIL.Image.open(io.BytesIO(page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png"))).convert("RGB")
            if a.grid:
                d = ImageDraw.Draw(img)
                step = a.grid_step
                x = 0.0
                while x <= page.rect.width:
                    X = x * z
                    d.line([(X, 0), (X, img.height)], fill=(0, 160, 255) if x % (step * 2) else (255, 60, 60), width=1)
                    d.text((X + 2 if X > 0 else 2, 2), str(int(x + page.rect.x0)), fill=(255, 0, 0), font=font)
                    x += step
                y = 0.0
                while y <= page.rect.height:
                    Y = y * z
                    d.line([(0, Y), (img.width, Y)], fill=(0, 160, 255) if y % (step * 2) else (255, 60, 60), width=1)
                    d.text((2, Y + 2), str(int(y + page.rect.y0)), fill=(255, 0, 0), font=font)
                    y += step
            p = os.path.join(pdir, f"p{pno:03d}{'_grid' if a.grid else ''}.png")
            img.save(p, optimize=True)
            print(f"p{pno} -> {p}  ({img.width}x{img.height}; 网格数字 = PDF 点坐标)" if a.grid else f"p{pno} -> {p}")
    return 0


def cmd_trim(a) -> int:
    PIL = need_pil()
    if a.out and len(a.images) > 1:
        die("--out 只能配合单张图片")
    for src in a.images:
        if not os.path.isfile(src):
            sys.stderr.write(f"skip: 不存在 {src}\n")
            continue
        im = PIL.Image.open(src)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = PIL.Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = PIL.Image.alpha_composite(bg, im)
        im = im.convert("RGB")
        w0, h0 = im.size
        box = bg_box(im)
        if box:
            p = a.pad
            box = (max(0, box[0] - p), max(0, box[1] - p), min(w0, box[2] + p), min(h0, box[3] + p))
            if (box[2] - box[0]) >= 32 and (box[3] - box[1]) >= 32:
                im = im.crop(box)
        s = min(1.0, math.sqrt(a.max_px / (im.width * im.height)), a.max_side / max(im.size))
        if s < 1.0:
            im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), PIL.Image.LANCZOS)
        dst = a.out or src
        if not dst.lower().endswith(".png"):
            dst = os.path.splitext(dst)[0] + ".png"
        im.save(dst, "PNG", optimize=True)
        print(f"{os.path.basename(src)}: {w0}x{h0} -> {im.width}x{im.height}  -> {dst}")
    return 0


# ----------------------------------------------------------------------------- front matter
def parse_front_matter(text: str):
    """Return (fields|None, body, body_offset_lines, errors). Values may be quoted or plain."""
    if not text.startswith("---\n"):
        return None, text, 0, []
    end = text.find("\n---\n", 3)
    if end < 0:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return None, text, 0, ["YAML 头没有结束的 ---"]
    raw = text[4:end]
    fields: dict[str, str] = {}
    errs: list[str] = []
    for i, ln in enumerate(raw.splitlines(), 2):
        if not ln.strip() or ln.lstrip().startswith("#") or ln.startswith((" ", "\t", "-")):
            continue
        k, sep, v = ln.partition(":")
        if not sep:
            errs.append(f"第 {i} 行不是 key: value")
            continue
        v = v.strip()
        if v[:1] in ("'", '"'):
            q = v[0]
            if len(v) < 2 or not v.endswith(q):
                errs.append(f"第 {i} 行 {k.strip()}：引号未闭合")
            v = v[1:-1]
            v = v.replace('\\"', '"').replace("\\\\", "\\") if q == '"' else v.replace("''", "'")
        elif ": " in v or v.endswith(":") or " #" in v or v[:1] in tuple("&*!|>%@`"):
            errs.append(f"第 {i} 行 {k.strip()}：值含 YAML 特殊字符（如「: 」），须用双引号包起来")
        fields[k.strip()] = v
    try:
        import yaml  # type: ignore
        try:
            yaml.safe_load(raw)
        except Exception as exc:
            msg = str(exc).splitlines()[0]
            if not any("双引号" in e for e in errs):
                errs.append(f"YAML 解析失败：{msg}")
    except ImportError:
        pass
    body = text[end + 5:]
    return fields, body, raw.count("\n") + 3, errs


# ----------------------------------------------------------------------------- check
IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
ANCHOR_RE = re.compile(r"<!--\s*p\s*(\d+)\s*-->", re.IGNORECASE)
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
REF_TITLE_RE = re.compile(r"参考文献|參考文獻|参考資料|引用文献|references|bibliography|literature cited|works cited|"
                          r"literatur|références|referencias", re.IGNORECASE)
REF_NUM_RE = re.compile(r"^\s{0,3}(?:\[(\d{1,4})\]|(\d{1,4})[.)])\s+\S")
REF_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+\S")
CAPLINE_RE = re.compile(r"^\s*\*\*\s*([^*\d]{0,24}?)\s*(S?\d{1,3})[A-Za-z]?\s*[^*]{0,6}\*\*")
TABLE_LABEL_RE = re.compile(r"表|table|tab\.?|tabelle|tableau|tabla", re.IGNORECASE)
SCHEME_LABEL_RE = re.compile(r"方案|scheme|图式|スキーム", re.IGNORECASE)
META_LINE_RE = re.compile(r"^\*\*[^*]{1,16}\*\*\s*[：:]")
LATIN_RUN_RE = re.compile(r"(?:[A-Za-z][A-Za-z'’\-]*[ ,;:]+){10,}[A-Za-z]")
PLACEHOLDER_RE = re.compile(r"<!--\s*CONT\s*-->|\bTODO\b|\bFIXME\b|待译|待补|未完待续|\[\[TBD\]\]")
SPECIAL_START = ("#", ">", "|", "```", "<", "![", "---", "$$")
LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
TERMINAL_RE = re.compile(r"[。！？…!?：:；;，,、）)」』】》”\"*`_\]$|]$")
RECOMMENDED = ("title", "title_orig", "venue", "year", "doi", "target_lang", "one_liner")
LEGACY_KEYS = {"title_zh": "title", "title_en": "title_orig", "journal": "venue"}


def translation_file(folder: str) -> list[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for f in sorted(names):
        if not f.endswith(".md") or f.startswith(("_", ".")) or f.lower() in ("readme.md", "cards.md"):
            continue
        if re.search(r"\.[a-z]{2,3}(?:-[A-Za-z]{2,4})?\.md$", f) or re.search(r"(翻译|译文|譯文)\.md$", f):
            out.append(f)
    return out


def target_lang_of(fname: str, fields: dict | None, default: str) -> str:
    if fields and fields.get("target_lang"):
        return fields["target_lang"].lower()
    m = re.search(r"\.([a-z]{2,3}(?:-[A-Za-z]{2,4})?)\.md$", fname)
    if m:
        return m.group(1).lower()
    if "中文" in fname:
        return "zh"
    return default


def is_plain(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(SPECIAL_START) and not s.startswith("**") and not LIST_RE.match(line)


def hard_wraps(lines: list[str]) -> list[int]:
    bad, in_code = [], False
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        a, b = lines[i], lines[i + 1]
        if is_plain(a) and b.strip() and not b.strip().startswith(SPECIAL_START) and not LIST_RE.match(b) \
                and not TERMINAL_RE.search(a.rstrip()):
            bad.append(i)
    return bad


def check_paper(folder: str, opts) -> tuple[str, list[tuple[str, str]], dict]:
    msgs: list[tuple[str, str]] = []
    info = {"figs": 0, "tabs": 0, "refs": 0}
    mds = translation_file(folder)
    if not mds:
        return "", [("FAIL", "没有译稿（<文件夹名>.<语言>.md，如 xxx.zh.md）")], info
    if len(mds) > 1:
        msgs.append(("WARN", f"有多份译稿，只检查第一份：{', '.join(mds)}"))
    md = mds[0]
    raw = open(os.path.join(folder, md), "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        msgs.append(("FAIL", "带 UTF-8 BOM（要求无 BOM）"))
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return md, [("FAIL", f"不是 UTF-8：{exc}")], info
    if b"\r\n" in raw:
        msgs.append(("WARN", "含 CRLF 换行（建议 LF）"))
        text = text.replace("\r\n", "\n")

    fields, body, off, errs = parse_front_matter(text)
    for e in errs:
        msgs.append(("FAIL", f"YAML 头：{e}"))
    if fields is None:
        msgs.append(("WARN", "缺少 YAML 卡片头（cards 索引会退化为从正文猜测）"))
    else:
        norm = {LEGACY_KEYS.get(k, k): v for k, v in fields.items()}
        miss = [k for k in RECOMMENDED if not norm.get(k)]
        if miss:
            msgs.append(("WARN", f"YAML 头缺字段：{', '.join(miss)}"))
    lang = target_lang_of(md, fields, opts.lang)
    lines = body.split("\n")
    L0 = off  # line offset of body inside the file (1-based adjustment)

    # headings
    first = next((ln for ln in lines if ln.strip()), "")
    if not first.startswith("# "):
        msgs.append(("FAIL", "YAML 头之后第一行应为「# 标题」（<!-- p1 --> 放在页眉信息块之后）"))
    heads, in_code = [], False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_code = not in_code
        elif not in_code:
            m = HEAD_RE.match(ln)
            if m:
                heads.append((i, len(m.group(1)), m.group(2)))
    if sum(1 for _, lv, _ in heads if lv == 1) > 1:
        msgs.append(("WARN", "一级标题不止一个"))
    if not any(lv == 2 for _, lv, _ in heads):
        msgs.append(("WARN", "没有二级标题（## 节）"))
    for (i1, l1, t1), (i2, l2, t2) in zip(heads, heads[1:]):
        if l2 > l1 + 1:
            msgs.append(("WARN", f"标题跳级 {'#' * l1}→{'#' * l2}：{t2[:30]}（行 {i2 + L0}）"))
            break
    empty = [t for (i1, l1, t), (i2, l2, _) in zip(heads, heads[1:])
             if l2 <= l1 and not any(x.strip() and not ANCHOR_RE.fullmatch(x.strip()) for x in lines[i1 + 1:i2])]
    if empty:
        msgs.append(("WARN", f"空章节：{'；'.join(e[:20] for e in empty[:3])}"))

    # hard wraps
    hw = hard_wraps(lines)
    if hw:
        msgs.append(("WARN", f"疑似段内硬折行 {len(hw)} 处（行 {', '.join(str(i + L0) for i in hw[:5])}）"))

    # page anchors
    man = load_manifest(folder)
    npages = man.get("pages")
    legacy = os.path.join(folder, "原文提取.txt")
    if not npages and os.path.isfile(legacy):
        nums = re.findall(r"<<<PAGE\s+(\d+)>>>", open(legacy, encoding="utf-8", errors="replace").read())
        npages = max(map(int, nums)) if nums else None
    anchors = [int(n) for n in ANCHOR_RE.findall(body)]
    if npages:
        if not anchors:
            msgs.append(("WARN", f"没有页码锚点 <!-- pN -->（原文 {npages} 页）"))
        else:
            if anchors != sorted(anchors) or len(set(anchors)) != len(anchors):
                msgs.append(("WARN", "页码锚点不是严格递增"))
            missing = [p for p in range(1, npages + 1) if p not in set(anchors)]
            if max(anchors) > npages:
                msgs.append(("WARN", f"页码锚点超出原文页数（最大 {max(anchors)} / 原文 {npages}）"))
            if missing:
                msgs.append(("WARN", f"这些页没有锚点（可能漏译）：{', '.join(map(str, missing[:15]))}"
                                     f"{' ...' if len(missing) > 15 else ''}"))

    # images
    refs = IMG_RE.findall(body)
    if any(r.startswith("data:") for r in refs):
        msgs.append(("FAIL", "有 base64 内嵌图片"))
    local = [r for r in refs if not r.startswith(("data:", "http://", "https://"))]
    if len(local) < len([r for r in refs if not r.startswith("data:")]):
        msgs.append(("WARN", "引用了外链图片（离线不可读）"))
    miss_img = [r for r in local if not os.path.isfile(os.path.join(folder, r))]
    if miss_img:
        msgs.append(("FAIL", f"引用的图片不存在：{', '.join(miss_img[:5])}"))
    figdir = os.path.join(folder, "figures")
    if os.path.isdir(figdir):
        used = {os.path.normpath(r) for r in local}
        unused = [f for f in sorted(os.listdir(figdir))
                  if os.path.isfile(os.path.join(figdir, f)) and not f.startswith(".")
                  and os.path.normpath(f"figures/{f}") not in used]
        if unused:
            msgs.append(("WARN", f"figures/ 中有未引用的文件：{', '.join(unused[:5])}{' ...' if len(unused) > 5 else ''}"))
    try:
        from PIL import Image
    except ImportError:
        Image = None
    big, small = [], []
    for r in local:
        p = os.path.join(folder, r)
        if not os.path.isfile(p):
            continue
        if not r.lower().endswith(".png"):
            msgs.append(("WARN", f"不是 PNG：{r}"))
        if Image:
            try:
                w, h = Image.open(p).size
            except Exception:
                msgs.append(("WARN", f"图片打不开：{r}"))
                continue
            if w * h > opts.max_px * 1.15 or max(w, h) > opts.max_side * 1.05:
                big.append(f"{os.path.basename(r)}({w}x{h})")
            elif w * h < 120_000:
                small.append(f"{os.path.basename(r)}({w}x{h})")
    if big:
        msgs.append(("WARN", f"超出读图预算、会被缩小：{', '.join(big[:4])} → pdftrans.py trim"))
    if small:
        msgs.append(("WARN", f"像素很少、小字可能看不清：{', '.join(small[:4])}"))

    # captions: every image must be followed by a **label N** caption line
    for i, ln in enumerate(lines):
        m = IMG_RE.search(ln)
        if not m:
            continue
        nxt = [x for x in lines[i + 1:i + 8] if x.strip()][:3]
        if not any(x.lstrip().startswith("**") for x in nxt):
            msgs.append(("FAIL", f"图片后缺图注（**图 N** …）：{m.group(1)}"))

    # figure / table coverage against the source captions
    tr_figs, tr_tabs = set(), set()
    for ln in lines:
        m = CAPLINE_RE.match(ln)
        if not m or META_LINE_RE.match(ln):
            continue
        label, num = m.group(1), m.group(2)
        if TABLE_LABEL_RE.search(label):
            tr_tabs.add(num)
        elif not SCHEME_LABEL_RE.search(label):
            tr_figs.add(num)
    info["figs"], info["tabs"] = len(tr_figs), len(tr_tabs)
    caps = man.get("captions", [])
    if caps:
        src_figs = {c["num"] if c["kind"] != "suppfig" or c["num"].startswith("S") else "S" + c["num"]
                    for c in caps if c["kind"] in ("fig", "suppfig")}
        src_tabs = {c["num"] for c in caps if c["kind"] == "table"}
        norm = lambda s: {re.sub(r"[A-Za-z]$", "", x) for x in s}  # noqa: E731
        lost_f = sorted(norm(src_figs) - norm(tr_figs), key=lambda x: (x.startswith("S"), int(re.sub(r"\D", "", x) or 0)))
        lost_t = sorted(norm(src_tabs) - norm(tr_tabs), key=lambda x: int(re.sub(r"\D", "", x) or 0))
        if lost_f:
            msgs.append(("WARN", f"原文有这些图的图注，译稿没找到：{', '.join(lost_f)}"))
        if lost_t:
            msgs.append(("WARN", f"原文有这些表，译稿没找到 **表 N** 表注：{', '.join(lost_t)}"))
        extra = sorted((norm(tr_figs) - norm(src_figs)) | {"表" + x for x in norm(tr_tabs) - norm(src_tabs)})
        if extra:
            msgs.append(("WARN", f"译稿里有原文图注清单中没有的编号（编号写错或自造？）：{', '.join(extra)}"))

    # placeholders
    ph = sorted(set(m.group(0) for m in PLACEHOLDER_RE.finditer(body)))
    if ph:
        msgs.append(("FAIL", f"残留占位标记：{', '.join(ph)}"))

    # references
    ref_i = next((i for i, lv, t in heads if lv <= 3 and REF_TITLE_RE.search(t)), None)
    est = man.get("references_estimate")
    if ref_i is None:
        if est:
            msgs.append(("FAIL", f"没有参考文献小节（原文约 {est} 条）"))
    else:
        lv = next(l for i, l, _ in heads if i == ref_i)
        end = next((i for i, l, _ in heads if i > ref_i and l <= lv), len(lines))
        sect = lines[ref_i + 1:end]
        nums = [int(m.group(1) or m.group(2)) for m in (REF_NUM_RE.match(x) for x in sect) if m]
        bullets = sum(1 for x in sect if REF_BULLET_RE.match(x))
        count = len(nums) or bullets
        info["refs"] = count
        if nums and nums != list(range(1, len(nums) + 1)):
            bad = [f"{i}->{n}" for i, n in enumerate(nums, 1) if i != n][:6]
            msgs.append(("FAIL", f"参考文献编号不连续：{', '.join(bad)}"))
        if not count:
            msgs.append(("FAIL", "参考文献小节里没有条目"))
        elif est and count != est:
            msgs.append(("WARN", f"参考文献 {count} 条，原文估计 {est} 条（估计可能不准，请核对）"))

    # untranslated residue (only meaningful when the target is CJK)
    if lang.split("-")[0] in ("zh", "ja", "ko"):
        stop = ref_i if ref_i is not None else len(lines)
        hits, cjk, latin, in_code = [], 0, 0, False
        for i, ln in enumerate(lines[:stop]):
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code
                continue
            if in_code or not s or s.startswith(("|", "![", "<!--", ">")) or META_LINE_RE.match(s):
                continue
            cjk += len(re.findall(f"[{CJK}]", s))
            latin += len(re.findall(r"[A-Za-z]", s))
            run = LATIN_RUN_RE.search(re.sub(r"\([^)]*\)|（[^）]*）", "", s))
            if run:
                words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", run.group(0))
                if sum(w[0].islower() for w in words) >= 0.5 * len(words):  # prose, not Title-Case names
                    hits.append(i + L0)
        if hits:
            msgs.append(("WARN", f"疑似未译的外文整句 {len(hits)} 处（行 {', '.join(map(str, hits[:6]))}）"))
        if cjk + latin > 500 and cjk < 0.4 * latin:
            msgs.append(("WARN", f"目标语言文字比例偏低（CJK {cjk} / 拉丁字母 {latin}），可能大段未译"))
    return md, msgs, info


def iter_papers(path: str):
    path = os.path.abspath(path)
    if translation_file(path) or os.path.isdir(os.path.join(path, WORK)):
        yield os.path.basename(path), path
        return
    for name in sorted(os.listdir(path)):
        p = os.path.join(path, name)
        if not os.path.isdir(p) or name.startswith((".", "_")) or name in SKIP_DIRS:
            continue
        if translation_file(p) or os.path.isdir(os.path.join(p, WORK)):
            yield name, p


def cmd_check(a) -> int:
    if not os.path.isdir(a.path):
        die(f"不是文件夹：{a.path}")
    tot = {"papers": 0, "fail": 0, "warn": 0, "bad": 0}
    lines = []
    for name, folder in iter_papers(a.path):
        tot["papers"] += 1
        md, msgs, info = check_paper(folder, a)
        f = [m for lv, m in msgs if lv == "FAIL"]
        w = [m for lv, m in msgs if lv == "WARN"]
        tot["fail"] += len(f)
        tot["warn"] += len(w)
        tot["bad"] += bool(f)
        st = "FAIL" if f else ("WARN" if w else "OK")
        if a.quiet and st == "OK":
            continue
        lines.append(f"[{st}] {name}  {md}  图注 {info['figs']} · 表注 {info['tabs']} · 参考文献 {info['refs']}")
        lines += [f"    FAIL  {m}" for m in f] + [f"    WARN  {m}" for m in w]
    if not tot["papers"]:
        die("没找到文献文件夹（含 *.<lang>.md 译稿或 _work/ 的子文件夹）", 1)
    summary = f"共 {tot['papers']} 篇 · FAIL {tot['fail']} 条（{tot['bad']} 篇）· WARN {tot['warn']} 条"
    passed = tot["fail"] == 0 and (not a.strict or tot["warn"] == 0)
    report = "\n".join(lines) + f"\n\n{summary}\n结果：{'通过' if passed else '未通过'}"
    if a.log:
        with open(a.log, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report + "\n")
    print(report)
    return 0 if passed else 1


# ----------------------------------------------------------------------------- cards
CARD_ORDER = ["folder", "title", "title_orig", "authors", "venue", "year", "doi", "type", "field",
              "source_lang", "target_lang", "pages", "figures", "tables", "references",
              "one_liner", "subject", "method", "key_result"]


def cmd_cards(a) -> int:
    root = os.path.abspath(a.root)
    cards = []
    for name, folder in iter_papers(root):
        mds = translation_file(folder)
        if not mds:
            continue
        text = open(os.path.join(folder, mds[0]), encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
        fields, body, _, errs = parse_front_matter(text)
        if errs:
            print(f"  note: {name} 的 YAML 头有问题（{errs[0]}），请先跑 check")
        card = {"folder": name}
        for k, v in (fields or {}).items():
            card[LEGACY_KEYS.get(k, k)] = v
        if not card.get("title"):
            m = re.search(r"(?m)^# (.+)$", body)
            card["title"] = re.sub(r"[*_`]", "", m.group(1)).strip() if m else name
        _, _, info = check_paper(folder, argparse.Namespace(lang="zh", max_px=MAX_PX, max_side=MAX_SIDE))
        for k, src in (("figures", "figs"), ("tables", "tabs"), ("references", "refs")):
            if not str(card.get(k, "")).strip() and info[src]:
                card[k] = info[src]
        for k in ("figures", "tables", "references", "pages", "year"):
            if isinstance(card.get(k), str) and card[k].isdigit():
                card[k] = int(card[k])
        card["file"] = f"{name}/{mds[0]}"
        cards.append(card)
    if not cards:
        die("没有找到任何译稿；路径应指向工作区根目录", 1)
    out = a.out or os.path.join(root, "cards.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for c in cards:
            row = {k: c[k] for k in CARD_ORDER if c.get(k) not in (None, "")}
            row.update({k: v for k, v in c.items() if k not in row and v not in (None, "")})
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(cards)} 张卡片 -> {out}  ({os.path.getsize(out)} 字节)")
    if a.md:
        esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        p = os.path.join(root, "cards.md")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 文献卡片索引\n\n| # | 文献 | 出处 | 年 | 图 | 参考文献 | 一句话 |\n|---:|---|---|---|---:|---:|---|\n")
            for i, c in enumerate(cards, 1):
                fh.write(f"| {i} | [{esc(c['title'])}](<{c['file']}>) | {esc(c.get('venue', ''))} | {esc(c.get('year', ''))} | "
                         f"{esc(c.get('figures', ''))} | {esc(c.get('references', ''))} | {esc(c.get('one_liner', ''))} |\n")
        print(f"表格 -> {p}")
    return 0


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    _safe_console()
    ap = argparse.ArgumentParser(prog="pdftrans", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"pdftrans {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def budget(p):
        p.add_argument("--max-px", type=int, default=MAX_PX, help=f"像素总量上限（默认 {MAX_PX}）")
        p.add_argument("--max-side", type=int, default=MAX_SIDE, help=f"长边上限（默认 {MAX_SIDE}）")

    p = sub.add_parser("extract", help="分页文本 + 清单")
    p.add_argument("pdf"); p.add_argument("outdir")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("figures", help="自动定位并裁出全部图")
    p.add_argument("pdf"); p.add_argument("outdir"); budget(p)
    p.add_argument("--pages", help="只处理这些页，如 3,5-7")
    p.add_argument("--only", help="只重做这些图，如 fig03,figS01")
    p.add_argument("--tables", action="store_true", help="表格也渲染成图（仅供核对，正文仍要转 Markdown 表）")
    p.add_argument("--force", action="store_true", help="覆盖手动出过的图")
    p.add_argument("--no-overview", action="store_true")
    p.set_defaults(fn=cmd_figures)

    p = sub.add_parser("render", help="按 PDF 点坐标手动出图")
    p.add_argument("pdf"); p.add_argument("outdir"); budget(p)
    p.add_argument("--page", type=int, required=True, help="1 起页码")
    p.add_argument("--clip", help="x0,y0,x1,y1（PDF 点）；省略 = 整页")
    p.add_argument("--name", required=True, help="输出名，如 fig03 / fig03-c / figS01")
    p.add_argument("--no-trim", action="store_true")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("pages", help="渲染页面供目视核对")
    p.add_argument("pdf"); p.add_argument("outdir"); budget(p)
    p.add_argument("--pages", required=True, help="如 1,3-5")
    p.add_argument("--grid", action="store_true", help="叠加 PDF 点坐标网格（用于手动定 --clip）")
    p.add_argument("--grid-step", type=int, default=50)
    p.set_defaults(fn=cmd_pages)

    p = sub.add_parser("trim", help="紧裁白边并限制像素（默认原地覆盖）")
    p.add_argument("images", nargs="+"); budget(p)
    p.add_argument("--out"); p.add_argument("--pad", type=int, default=8)
    p.set_defaults(fn=cmd_trim)

    p = sub.add_parser("check", help="校验译稿")
    p.add_argument("path"); budget(p)
    p.add_argument("--lang", default="zh", help="译稿未声明 target_lang 时的默认目标语言")
    p.add_argument("--strict", action="store_true", help="WARN 也算不通过")
    p.add_argument("-q", "--quiet", action="store_true", help="只显示有问题的文献")
    p.add_argument("--log", help="报告另存到此文件")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("cards", help="汇总 cards.jsonl")
    p.add_argument("root"); p.add_argument("--md", action="store_true"); p.add_argument("--out")
    p.set_defaults(fn=cmd_cards)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
