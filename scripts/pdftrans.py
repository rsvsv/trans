#!/usr/bin/env python3
"""pdftrans - helper CLI for the pdf-paper-translation skill (single file, cross-platform).

Subcommands
  extract  PDF OUTDIR                 page text + refs.md + manifest  -> OUTDIR/_work/
  figures  PDF OUTDIR                 auto-locate and crop every figure -> OUTDIR/figures/
  render   PDF OUTDIR --page N --clip x0,y0,x1,y1 --name fig03   manual crop (PDF points)
  pages    PDF OUTDIR --pages 3,5-7 [--grid]                     page renders for review
  trim     IMG [IMG ...] [--out PATH|DIR]  trim margins + cap budget (in place, original kept in .orig/)
  anchor   PATH [--dry-run] [--auto]   verify page anchors against source.txt; fix only on strong evidence
  check    PATH                        validate translation(s); PATH = paper folder or library root
  brief    PATH [--cap-chars N] [--toc-only]  reading view: outline (file lines, pages) + abstract + captions + conclusion
  cards    ROOT [--md] [--list [--with F,..]]  build ROOT/cards.jsonl (+ cards.md); --list prints a compact table
  find     ROOT PATTERN [--and P] [--not P] [--scope para|section|paper] [--in zh,src,cards]
                                       library search, grouped by paper, translation and source together
  pair     PATH --page N | --line L | --sample K   source page next to its translation (spot checks)
  wrap     PATH [--dry-run]            wrap lines over 1,500 characters at sentence ends (text unchanged)
  refresh  ROOT [--dry-run] [--pdf-dir DIR]  re-extract _work/ made by an older version (translations untouched)

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
import shutil
import sys
from collections import Counter, defaultdict

VERSION = "3.2.1"
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


def backup_once(folder: str, path: str, bak_name: str) -> None:
    """Copy path to _work/backup/bak_name unless that backup already exists (the first pre-edit copy is kept)."""
    bdir = os.path.join(work_dir(folder), "backup")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, bak_name)
    if os.path.isfile(path) and not os.path.exists(bak):
        shutil.copyfile(path, bak)


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
        elif re.search(r"[A-Za-zα-ω]-$", out) and re.match(r"[a-z]", ln):
            out += ln  # word broken at line end: rejoin, keep the hyphen (glycer-ophosphate / cell-based)
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
# Symbol-encoded fonts without a ToUnicode map put Greek glyphs on ASCII codes ("b-TCP" = β-TCP,
# "150 mm" = 150 μm, "2q" = 2θ). Only ASCII letters inside such fonts are remapped (Adobe Symbol encoding).
SYMFONT_RE = re.compile(r"symbol|^AdvPS", re.I)  # verified mappings only; TeX CMMI etc. hold Latin italics
GREEK = str.maketrans("abcdefghijklmnopqrstuvwxyzABCDEGHIKLMNOPQRSTUWXYZ",
                      "αβχδεφγηιϕκλμνοπθρστυϖωξψζΑΒΧΔΕΓΗΙΚΛΜΝΟΠΘΡΣΤΥΩΞΨΖ")


def _script(text: str, table: dict) -> str | None:
    t = text.strip()
    if t and all(ord(ch) in table for ch in t):
        return text.translate(table)
    return None


def page_blocks(fitz, page, fixes: list | None = None) -> list[TB]:
    """Text blocks with superscripts / subscripts restored as Unicode (10⁸, mL⁻¹, H₂O₂) and Greek
    letters from symbol-encoded fonts restored (β-TCP, μm). Each Greek fix is appended to `fixes`.

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
            for k, sp in enumerate(spans):
                if SYMFONT_RE.search(sp.get("font", "")) and re.search(r"[A-Za-z]", sp["text"]):
                    new = sp["text"].translate(GREEK)
                    if new != sp["text"] and fixes is not None:
                        before = "".join(s["text"] for s in spans[max(0, k - 1):k])[-15:]
                        after = "".join(s["text"] for s in spans[k + 1:k + 2])[:15]
                        fixes.append({"page": page.number + 1, "from": sp["text"].strip(), "to": new.strip(),
                                      "context": f"{before}{new}{after}".strip()})
                    sp["text"] = new
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


REF_HEAD_STRIP_RE = re.compile(r"^\s*(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,4}\.|\d{1,4})\s*")
# like REF_MARK_RE, but a visual line may hold the number alone ("34." on its own line)
REF_LINE_RE = re.compile(r"^[ \t]*(?:\[(\d{1,4})\]|\((\d{1,4})\)|(\d{1,4})\.(?=\s|$)|(\d{1,4})(?=[ \t]+[A-Z]|$))")
REF_TAIL_STOP_RE = re.compile(r"^\s*(?:©|#|acknowledg|publisher|disclaimer|supplementary|appendix|data availability|"
                              r"author contrib|conflicts? of interest|declaration|funding|this journal|downloaded|"
                              r"received|\d{1,5}\s*$)", re.I)
LONG_LINE = 1500  # file readers often cut lines at ~2000 characters


def soft_wrap(p: str, limit: int = LONG_LINE) -> str:
    """Break an over-long paragraph into lines <= limit at sentence ends (paragraphs stay blank-line separated)."""
    if len(p) <= limit:
        return p
    sents = re.split(r"(?<=[.!?。！？;；])\s+(?=\S)", p)
    out, cur = [], ""
    for s in sents:
        while len(s) > limit:  # one giant "sentence" (e.g. a run of references): cut at a space
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            s1, s = s[:cut].rstrip(), s[cut:].lstrip()
            if cur:
                out.append(cur)
                cur = ""
            out.append(s1)
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        out.append(cur)
    return "\n".join(out)


def split_refs(lines: list) -> tuple[list[tuple[int, str]], list[int], list[int]]:
    """Cut the numbered reference list into entries from visual lines [(page, text|None)].
    None marks a block boundary. Returns (entries [(n, text)], missing numbers, pages used)."""
    marks: dict[int, list[tuple[int, int]]] = {0: [], 1: [], 2: [], 3: []}
    for i, (_, t) in enumerate(lines):
        if t is None:
            continue
        m = REF_LINE_RE.match(t)
        if m:
            for style in range(4):
                if m.group(style + 1):
                    marks[style].append((i, int(m.group(style + 1))))
    best: list[tuple[int, int]] = []
    for seq in marks.values():
        for s in [k for k, (_, n) in enumerate(seq) if n == 1][-6:]:
            run, last = [seq[s]], 1
            for i, n in seq[s + 1:]:
                if n == last + 1 or n == last + 2:
                    run.append((i, n))
                    last = n
            # ties go to the later start: section headings ("1. Introduction") and affiliations
            # number 1, 2, 3 ... too and would otherwise swallow the first references
            if len(run) >= len(best):
                best = run
    if len(best) < 8:
        return [], [], []
    entries = []
    for k, (i, n) in enumerate(best):
        end = best[k + 1][0] if k + 1 < len(best) else None
        parts, j = [REF_HEAD_STRIP_RE.sub("", lines[i][1], count=1)], i + 1
        while j < len(lines) and (end is None or j < end):
            t = lines[j][1]
            if t is not None:
                if end is None:  # last entry: stop once it looks complete, or at back matter
                    done = " ".join(parts)
                    mk = REF_LINE_RE.match(t)
                    new_entry = mk and int(next(g for g in mk.groups() if g)) < 1900  # "(2020) 115426" is a year
                    if len(done) > 700 or REF_TAIL_STOP_RE.match(t) or new_entry or \
                            (re.search(r"\b(19|20)\d{2}\b", done) and re.search(r"[.\d)]\s*$", done)):
                        break
                parts.append(t)
            j += 1
        entries.append((n, re.sub(r"\s+", " ", join_lines("\n".join(parts))).strip()))
    nums = [n for n, _ in entries]
    missing = [n for n in range(1, nums[-1] + 1) if n not in set(nums)]
    pages = sorted({lines[i][0] for i, _ in best})
    return entries, missing, pages


def write_refs(path: str, entries, missing, pages, estimate) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        span = f"第 {pages[0]}–{pages[-1]} 页" if pages[0] != pages[-1] else f"第 {pages[0]} 页"
        fh.write(f"<!-- pdftrans：从原文{span}切出 {len(entries)} 条（编号 1–{entries[-1][0]}）；"
                 f"原文估计 {estimate or '?'} 条。末条结尾与缺号请对照 source.txt 核对 -->\n\n")
        have = dict(entries)
        for n in range(1, entries[-1][0] + 1):
            if n in have:
                fh.write(f"{n}. {have[n]}\n")
            else:
                fh.write(f"{n}. <!-- 缺第 {n} 条：提取时没切出来，请从 source.txt 补上 -->\n")


def cmd_extract(a) -> int:
    fitz = need_fitz()
    pdf, out = os.path.abspath(a.pdf), os.path.abspath(a.outdir)
    if not os.path.isfile(pdf):
        die(f"找不到 PDF：{pdf}")
    wd = work_dir(out)
    pages_txt, raw_all, counts, captions, scanned = [], [], [], [], []
    ref_lines: list = []
    sym_fixes: list = []
    seen: set = set()
    with fitz.open(pdf) as doc:
        if doc.needs_pass:
            die("PDF 已加密，需要密码", 3)
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        for pno, page in enumerate(doc, 1):
            ordered = reading_order(page_blocks(fitz, page, sym_fixes), page.rect)
            captions += find_captions(pno, ordered, seen)
            paras = [soft_wrap(join_lines(b.text)) for b in ordered]
            pages_txt.append("\n\n".join(p for p in paras if p))
            for b in ordered:  # running headers/footers out, but keep a reference entry that sits at the page top
                if not in_margin(b.rect, page.rect) or (REF_LINE_RE.match(b.text) and len(b.text.strip()) > 12):
                    ref_lines += [(pno, t.strip()) for t in b.text.splitlines() if t.strip()] + [(pno, None)]
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
    entries, missing, rpages = split_refs(ref_lines)
    refs_path = os.path.join(wd, "refs.md")
    if entries:
        write_refs(refs_path, entries, missing, rpages, refs)
    elif os.path.isfile(refs_path):
        os.remove(refs_path)
    figs = sorted({(c["kind"], c["num"]) for c in captions if c["kind"] != "table" and not c["continued"]})
    tabs = sorted({c["num"] for c in captions if c["kind"] == "table" and not c["continued"]})
    old = load_manifest(out)
    manifest = {"tool": f"pdftrans {VERSION}", "pdf": pdf, "pages": npages, "metadata": meta,
                "chars_per_page": counts, "scanned_pages": scanned, "references_estimate": refs,
                "references_split": {"count": len(entries), "last": entries[-1][0] if entries else 0,
                                     "missing": missing, "pages": rpages} if entries else None,
                "symbol_fixes": {"count": len(sym_fixes), "samples": sym_fixes[:40]},
                "captions": captions, "figures": old.get("figures", [])}
    save_manifest(out, manifest)
    print(f"pages={npages}  chars={sum(counts)}  figures={len(figs)}  tables={len(tabs)}  "
          f"references~{refs if refs else '?'}")
    print(f"-> {os.path.join(wd, 'source.txt')}")
    if entries:
        gap = f"，缺号 {missing[:10]}" if missing else ""
        warn = "  <- 与估计不符，请核对" if refs and entries[-1][0] != refs else ""
        print(f"-> {refs_path}  参考文献 {len(entries)} 条（1–{entries[-1][0]}{gap}）{warn}")
    else:
        print("参考文献：未切出编号列表（作者-年份制或版式特殊），请从 source.txt 整理")
    if sym_fixes:
        kinds = sorted({f"{f['from']}→{f['to']}" for f in sym_fixes})
        print(f"符号字体还原 {len(sym_fixes)} 处：{'、'.join(kinds[:12])}{' …' if len(kinds) > 12 else ''}"
              f"（样例见 manifest.json 的 symbol_fixes，可疑处用 pages 看原页）")
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
    """Trim + cap budget. In place by default, but the first time a file is touched its original is
    copied to <dir>/.orig/<name> (hidden from `check`). --out DIR writes elsewhere and never touches inputs."""
    PIL = need_pil()
    import shutil
    out_dir = None
    if a.out and (len(a.images) > 1 or os.path.isdir(a.out) or a.out.endswith(("/", "\\"))):
        out_dir = a.out
        os.makedirs(out_dir, exist_ok=True)
    backups = 0
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
        if out_dir:
            dst = os.path.join(out_dir, os.path.basename(src))
        else:
            dst = a.out or src
        if not dst.lower().endswith(".png"):
            dst = os.path.splitext(dst)[0] + ".png"
        if os.path.abspath(dst) == os.path.abspath(src) and not a.no_backup:
            bdir = os.path.join(os.path.dirname(os.path.abspath(src)), ".orig")
            bak = os.path.join(bdir, os.path.basename(src))
            if not os.path.exists(bak):  # keep the very first original, never overwrite it
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(src, bak)
                backups += 1
        im.save(dst, "PNG", optimize=True)
        print(f"{os.path.basename(src)}: {w0}x{h0} -> {im.width}x{im.height}  -> {dst}")
    if backups:
        print(f"原图已备份 {backups} 张到各自目录下的 .orig/（不需要时可删除；加 --no-backup 跳过备份）")
    return 0


# ----------------------------------------------------------------------------- front matter
def parse_front_matter(text: str):
    """Return (fields|None, body, body_offset_lines, errors). Values may be quoted or plain."""
    if not text.startswith("---\n"):
        return None, text, 1, []
    end = text.find("\n---\n", 3)
    if end < 0:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return None, text, 1, ["YAML 头没有结束的 ---"]
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
    # 1-based file line of body[0]: opening ---, the raw lines, closing ---, then the body
    return fields, body, raw.count("\n") + 4, errs


# ----------------------------------------------------------------------------- check
IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
ANCHOR_RE = re.compile(r"<!--\s*p\s*(\d+)\s*-->", re.IGNORECASE)
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
REF_TITLE_RE = re.compile(r"参考文献|參考文獻|参考資料|引用文献|references|bibliography|literature cited|works cited|"
                          r"literatur|références|referencias", re.IGNORECASE)
REF_NUM_RE = re.compile(r"^\s{0,3}(?:\[(\d{1,4})\]|\((\d{1,4})\)|(\d{1,4})[.)])\s+\S")
REF_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+\S")
CAPLINE_RE = re.compile(r"^\s*\*\*\s*([^*\d]{0,24}?)\s*(S?\d{1,3})[A-Za-z]?\s*[^*]{0,6}\*\*")
TABLE_LABEL_RE = re.compile(r"表|table|tab\.?|tabelle|tableau|tabla", re.IGNORECASE)
SCHEME_LABEL_RE = re.compile(r"方案|scheme|图式|スキーム", re.IGNORECASE)
META_LINE_RE = re.compile(r"^\*\*[^*]{1,16}\*\*\s*[：:]")
LATIN_RUN_RE = re.compile(r"(?:[A-Za-z][A-Za-z'’\-]*[ ,;:]+){10,}[A-Za-z]")
PLACEHOLDER_RE = re.compile(r"<!--\s*CONT\s*-->|\bTODO\b|\bFIXME\b|待译|待补|未完待续|\[\[TBD\]\]")
SPECIAL_START = ("#", ">", "|", "```", "<", "![", "---", "$$")
LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
# a period after these is not the end of a sentence (Wang et al. (2020), Fig. 3, e.g. the)
ABBREV_END_RE = re.compile(r"(?:\bet al|\b(?:figs?|eqs?|refs?|nos?|vs|approx|ca|cf|resp|e\.g|i\.e|dr|prof|inc|ltd|co|corp"
                           r"|suppl|vol|pp?))\.$", re.I)
TERMINAL_RE = re.compile(r"[。！？…!?：:；;）)」』】》”\"*`_\]$|.]$")  # no commas: a line ending in ，/,/、 is mid-sentence
# a caption line: bold label + number in any language, or a known unnumbered kind (graphical abstract, TOC)
CAPSTART_RE = re.compile(r"^\s*\*\*\s*(?:图文摘要|graphical abstract|toc\b|table of contents)", re.IGNORECASE)
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
                and not (TERMINAL_RE.search(a.rstrip()) and not ABBREV_END_RE.search(a.rstrip())):
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
        msgs.append(("WARN", "缺少 YAML 卡片头（cards 只能从页眉信息块取标题、出处、年份、DOI，没有一句话结论）"))
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
    if not npages:  # no manifest: fall back to the page markers of _work/source.txt or 原文提取.txt
        sp = source_pages(folder)
        npages = max(p for p, _ in sp) if sp else None
    blocks = md_blocks(lines)
    ranch = real_anchors(lines, blocks)  # anchors inside code blocks are examples, not anchors
    anchors = [p for _, p, _ in ranch]
    if npages:
        if not anchors:
            msgs.append(("WARN", f"没有页码锚点 <!-- pN -->（原文 {npages} 页）→ 运行 anchor 自动插入"))
        else:
            if anchors != sorted(anchors) or len(set(anchors)) != len(anchors):
                dup = sorted({p for p in anchors if anchors.count(p) > 1})
                msgs.append(("WARN", "页码锚点不是严格递增" + (f"（重复：{', '.join(map(str, dup))}）" if dup else "")
                             + " → 运行 anchor 重排"))
            missing = [p for p in range(1, npages + 1) if p not in set(anchors)]
            if max(anchors) > npages:
                msgs.append(("WARN", f"页码锚点超出原文页数（最大 {max(anchors)} / 原文 {npages}）"))
            if missing:
                msgs.append(("WARN", f"这些页没有锚点（可能漏译）：{', '.join(map(str, missing[:15]))}"
                                     f"{' ...' if len(missing) > 15 else ''}"))
    inline = [x + L0 for x, _, st in ranch if not st]
    if inline:
        msgs.append(("WARN", f"页码锚点要单独成行，这些行里嵌着锚点：{', '.join(map(str, inline[:6]))} → 运行 anchor"))
    stray = [s + L0 for s, e, k in blocks if k == "html" or (k not in ("code", "anchor") and
                                                            re.search(r"<!--(?!\s*p\s*\d+\s*-->)", " ".join(lines[s:e])))]
    if stray:
        msgs.append(("WARN", f"除页码锚点外不应有 HTML 注释或标签（如 refs.md 开头的说明行、缺号注释）：行 {', '.join(map(str, stray[:6]))}"))

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
        if not any((CAPLINE_RE.match(x) or CAPSTART_RE.match(x)) and not META_LINE_RE.match(x) for x in nxt):
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
        nums = [int(m.group(1) or m.group(2) or m.group(3)) for m in (REF_NUM_RE.match(x) for x in sect) if m]
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
    if not getattr(opts, "structure_only", False):
        doc = {"fields": fields or {}, "has_yaml": fields is not None, "lines": lines, "body": body, "off": off,
               "text": text, "name": md, "path": os.path.join(folder, md)}
        msgs += v3_checks(folder, doc, lines, L0, ref_i, anchors)
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


def _json_item(m: str) -> dict:
    head, *rest = m.split(DETAIL_INDENT)
    return {"message": head, "details": rest}


def cmd_check(a) -> int:
    if not os.path.isdir(a.path):
        die(f"不是文件夹：{a.path}")
    as_json = getattr(a, "json", False)
    tot = {"papers": 0, "fail": 0, "warn": 0, "bad": 0}
    lines, jpapers = [], []
    for name, folder in iter_papers(a.path):
        tot["papers"] += 1
        md, msgs, info = check_paper(folder, a)
        f = [m for lv, m in msgs if lv == "FAIL"]
        w = [m for lv, m in msgs if lv == "WARN"]
        tot["fail"] += len(f)
        tot["warn"] += len(w)
        tot["bad"] += bool(f)
        st = "FAIL" if f else ("WARN" if w else "OK")
        if as_json:
            jpapers.append({"paper": name, "file": md, "status": st, **info,
                            "fail": [_json_item(m) for m in f], "warn": [_json_item(m) for m in w]})
            continue
        if a.quiet and st == "OK":
            continue
        lines.append(f"[{st}] {name}  {md}  图注 {info['figs']} · 表注 {info['tabs']} · 参考文献 {info['refs']}")
        lines += [f"    FAIL  {m}" for m in f] + [f"    WARN  {m}" for m in w]
    if not tot["papers"]:
        die("没找到文献文件夹（含 *.<lang>.md 译稿或 _work/ 的子文件夹）", 1)
    passed = tot["fail"] == 0 and (not a.strict or tot["warn"] == 0)
    if as_json:
        report = json.dumps({"papers": jpapers, "total": tot, "passed": passed}, ensure_ascii=False, indent=1)
    else:
        summary = f"共 {tot['papers']} 篇 · FAIL {tot['fail']} 条（{tot['bad']} 篇）· WARN {tot['warn']} 条"
        report = "\n".join(lines) + f"\n\n{summary}\n结果：{'通过' if passed else '未通过'}"
    if a.log:
        with open(a.log, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report + "\n")
    print(report)
    return 0 if passed else 1


# ----------------------------------------------------------------------------- brief
ABS_HEAD_RE = re.compile(r"^(?:\d+[.\s]*)?(?:报告)?(摘要|摘\s*要|abstract|summary|要旨|zusammenfassung|résumé|resumen)\s*$", re.I)
CONCL_HEAD_RE = re.compile(r"结论|結論|总结|結語|结语|conclusion|concluding|まとめ|fazit|schlussfolgerung", re.I)
NOTICE_LINE_RE = re.compile(r"^\*\*\s*[^*]{0,8}(开放获取|版权|许可|授权|引用本文|出版说明|open access|copyright|licen[cs]e|"
                            r"citation|publisher'?s note)[^*]{0,8}\*\*", re.I)


def _strip_num(t: str) -> str:
    return re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s*", "", t).strip()


def brief_paper(folder: str, a) -> str:
    """Compact reading view: card facts, outline with file line ranges and pages, abstract, every
    caption, conclusion. Line numbers are 1-based file lines, usable directly as a Read offset."""
    mds = translation_file(folder)
    if not mds:
        return f"（{os.path.basename(folder)}：没有译稿）"
    raw = open(os.path.join(folder, mds[0]), "rb").read()
    text = raw.decode("utf-8", errors="replace").lstrip("﻿").replace("\r\n", "\n")
    fields, body, off, _ = parse_front_matter(text)
    lines = body.split("\n")
    L = lambda i: i + off  # noqa: E731  body index -> file line
    page, pages = None, []
    for ln in lines:
        m = ANCHOR_RE.search(ln)
        if m:
            page = int(m.group(1))
        pages.append(page)
    P = lambda i: f"p{pages[i]}" if pages[i] else "  "  # noqa: E731

    heads, in_code = [], False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            in_code = not in_code
        elif not in_code:
            m = HEAD_RE.match(ln)
            if m:
                heads.append((i, len(m.group(1)), m.group(2).strip()))
    ref_i = next((i for i, lv, t in heads if lv <= 3 and REF_TITLE_RE.search(t)), None)

    def span(k):  # body index range of heading k's own section (until the next heading of same or higher level)
        i, lv, _ = heads[k]
        end = next((j for j, l2, _ in heads[k + 1:] if l2 <= lv), len(lines))
        return i, end

    def content(i, end):
        out = [x.strip() for x in lines[i + 1:end] if x.strip() and not ANCHOR_RE.fullmatch(x.strip())
               and x.strip() not in ("---", "***", "___")
               and not NOTICE_LINE_RE.match(x.strip())]  # keep structured-abstract labels, drop licence notes
        return "\n".join(out)

    # facts
    meta = {LEGACY_KEYS.get(k, k): v for k, v in (fields or {}).items()}
    for k, v in header_fields(body).items():
        meta.setdefault(k, v)
    title = meta.get("title") or next((t for _, lv, t in heads if lv == 1), os.path.basename(folder))
    out = [f"# {re.sub(r'[*_`]', '', title)}"]
    src = " · ".join(str(meta[k]) for k in ("venue", "year") if meta.get(k))
    for lab, val in (("原文", meta.get("title_orig")), ("出处", src), ("DOI", meta.get("doi"))):
        if val:
            out.append(f"{lab}：{val}")
    for k in ("keywords", "one_liner", "subject", "method", "key_result"):
        if meta.get(k):
            out.append(f"{k}：{meta[k]}")
    ff = find_fields_file(folder)
    for k, _ in (parse_fields_file(ff) if ff else []):
        if meta.get(k):
            out.append(f"{k}：{meta[k]}")

    caps = []
    stop = ref_i if ref_i is not None else len(lines)
    for i, ln in enumerate(lines[:stop]):
        if (CAPLINE_RE.match(ln) or CAPSTART_RE.match(ln)) and not META_LINE_RE.match(ln):
            caps.append(i)
    nref = 0
    if ref_i is not None:
        k = next(n for n, h in enumerate(heads) if h[0] == ref_i)
        i, end = span(k)
        nref = sum(1 for x in lines[i + 1:end] if REF_NUM_RE.match(x) or REF_BULLET_RE.match(x))
    npages = max((p for p in pages if p), default=0)
    out.append(f"文件：{os.path.basename(folder)}/{mds[0]} · {L(len(lines) - 1)} 行 · {len(text)} 字符"
               f"{f' · {npages} 页' if npages else ''} · 图表注 {len(caps)} · 参考文献 {nref}")

    # outline
    out.append("\n## 目录（文件行号 · 页 · 本节字数）")
    for k, (i, lv, t) in enumerate(heads):
        if lv == 1:
            continue
        s, e = span(k)
        n = sum(len(x) for x in lines[s + 1:e])
        size = f"{n / 1000:.1f}k" if n >= 1000 else str(n)
        out.append(f"L{L(s)}-{L(e - 1)}  {P(i):>4}  {'  ' * (lv - 2)}{t}  [{size}]")
    if getattr(a, "toc_only", False):
        return "\n".join(out)

    # abstract
    ab = next((k for k, (i, lv, t) in enumerate(heads) if ABS_HEAD_RE.match(_strip_num(t))), None)
    out.append("\n## 摘要")
    if ab is None:
        out.append("（未找到摘要节）")
    else:
        i, end = span(ab)
        out.append(content(i, end))

    # captions
    out.append("\n## 图表注（行号 · 页）")
    lim = getattr(a, "cap_chars", 0) or 0
    for i in caps:
        c = lines[i].strip()
        if lim and len(c) > lim:
            c = c[:lim] + "…"
        out.append(f"L{L(i)} {P(i)}  {c}")
    if not caps:
        out.append("（没有图表注）")

    # conclusion
    if not getattr(a, "no_conclusion", False):
        ks = [k for k, (i, lv, t) in enumerate(heads) if lv in (2, 3) and CONCL_HEAD_RE.search(t)
              and (ref_i is None or i < ref_i)]
        if ks:
            i, end = span(ks[-1])
            out.append(f"\n## {heads[ks[-1]][2]}（L{L(i)}）")
            out.append(content(i, end))
    return "\n".join(out)


def cmd_brief(a) -> int:
    path = a.path
    if os.path.isfile(path):
        path = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(path):
        die(f"不是文件夹：{a.path}")
    papers = list(iter_papers(path))
    if not papers:
        die("没找到译稿", 1)
    rep = ("\n\n" + "=" * 60 + "\n\n").join(brief_paper(f, a) for _, f in papers)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(rep + "\n")
        print(f"{len(papers)} 篇 -> {a.out}  ({len(rep)} 字符)")
    else:
        print(rep)
    return 0


# ----------------------------------------------------------------------------- cards
CARD_ORDER =["folder", "title", "title_orig", "authors", "venue", "year", "doi", "type", "field",
              "source_lang", "target_lang", "pages", "figures", "tables", "references", "keywords",
              "one_liner", "subject", "method", "key_result"]
HDR_LINE_RE = re.compile(r"^\*\*([^*]{1,16})\*\*\s*[：:]\s*(.+)$")
HDR_KEYS = {"title_orig": ("原文标题", "original title", "title"),
            "venue": ("出处", "期刊出处", "期刊", "来源", "source", "journal", "published in")}
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s）)\]>，,；;]+)")


def header_fields(body: str) -> dict:
    """Metadata from the header block (**原文标题**：… / **出处**：… / **DOI**：…) for files whose YAML
    header is missing or incomplete. Only facts written in the file; one_liner is never synthesised."""
    f: dict[str, str] = {}
    for ln in body.split("\n")[:60]:
        if ln.startswith("## "):
            break
        m = HDR_LINE_RE.match(ln.strip())
        if not m:
            continue
        label, val = m.group(1).strip().lower(), m.group(2).strip()
        for key, labels in HDR_KEYS.items():
            if label in labels and key not in f:
                if key == "venue":
                    it = re.match(r"\*([^*]+)\*", val)
                    f["venue"] = (it.group(1) if it else re.split(r"[,，(（]", val)[0]).strip()
                    y = re.search(r"\b(19|20)\d{2}\b", val)
                    if y:
                        f.setdefault("year", y.group(0))
                else:
                    f[key] = re.sub(r"[*_`]", "", val).strip()
        if label == "doi":
            d = DOI_RE.search(val)
            if d:
                f.setdefault("doi", d.group(1).rstrip("."))
    return f


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
        for k, v in header_fields(body).items():  # fill gaps only; YAML always wins
            if not str(card.get(k, "")).strip():
                card[k] = v
        if not fields:
            card["source"] = "header-block"  # no YAML: one_liner / subject / method / key_result absent
        _, _, info = check_paper(folder, argparse.Namespace(lang="zh", max_px=MAX_PX, max_side=MAX_SIDE,
                                                            structure_only=True))
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
    if a.list:  # compact table on stdout, nothing written: the cheap first layer for reading a library
        extra = [f.strip() for f in (a.with_ or "").split(",") if f.strip()]
        clip = lambda s: re.sub(r"\s+", " ", str(s)).strip()  # noqa: E731
        for c in cards:
            row = [c["folder"], str(c.get("year", "—")), clip(c.get("type", "—")), clip(c.get("one_liner", "—"))]
            row += [f"{f}: {clip(c.get(f, '—'))}" for f in extra]
            print(" | ".join(row))
        print(f"\n{len(cards)} 篇" + (f"；字段 {', '.join(extra)} 缺失的写作「—」" if extra else ""))
        return 0
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


# ----------------------------------------------------------------------------- v3: source text + page evidence
SRC_PAGE_RE = re.compile(r"<<<PAGE\s+(\d+)>>>")
NUM_TOKEN_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?!\.?\d)")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
FIGREF_RE = re.compile(r"(图|圖|図|表|Fig(?:ure)?s?\.?|Tables?|Tab\.?|Abb\.?|Abbildung)\s*(S?\d{1,3})", re.I)
SIG_STOP = {"the", "and", "for", "with", "were", "was", "from", "that", "this", "are", "which", "fig", "figure",
            "figures", "table", "tables", "than", "into", "have", "has", "been", "after", "each", "all", "not",
            "also", "these", "those", "their", "its", "our", "both", "using", "used", "use", "between", "within"}
SUBSCRIPT_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
ENTRY_SPLIT_RE = re.compile(f"(?=[{CIRCLED}])")


def norm_digits(t: str) -> str:
    """Subscript digits to ASCII (OD₆₀₀ = OD600) and thousands separators out (1,000 = 1000).
    Superscripts are kept on purpose: 10⁶ must not match a translation that lost it as 106."""
    t = t.translate(SUBSCRIPT_DIGITS)
    return re.sub(r"(?<=\d)[,  ](?=\d{3}(?!\d))", "", t)


def sig_tokens(text: str) -> set:
    """Tokens that survive translation: numbers, Latin words (names, genes, abbreviations), figure/table numbers."""
    t = norm_digits(text)
    out = {n for n in NUM_TOKEN_RE.findall(t) if len(n) >= 2}
    out |= {w.lower() for w in LATIN_TOKEN_RE.findall(t) if w.lower() not in SIG_STOP}
    for m in FIGREF_RE.finditer(t):
        out.add(("T" if m.group(1)[0] in "表Tt" else "F") + m.group(2).upper())
    return out


def source_path(folder: str) -> str | None:
    for p in (os.path.join(folder, WORK, "source.txt"), os.path.join(folder, "原文提取.txt")):
        if os.path.isfile(p):
            return p
    return None


def source_pages(folder: str) -> list:
    """[(page_number, text)] from _work/source.txt (or the legacy 原文提取.txt); [] when absent."""
    p = source_path(folder)
    if not p:
        return []
    txt = open(p, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    parts = SRC_PAGE_RE.split(txt)
    return [(int(parts[k]), parts[k + 1]) for k in range(1, len(parts) - 1, 2)]


RUN_MAX = 200  # running headers / footers are short


def _run_key(par: str) -> str:
    return re.sub(r"[\s|]+", " ", re.sub(r"\d+", "#", par.lower())).strip()


def running_keys(paras) -> set:
    """Running headers and footers of a source, from (page, paragraph) pairs: short paragraphs (journal line,
    URL, 'Page 3 of 9', 'Wang et al.') that recur on at least 3 pages and on 30% of all pages once digits are
    masked (the page number inside them changes); 70% for a line without a changing number, year, URL or
    'et al.', so a figure label or table header repeated on a few pages is kept. Measured on a 38-paper library: every paper's running lines
    found, no body paragraph caught; first-page mastheads (not repeated) and reference entries stay.
    source.txt keeps these lines (they carry volume, article number and DOI); find and pair skip them."""
    seen, raw, pages = defaultdict(set), defaultdict(set), set()
    for p, t in paras:
        pages.add(p)
        t = t.strip()
        if t and len(t) <= RUN_MAX and re.search(r"[A-Za-z]{3}", t):
            k = _run_key(t)
            if len(k) >= 8 or "#" in k:
                seen[k].add(p)
                raw[k].add(tuple(re.findall(r"\d+", t)))
    n = len(pages)

    def typical(k):  # what running lines are made of: a page number (digits that change from page to page),
        # a year/volume line, a URL, 'Wang et al.'. A label repeated with the same digits ('Day 7 group A') is not.
        return (len(raw[k]) > 1 or any(re.fullmatch(r"(?:19|20)\d\d", x) for d in raw[k] for x in d)
                or "www" in k or "http" in k or "et al" in k)
    # anything else must be on most pages: hiding a real paragraph from pair/find is worse than a little noise.
    # A line with no digits at all is a plain footer ('Paper RSC Advances' on every other page): 50% is enough.
    return {k for k, ps in seen.items()
            if len(ps) >= max(3, (0.3 if typical(k) else (0.5 if "#" not in k else 0.7)) * n)}


def is_running(par: str, keys: set) -> bool:
    t = par.strip()
    return bool(keys) and len(t) <= RUN_MAX and _run_key(t) in keys


def page_paras(pages: list):
    """(page, paragraph) pairs of source pages (paragraphs are separated by a blank line)."""
    return [(p, x) for p, t in pages for x in re.split(r"\n\s*\n", t) if x.strip()]


def strip_running(pages: list) -> list:
    """Source pages without their running headers/footers."""
    keys = running_keys(page_paras(pages))
    out = []
    for p, t in pages:
        keep = [x.strip("\n") for x in re.split(r"\n\s*\n", t) if x.strip() and not is_running(x, keys)]
        out.append((p, "\n\n".join(keep)))
    return out


FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
ITEM_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,4}[.)])\s+\S")
RULE_RE = re.compile(r"(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,}")


def load_translation(folder: str) -> dict | None:
    """Read the translation strictly. A file that is not UTF-8 is reported ('fatal') and never rewritten."""
    mds = translation_file(folder)
    if not mds:
        return None
    path = os.path.join(folder, mds[0])
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"path": path, "name": mds[0], "text": "", "fields": {}, "has_yaml": False, "body": "", "off": 1,
                "lines": [], "errs": [], "normalized": [],
                "fatal": f"不是 UTF-8 编码（第 {exc.start} 字节起无法解码）：先转成 UTF-8；脚本不会改写这个文件"}
    crlf = "\r" in text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fields, body, off, errs = parse_front_matter(text)
    return {"path": path, "name": mds[0], "text": text, "fields": fields or {}, "has_yaml": fields is not None,
            "body": body, "off": off, "lines": body.split("\n"), "errs": errs, "fatal": None,
            "normalized": [x for x, f in (("去掉 BOM", bom), ("CRLF 换行改为 LF", crlf)) if f]}


def md_blocks(lines: list, start: int = 0, stop: int | None = None) -> list:
    """Logical Markdown blocks as (first, end_exclusive, kind). kind is one of blank, code, anchor, heading,
    image, table, quote, html, item (a list item with its continuation lines) or para (soft-wrapped lines of
    one paragraph). Page anchors are only ever inserted between blocks, never inside one."""
    stop = len(lines) if stop is None else stop

    def starts_block(s):  # lines that open a new block even without a blank line before them
        st = s.strip()
        return (not st or FENCE_RE.match(s) or ANCHOR_RE.fullmatch(st) or HEAD_RE.match(s) or RULE_RE.fullmatch(st)
                or st.startswith(("|", ">", "![")) or ITEM_RE.match(s))

    out, i = [], start
    while i < stop:
        ln = lines[i]
        s = ln.strip()
        j = i + 1
        fm = FENCE_RE.match(ln)
        if not s:
            kind = "blank"
        elif fm:
            fence = fm.group(1)
            close = re.compile(re.escape(fence[0]) + "{%d,}\\s*" % len(fence))
            while j < stop and not close.fullmatch(lines[j].strip()):
                j += 1
            j, kind = min(j + 1, stop), "code"
        elif ANCHOR_RE.fullmatch(s):
            kind = "anchor"
        elif RULE_RE.fullmatch(s):
            kind = "rule"
        elif HEAD_RE.match(ln):
            kind = "heading"
        elif s.startswith("|"):
            while j < stop and lines[j].strip().startswith("|"):
                j += 1
            kind = "table"
        elif s.startswith(">"):
            while j < stop and lines[j].strip() and not (FENCE_RE.match(lines[j]) or HEAD_RE.match(lines[j])
                                                          or ANCHOR_RE.fullmatch(lines[j].strip())):
                j += 1
            kind = "quote"
        elif s.startswith("<") and not ANCHOR_RE.match(s):
            while j < stop and lines[j].strip():
                j += 1
            kind = "html"
        elif s.startswith("!["):
            kind = "image"
        else:
            kind = "item" if ITEM_RE.match(ln) else "para"
            while j < stop and not starts_block(lines[j]):
                j += 1
        out.append((i, j, kind))
        i = j
    return out


def real_anchors(lines: list, blocks: list | None = None) -> list:
    """[(line, page, standalone)] for every page anchor outside code blocks, in file order."""
    out = []
    for s, e, k in (blocks if blocks is not None else md_blocks(lines)):
        if k == "code":
            continue
        for x in range(s, e):
            for m in ANCHOR_RE.finditer(lines[x]):
                out.append((x, int(m.group(1)), k == "anchor"))
    return out


def align_units(lines: list, blocks: list | None = None) -> list:
    """[(first_line, text)] of the blocks that carry page evidence: paragraphs (all their soft-wrapped lines),
    list items (reference entries) and block quotes. The header block (before the first anchor or ## heading),
    headings, figures, captions, tables and code are left out: floats follow the section that cites them."""
    blocks = blocks if blocks is not None else md_blocks(lines)
    cands = [x for x, _, _ in real_anchors(lines, blocks)]
    cands += [s for s, e, k in blocks if k == "heading" and not lines[s].lstrip().startswith("# ")]
    if cands:
        start = min(cands)
    else:
        start = next((s + 1 for s, e, k in blocks if k == "heading"), 0)
    units = []
    for s, e, k in blocks:
        if s < start or k not in ("para", "item", "quote"):
            continue
        txt = " ".join(ANCHOR_RE.sub("", lines[x]).strip() for x in range(s, e)).strip()
        if not txt or ((CAPLINE_RE.match(txt) or CAPSTART_RE.match(txt)) and not META_LINE_RE.match(txt)):
            continue
        units.append((s, txt))
    return units


def line_pages(lines: list, blocks: list | None = None) -> list:
    """Page of every body line according to the anchors in the file (None before the first)."""
    at = {}
    for x, p, _ in real_anchors(lines, blocks):
        at[x] = p
    page, out = None, []
    for i in range(len(lines)):
        page = at.get(i, page)
        out.append(page)
    return out


def page_evidence(unit_tokens: list, pages: list):
    ptok = [sig_tokens(t) for _, t in pages]
    P = len(pages)
    df = Counter()
    for s in ptok:
        df.update(s)
    idf = {k: math.log((P + 1) / (v + 0.5)) for k, v in df.items()}
    S = [[sum(idf.get(t, 0.0) for t in ut if t in ptok[p]) for p in range(P)] for ut in unit_tokens]
    return S, ptok, idf


def auto_assign(S: list, jump: float = 0.3) -> list:
    """Monotone page assignment maximising evidence minus jump × pages skipped (no anchors to start from).
    new[p] = S[i][p] + max_{q<=p}(dp[q] - jump*(p-q)) = S[i][p] + max_{q<=p}(dp[q] + jump*q) - jump*p."""
    n, P = len(S), len(S[0])
    dp = [S[0][p] - jump * p for p in range(P)]
    back = []
    for i in range(1, n):
        new, bk = [0.0] * P, [0] * P
        best, bq = float("-inf"), 0
        for p in range(P):
            if dp[p] + jump * p > best:
                best, bq = dp[p] + jump * p, p
            new[p], bk[p] = best - jump * p + S[i][p], bq
        dp = new
        back.append(bk)
    p = max(range(P), key=lambda q: dp[q])
    out = [0] * n
    out[-1] = p
    for i in range(n - 1, 0, -1):
        p = back[i - 1][p]
        out[i - 1] = p
    return out


def hybrid_assign(S: list, Sh: list, ptok: list, idf: dict, unit_tokens: list, model: list):
    """Keep the translator's pages; move a paragraph to the neighbouring page only when (1) that page holds
    several of its tokens the translator's page lacks, (2) the overall evidence clearly favours it, and (3) the
    opening of the paragraph (Sh) also points there: a paragraph that starts on page m and runs onto m+1 belongs
    to m even when most of its numbers are on m+1. Then restore monotone order with the fewest changes."""
    n, P = len(S), len(S[0])
    strong = {}
    for i in range(n):
        m = model[i]
        if m is None:
            continue
        b = max(range(P), key=lambda q: S[i][q])
        if b == m or abs(b - m) != 1:
            continue
        spec = [t for t in unit_tokens[i] if t in ptok[b] and t not in ptok[m]]
        # precise numbers (decimals, 3+ digits) are the hardest evidence: all on b, none on m settles it even
        # when shared words (a caption on page m using the same terms) blur the overall score
        nums = {t for t in unit_tokens[i] if re.fullmatch(r"\d+\.\d+|\d{3,}", t)}
        by_numbers = len(nums & ptok[b]) >= 2 and not (nums & ptok[m])
        if (S[i][b] - S[i][m] >= 4.0 and len(spec) >= 2 and (S[i][m] <= 0.25 * S[i][b] or by_numbers)
                and Sh[i][b] > Sh[i][m]):
            strong[i] = (b, sorted(spec, key=lambda t: -idf.get(t, 0.0))[:6])

    def cost(i, p):
        if i in strong:
            return 0.0 if p == strong[i][0] else 5.0
        if model[i] is None:
            return 0.0
        return 0.0 if p == model[i] else 1.0 + 0.001 * abs(p - model[i])

    INF = float("inf")
    dp = [cost(0, p) for p in range(P)]
    back = []
    for i in range(1, n):
        new, bk = [INF] * P, [0] * P
        best, bq = INF, 0
        for p in range(P):
            if dp[p] < best:
                best, bq = dp[p], p
            new[p], bk[p] = best + cost(i, p), bq
        dp = new
        back.append(bk)
    p = min(range(P), key=lambda q: dp[q])
    out = [0] * n
    out[-1] = p
    for i in range(n - 1, 0, -1):
        p = back[i - 1][p]
        out[i - 1] = p
    return out, strong


def head_text(t: str) -> str:
    """Opening of a paragraph: decides the page it starts on."""
    return t[:max(150, len(t) // 3)]


def plan_anchors(doc: dict, pages: list, force_auto: bool = False) -> dict:
    """Decide the page of every text paragraph. Mode 'check' when the file has anchors (translator's pages,
    corrected on strong evidence), 'auto' when it has none (or force_auto)."""
    lines = doc["lines"]
    blocks = md_blocks(lines)
    ul = align_units(lines, blocks)
    units, texts = [s for s, _ in ul], [t for _, t in ul]
    pnos = [p for p, _ in pages]
    idx = {p: k for k, p in enumerate(pnos)}
    lp = line_pages(lines, blocks)
    has_anchors = any(x is not None for x in lp)
    res = {"units": units, "texts": texts, "blocks": blocks, "pnos": pnos, "old": [lp[i] for i in units],
           "has_anchors": has_anchors, "mode": "auto" if (force_auto or not has_anchors) else "check",
           "strong": {}, "no_evidence": 0, "empty_pages": []}
    if not units or not pages:
        res["new"] = res["old"]
        return res
    utok = [sig_tokens(t) for t in texts]
    htok = [sig_tokens(head_text(t)) for t in texts]
    S, ptok, idf = page_evidence(utok, pages)
    P = len(pages)
    Sh = [[sum(idf.get(t, 0.0) for t in ht if t in ptok[p]) for p in range(P)] for ht in htok]
    res["no_evidence"] = sum(1 for row in S if max(row) <= 0)
    if res["mode"] == "auto":
        # mild position prior: a paragraph a third into the translation sits about a third into the source
        # prose. Stops evidence-free openings (abstracts) from sliding onto page 2; tested beta 0-40, 8 is safe.
        # The opening of each paragraph counts twice (a paragraph belongs to the page it starts on).
        tl = [len(t) for t in texts]
        tot, cum, ft = sum(tl) or 1, 0, []
        for x in tl:
            ft.append((cum + x / 2) / tot)
            cum += x
        pc = [prose_chars(t) for _, t in pages]
        ptot, c, fs = sum(pc) or 1, 0, []
        for x in pc:
            fs.append((c + x / 2) / ptot)
            c += x
        assign = auto_assign([[S[i][p] + Sh[i][p] - 8.0 * abs(ft[i] - fs[p]) for p in range(P)]
                              for i in range(len(units))])
    else:
        def to_idx(p):
            if p is None:
                return None
            if p in idx:
                return idx[p]
            return min(range(len(pnos)), key=lambda k: abs(pnos[k] - p))
        assign, strong = hybrid_assign(S, Sh, ptok, idf, utok, [to_idx(p) for p in res["old"]])
        res["strong"] = {units[i]: (pnos[b], toks) for i, (b, toks) in strong.items()}
    res["new"] = [pnos[k] for k in assign]
    covered = set(res["new"])
    res["empty_pages"] = [p for p in pnos if p not in covered]
    return res


def place_anchors(lines: list, units: list, final: list, pnos: list, blocks: list | None = None) -> list:
    """Rewrite the body with one standalone <!-- pN --> per source page, inserted only between blocks (never
    inside a paragraph, table, list item, quote or code block; anchors inside code are left alone). An existing
    anchor stays where it is when it already sits in the right gap; otherwise the anchor goes before the
    headings that open the page's first paragraph. Pages with no text paragraph keep their anchor or stack
    before the next page's."""
    blocks = blocks if blocks is not None else md_blocks(lines)
    N = len(lines)
    kind, bstart, bend = [None] * N, list(range(N)), {}
    for s, e, k in blocks:
        bend[s] = e
        for x in range(s, e):
            kind[x], bstart[x] = k, s

    def prev_content(i):
        j = i - 1
        while j >= 0 and kind[j] == "anchor":
            j -= 1
        return j

    # pairs that become one block once old anchors are removed (paragraph split by an anchor line, table rows,
    # an image and its caption): an anchor must not go between them
    bad_pairs = {("table", "table"), ("para", "para"), ("quote", "quote"), ("image", "para"), ("item", "para")}

    def legal(i):
        while i < N and kind[i] == "anchor":  # old anchors disappear: judge the real neighbours
            i += 1
        if i <= 0 or i >= N:
            return True
        j = prev_content(i)
        if j < 0 or not lines[j].strip() or not lines[i].strip():
            return True
        return (kind[j], kind[i]) not in bad_pairs

    def snap(i):
        i = bstart[i] if i < N else i
        while not legal(i):
            i = bstart[prev_content(i)]
        return i

    model_pos = {}
    for x, p, _ in real_anchors(lines, blocks):
        model_pos.setdefault(p, snap(bstart[x]))
    first_line = {}
    for li, pg in zip(units, final):
        first_line.setdefault(pg, li)

    def last_unit_before(p):
        c = [li for li, pg in zip(units, final) if pg < p]
        return c[-1] if c else -1

    def default_pos(hi, lo):
        j, k = hi, hi - 1
        while k > lo:
            if kind[k] in ("blank", "anchor"):
                k -= 1
                continue
            if kind[k] == "heading" and not lines[k].lstrip().startswith("# "):
                j = k
                k -= 1
                continue
            break
        return j

    pos = {}
    for p in pnos:
        if p in first_line:
            lo, hi = last_unit_before(p), first_line[p]
            mp = model_pos.get(p)
            pos[p] = mp if mp is not None and lo < mp <= hi else default_pos(hi, lo)
    for p in pnos:  # pages without a paragraph starting on them (continuation, full-page figure/table, blank)
        if p in pos:
            continue
        prev = max((pos[q] for q in pos if q < p), default=-1)
        nxt = min((pos[q] for q in pos if q > p), default=None)
        mp = model_pos.get(p)
        if mp is not None and prev <= mp <= (nxt if nxt is not None else N):
            pos[p] = mp
        elif nxt is not None:
            pos[p] = nxt
        else:
            pos[p] = bend.get(units[-1], N) if units else N
    last = -1
    for p in pnos:  # legal, and monotone with page order
        pos[p] = max(snap(pos[p]), last)
        last = pos[p]
    at = defaultdict(list)
    for p, li in pos.items():
        at[li].append(p)
    out = []
    for i in range(N + 1):
        for p in sorted(at.get(i, [])):
            if out and out[-1].strip():
                out.append("")
            out += [f"<!-- p{p} -->", ""]
        if i == N:
            break
        ln, k = lines[i], kind[i]
        if k == "anchor":
            continue
        if k != "code" and ANCHOR_RE.search(ln):
            ln = ANCHOR_RE.sub("", ln).rstrip()
            if not ln.strip():
                continue
        if k == "blank" and out and not out[-1].strip():
            continue
        out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return out + [""]


def set_yaml_field(text: str, key: str, value: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    head, rest = text[:end], text[end:]
    line = f'{key}: "{value}"'
    if re.search(rf"(?m)^{re.escape(key)}:", head):
        head = re.sub(rf"(?m)^{re.escape(key)}:.*$", line, head)
    else:
        head += "\n" + line
    return head + rest


def write_atomic(path: str, text: str) -> None:
    """Write to a temporary file in the same folder, then replace: an interrupted run never leaves half a file."""
    tmp = path + ".pdftrans-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def anchor_layout_problems(lines: list, blocks: list, pnos: list) -> list:
    """Structural problems of the anchors already in the file, against the source page list."""
    ra = real_anchors(lines, blocks)
    got = [p for _, p, _ in ra]
    probs = []
    dup = sorted({p for p in got if got.count(p) > 1})
    if dup:
        probs.append("重复 " + ", ".join(f"p{p}" for p in dup))
    extra = sorted({p for p in got if p not in set(pnos)})
    if extra:
        probs.append("超出原文页码 " + ", ".join(f"p{p}" for p in extra))
    if got != sorted(got):
        probs.append("顺序不递增")
    miss = [p for p in pnos if p not in set(got)]
    if miss:
        probs.append("缺 " + ", ".join(f"p{p}" for p in miss[:10]) + (" …" if len(miss) > 10 else ""))
    inline = [x for x, _, st in ra if not st]
    if inline:
        probs.append(f"{len(inline)} 个没有单独成行")
    return probs


def cmd_anchor(a) -> int:
    if not os.path.isdir(a.path):
        die(f"不是文件夹：{a.path}")
    n = changed = refused = 0
    verb = "将" if a.dry_run else "已"
    for name, folder in iter_papers(a.path):
        doc = load_translation(folder)
        if not doc:
            continue
        n += 1
        if doc["fatal"]:
            print(f"[拒绝] {name}  {doc['fatal']}")
            refused += 1
            continue
        if doc["errs"]:
            print(f"[拒绝] {name}  YAML 头有问题（{doc['errs'][0]}）：先修好再运行 anchor，以免改坏文件")
            refused += 1
            continue
        sp = source_path(folder)
        pages = source_pages(folder)
        if not sp:
            print(f"[跳过] {name}  没有 _work/source.txt：先用原 PDF 运行 extract")
            continue
        if not pages:
            print(f"[跳过] {name}  {os.path.relpath(sp, folder)} 没有 <<<PAGE n>>> 分页标记，无法核对页码")
            continue
        r = plan_anchors(doc, pages, a.auto)
        lines, units, blocks = doc["lines"], r["units"], r["blocks"]
        if not units:
            print(f"[跳过] {name}  没有可对齐的正文段落")
            continue
        texts = dict(zip(units, r["texts"]))
        moved = [(units[k], o, w) for k, (o, w) in enumerate(zip(r["old"], r["new"])) if o != w]
        probs = anchor_layout_problems(lines, blocks, r["pnos"])
        want = "auto" if r["mode"] == "auto" else "checked"
        meta_ok = not doc["has_yaml"] or str(doc["fields"].get("anchors", "")).strip() == want
        off = doc["off"]
        rewrite = True
        if r["mode"] == "auto":
            head = (f"[自动] {name}  {'原稿无锚点，' if not r['has_anchors'] else '按 --auto 重排，'}{verb}插入 {len(r['pnos'])} 个锚点；"
                    f"{r['no_evidence']} 段没有数字/外文可比对，页码按上下文推定（误差可能 ±1 页）")
        elif not moved and not probs:
            rewrite = False
            if meta_ok and not doc["normalized"]:
                print(f"[OK]   {name}  锚点与原文一致")
                continue
            head = f"[OK]   {name}  锚点与原文一致" + ("" if meta_ok else f"；{verb}写入 anchors: \"checked\"")
        elif not moved:
            head = f"[整理] {name}  段落页码无误，但锚点{'；'.join(probs)}：{verb}重排为每页一个"
        else:
            head = f"[校正] {name}  {len(moved)} 段页码与原文证据不符，{verb}调整" + (f"；另：锚点{'；'.join(probs)}" if probs else "")
        print(head)
        if doc["normalized"]:
            print(f"    编码规范化：{'、'.join(doc['normalized'])}")
        shown = 0
        for li, o, w in moved:
            if r["mode"] == "auto" or shown >= a.show:
                break
            ev = r["strong"].get(li)
            why = f"  依据：{', '.join(ev[1])}" if ev else "  （随相邻段落调整）"
            print(f"    L{li + off} p{o}→p{w}{why}  {texts[li][:30]}")
            shown += 1
        if r["mode"] != "auto" and len(moved) > shown:
            print(f"    …另有 {len(moved) - shown} 段")
        if r["empty_pages"]:
            print(f"    没有段落从这些页开始（续段页、纯图/表页，或漏译，请核对）：{', '.join('p%d' % p for p in r['empty_pages'][:20])}")
        if r["mode"] == "check" and r["has_anchors"]:
            n_model = len({p for p in r["old"] if p})
            if n_model < 0.5 * len(r["pnos"]):
                print(f"    提示：原稿锚点只覆盖 {n_model}/{len(r['pnos'])} 页，可考虑 --auto 整篇重排")
        if a.dry_run:
            continue
        backup_once(folder, doc["path"], re.sub(r"\.md$", "", doc["name"]) + ".before-anchor.md")
        prefix = doc["text"][:len(doc["text"]) - len(doc["body"])]
        body = "\n".join(place_anchors(lines, units, r["new"], r["pnos"], blocks)) if rewrite else doc["body"]
        write_atomic(doc["path"], set_yaml_field(prefix + body, "anchors", want))
        changed += 1
    if not n:
        die("没找到译稿", 1)
    tail = "预览，未改动文件" if a.dry_run else f"改动 {changed} 篇（首次改动前的原稿在 _work/backup/）"
    print(f"\n共 {n} 篇，{tail}" + (f"；拒绝处理 {refused} 篇" if refused else ""))
    return 1 if refused else 0


# ----------------------------------------------------------------------------- v3: checks
NUMCHK_RE = re.compile(r"(?<![\d.])(\d+\.\d+|\d{3,})(?!\.?\d)")
POW_LOST_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s*[×xX]\s*10([1-9]\d?)(?![\d.⁰¹²³⁴⁵⁶⁷⁸⁹])"  # 1 × 106, not × 100
                         r"|(?<![\d.A-Za-z])10([1-9])\s*(?:CFU|cfu|copies|cells|个细胞|个菌)")  # 106 CFU for 10⁶ CFU
PTAG_RE = re.compile(r"\[p(\d+)(?:\s*[-–,，]\s*p?(\d+))?\]")
LINE_CUT = 2000  # file readers cut lines at about this length (check warns above it)
UNIT_PAT = (r"(wt\s?%|%|°C|kDa|MPa|kPa|GPa|Pa|kHz|MHz|Hz|nm|μm|mm|cm|μL|mL|ng|μg|mg|kg|g|nM|μM|mM|mol|"
            r"h|min|CFU|rpm|kV|mV|V|mW|W|J|eV|U|IU)(?![A-Za-z])")
_NUM = r"\d+(?:\.\d+)?"
# not an exponent: mg L\u22121 min, 10\u22123 mM, 1 \u00d7 10\u22123 mg (a true minus sign; '10-30 mM' with a hyphen is a range)
_NOT_EXP = r"(?<![A-Za-z][\u2212\u207b-])(?<!\b10[\u2212\u207b])"
# in the source a list may close on a unit after a hyphenated modifier ('scale bars: top-2 mm, bottom-500 \u03bcm'),
# so only a true minus sign blocks there: ASCII '-' would hide the number ('CaO-5% P2O5')
_NOT_EXP_LIST = r"(?<![A-Za-z][\u2212\u207b])(?<!\b10[\u2212\u207b])"
NUM_UNIT_RE = re.compile(r"(?<![\d.A-Za-z])" + _NOT_EXP + r"(" + _NUM + r")\s*" + UNIT_PAT)
# in the source a unit may close a list or range: "6 and 24 h", "66.03 and 59.5%", "20 to 50 °C", "12-h", "565/578 nm"
NUM_LIST_UNIT_RE = re.compile(r"(?<![\d.A-Za-z])" + _NOT_EXP_LIST + r"(" + _NUM + r"(?:\s*(?:,|，|and|or|to|vs\.?|–|—|-|~|/|、|×)\s*" + _NUM
                              + r")*)\s*[-‐]?\s*" + UNIT_PAT)
PM_RE = re.compile(r"(?<![\d.])(" + _NUM + r")\s*(?:±|\+/-|\+/−)\s*(" + _NUM + r")(?!\d|\.\d)")


def norm_units(t: str) -> str:
    """Spaces, degree signs, micro signs and full-width percent written one way, so that 37 \u25e6C = 37 \u00b0C."""
    t = re.sub(r"[\u00a0\u2002-\u2009\u202f]", " ", t).replace("\uff05", "%")
    t = norm_digits(t).replace("\u00b5", "\u03bc").replace("\u2103", "\u00b0C")
    t = re.sub(r"[\u00b0\u25e6\u02da\u00ba\ufffd]\s*C(?![A-Za-z])", "\u00b0C", t)
    return re.sub(r"(?<![A-Za-z])u(?=[gLM](?![A-Za-z]))", "\u03bc", t)


NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
UNIT_WORDS = [(r"micrograms?", "\u03bcg"), (r"milligrams?", "mg"), (r"nanograms?", "ng"), (r"kilograms?", "kg"),
              (r"grams?", "g"), (r"microlit(?:er|re)s?", "\u03bcL"), (r"millilit(?:er|re)s?", "mL"),
              (r"micromet(?:er|re)s?|microns?", "\u03bcm"), (r"millimet(?:er|re)s?", "mm"),
              (r"centimet(?:er|re)s?", "cm"), (r"nanomet(?:er|re)s?", "nm"), (r"minutes?|mins", "min"),
              (r"hours?|hrs?", "h"), (r"micromolar", "\u03bcM"), (r"millimolar", "mM"), (r"nanomolar", "nM"),
              (r"degrees? (?:Celsius|centigrade)", "\u00b0C"), (r"kilodaltons?", "kDa")]
_UNIT_WORD_RE = re.compile(r"(?<![\d.A-Za-z])((?:\d+(?:\.\d+)?|[A-Za-z]+(?:-[A-Za-z]+)?)[\s\u2010-]+)("
                           + "|".join(w for w, _ in UNIT_WORDS)
                           + r")(?![A-Za-z])", re.I)
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_ONES = "one|two|three|four|five|six|seven|eight|nine"
_NUMWORD_RE = re.compile(r"(?<![A-Za-z])(?:(" + _TENS + r")[\s-]+(" + _ONES + r")|(" + "|".join(NUM_WORDS) + r"))"
                         r"(?=[\s-]+" + UNIT_PAT + ")", re.I)


def augment_source(src_text: str) -> str:
    """The source plus a variant written the way a translation writes it, so that membership tests do not
    flag faithful renderings: 'One milliliter' / 'Four-micrometer-thick' / 'Twenty micrograms' / '5 minutes'
    -> 1 mL / 4-\u03bcm / 20 \u03bcg / 5 min; decimal commas (0,1 mg) -> 0.1; 'No.30013' -> No. 30013.
    Only ever adds candidates; nothing in the source is hidden."""
    def unit(m):  # only right after a number or a number word: '5 minutes', 'Four-micrometer', not 'Gram-negative'
        lead = m.group(1).strip(" -\u2010").lower()
        if not (re.fullmatch(r"\d+(?:\.\d+)?", lead) or all(x in NUM_WORDS for x in lead.split("-"))):
            return m.group(0)
        # 'gram-' + word is the Gram stain ('Two gram-variable isolates', 'Ten Gram-positive'), not grams, unless
        # the number is hyphen-joined to it as well ('five-gram-heavy'); other units keep their hyphenated
        # adjective ('5 micron-thick', '5- micron-thick', 'two hour-long'). rstrip: the separator may end in a space.
        if (m.group(2).lower().startswith("gram") and re.match(r"[\u2010-][A-Za-z]", m.string[m.end():])
                and m.group(1).rstrip()[-1] not in "-\u2010"):
            return m.group(0)
        return m.group(1) + next(s for w, s in UNIT_WORDS if re.fullmatch(w, m.group(2), re.I))
    t = _UNIT_WORD_RE.sub(unit, src_text)

    def num(m):
        if m.group(3):
            return str(NUM_WORDS[m.group(3).lower()])
        return str(NUM_WORDS[m.group(1).lower()] + NUM_WORDS[m.group(2).lower()])
    t = _NUMWORD_RE.sub(num, t)
    # only before a unit or \u00b1 (0,1 mg): '[1,2]' and 'refs 3,4' are lists, not decimals
    t = re.sub(r"(?<![\d,.])(\d+),(\d{1,2})(?![\d,])(?=\s*(?:\u00b1|[-\u2010]?\s*" + UNIT_PAT + "))", r"\1.\2", t)
    t = re.sub(r"(?<=[A-Za-z])\.(?=\d)", ". ", t)
    t = re.sub(r"(?<=[a-z]{2})(?=" + _NUM + r"\s*[-\u2010]?\s*" + UNIT_PAT + ")", " ", t)  # 'into5 mL' (lost space)
    return src_text if t == src_text else src_text + "\n\n" + t


def src_num_context(src_text: str) -> tuple:
    """(number, unit) pairs and (value, sd) pairs that occur in the source."""
    t = norm_units(src_text)
    pairs = set()
    for m in NUM_LIST_UNIT_RE.finditer(t):
        for a in re.findall(_NUM, m.group(1)):
            pairs.add((a, m.group(2).replace(" ", "")))
    return pairs, set(PM_RE.findall(t))


def _cap_key(kind_label: str, num: str, src_kind: str | None = None) -> str:
    """Comparable key for a caption: F3, FS2, T1, SC1 (a trailing panel letter is dropped)."""
    num = re.sub(r"(?<=\d)[A-Za-z]$", "", num.strip())
    kind = src_kind or ("table" if TABLE_LABEL_RE.search(kind_label) else
                        "scheme" if SCHEME_LABEL_RE.search(kind_label) else "fig")
    if kind == "table":
        return "T" + num
    if kind == "scheme":
        return "SC" + num
    if kind == "suppfig" and not num.startswith("S"):
        num = "S" + num
    return "F" + num


def _cap_label(key: str) -> str:
    if key.startswith("SC"):
        return "方案 " + key[2:]
    return ("表 " if key.startswith("T") else "图 ") + key[1:]


def _cap_numbers(text: str, source: bool = False) -> set:
    """Precise quantities of a caption: decimals, integers of 2+ digits, number + unit, value \u00b1 sd.
    Figure references (Fig. 2a, \u56fe S3), citation markers and the caption label are removed first; lone small
    integers (n = 3, often written as a word) are left out. In the source a unit may close a list (50 and 100 \u03bcm)."""
    t = norm_units(augment_source(text) if source else text)
    t = re.sub(r"\[\s*\d+(?:\s*[,\uff0c\u2013\-]\s*\d+)*\s*\]", " ", t)
    t = re.sub(r"^\s*\**\s*[^\d\s*]{0,24}\s*S?\d{1,3}[A-Za-z]?\s*[.:\uff1a|]?\**", " ", t)  # the label
    t = re.sub(r"[pP]\s*[<>\u2264\u2265]\s*0?\.\d+", " ", t)  # significance thresholds, often printed in the figure
    t = FIGREF_RE.sub(" ", t)
    out = set()
    if source:
        for m in NUM_LIST_UNIT_RE.finditer(t):
            for a in re.findall(_NUM, m.group(1)):
                out.add(f"{a} {m.group(2).replace(' ', '')}")
    else:
        for a, u in NUM_UNIT_RE.findall(t):
            out.add(f"{a} {u.replace(' ', '')}")
    for a, b in PM_RE.findall(t):
        out.add(f"{a} \u00b1 {b}")
    out.update(re.findall(r"(?<![\d.A-Za-z])(\d+\.\d+|\d{2,})(?!\d|\.\d)", t))
    return out


SCALE_RE = re.compile(r"标尺|比例尺|scale\s*bars?|bars?\s*[=:：]", re.I)


def caption_issues(lines: list, L0: int, folder: str, pages: list) -> tuple:
    """Compare every translated caption with the original caption of the same number (manifest.json, completed
    from source.txt): quantities the original caption does not have (read off the figure, a scale bar, another
    figure's caption), quantities of the original missing from the translation (a dropped half-sentence), and
    captions whose numbers all belong to another figure. Returns (messages, body lines compared)."""
    caps = load_manifest(folder).get("captions", [])
    if not caps or not pages:
        return [], set()
    byp = {p: re.sub(r"\s+", " ", t) for p, t in pages}
    starts = defaultdict(list)  # where each caption begins on its page
    for c in caps:
        txt = re.sub(r"\s+", " ", c.get("text", "")).strip()
        i = byp.get(c.get("page"), "").find(txt[:30]) if txt else -1
        starts[c.get("page")].append((i, c, txt))
    src, head = {}, {}
    for pg, lst in starts.items():
        lst.sort(key=lambda x: x[0])
        for n, (i, c, txt) in enumerate(lst):
            k = _cap_key("", str(c.get("num", "")), c.get("kind"))
            if i < 0:
                win = txt
            else:  # from this caption to the next one on the page (or 2,500 characters)
                nxt = next((j for j, _, _ in lst[n + 1:] if j > i), None)
                win = byp[pg][i:min(nxt if nxt is not None else len(byp[pg]), i + 2500)]
            src[k] = (src[k] + " " + win) if k in src else win
            cut = txt[:max(0, txt.rfind(" ", 0, max(0, len(txt) - 12)))]  # surely caption text, whole tokens only
            head[k] = (head[k] + " " + cut) if k in head else cut
    srcq = {k: _cap_numbers(v, source=True) for k, v in src.items()}
    headq = {k: _cap_numbers(v, source=True) for k, v in head.items()}
    extra, lost, swapped, covered = [], [], [], set()
    groups = defaultdict(list)  # one figure may be captioned in several parts (图 4a、b / 图 4c、d)
    for s, e, k in md_blocks(lines):
        if k != "para" or not CAPLINE_RE.match(lines[s]) or META_LINE_RE.match(lines[s]):
            continue
        bold = re.match(r"\s*\*\*([^*]+)\*\*", lines[s])
        labs = list(re.finditer(r"([^\d\s\uff1a:\u3001,]{0,8})\s*(S?\d{1,3})[A-Za-z]?", bold.group(1))) if bold else []
        if not labs:
            continue
        key = _cap_key(labs[-1].group(1), labs[-1].group(2))  # 补充文件 1：图 S1 -> 图 S1
        if key in srcq:
            groups[key].append((s, e))
    for key, spans in groups.items():
        s0 = spans[0][0]
        for s, e in spans:
            covered.update(range(s, e))
        text = " ".join(" ".join(lines[s:e]) for s, e in spans)
        label, where = _cap_label(key), f"L{s0 + L0}"
        tq, sq = _cap_numbers(text), srcq[key]
        # swapped caption: its distinctive numbers (decimals, value \u00b1 sd; scale bars repeat across figures)
        # are all absent from its own original caption and present in another one
        dist = {q for q in tq if "." in q.split(" ")[0] or "\u00b1" in q}
        if len(dist) >= 3 and not (dist & sq):
            best = max(((len(dist & q), kk) for kk, q in srcq.items() if kk != key), default=(0, ""))
            if best[0] >= max(3, len(dist) * 2 // 3):
                swapped.append(f"{label} {where}（数字与原文{_cap_label(best[1])}的图注一致）")
                continue
        add = tq - sq
        with_unit = {q.split(" ")[0] for q in add if " " in q}
        add = sorted(q for q in add if " " in q or q not in with_unit)  # 100 \u03bcm, not also 100
        tvals = {q.split(" ")[0] for q in tq}
        tvals |= set(re.findall(r"\d+(?:\.\d+)?", text.translate(SUPERSCRIPT_DIGITS)))
        miss = sorted(q for q in headq.get(key, set()) if " " not in q and q not in tvals)
        if add:
            extra.append((label, s0, add, text))
        if miss:
            lost.append(f"{label} {where}：{'、'.join(miss[:4])}")
    out = []
    if swapped:
        out.append(("WARN", "图注可能张冠李戴：" + "；".join(swapped[:4])))
    rows_out, n_scale = [], 0
    for label, s0, add, text in extra:
        flat = norm_units(re.sub(r"\s+", " ", text))
        sc, ot = [], []
        for q in add:
            m = re.search(r"(?<![\d.])" + re.escape(q).replace(r"\ ", r"\s*") + r"(?!\d)", flat)
            # same clause: back to the previous full stop, at most 60 characters (标尺：5 μm（B）；20 μm（D）)
            before = re.split(r"[。！？]|\.\s", flat[max(0, m.start() - 60):m.start()])[-1] if m else ""
            hits = list(SCALE_RE.finditer(before))
            # 标尺：5 μm（B）；20 μm（D） counts; 标尺：100 μm；切片厚度为 30 μm does not (words in between)
            gap = before[hits[-1].end():] if hits else ""
            (sc if hits and len(re.findall(r"[\u4e00-\u9fff]", gap)) <= 4 else ot).append(q)
        if sc:
            n_scale += 1
            rows_out.append(f"{label} L{s0 + L0} [标尺] 「{'、'.join(sc[:4])}」 {line_snip(text, sc[0])}")
        if ot:
            rows_out.append(f"{label} L{s0 + L0} 「{'、'.join(ot[:4])}」 {line_snip(text, ot[0])}")
    if rows_out:
        hint = f"标 [标尺] 的 {n_scale} 条是从图上读的标尺，原图注文字里没有就删掉；" if n_scale else ""
        out.append(("WARN", f"图注里有原图注没有的数字或单位 {len(extra)} 条图注（{hint}其余：从图上读出、单位抄错或抄串了？"
                            f"图注只译原图注）：" + details(rows_out)))
    if lost:
        out.append(("WARN", f"原图注里的数字译文图注里没有 {len(lost)} 条（可能漏译半句）："
                            + "；".join(lost[:5]) + (" …" if len(lost) > 5 else "")))
    return out, covered


DETAIL_INDENT = "\n          "
DETAIL_MAX = 6


def line_snip(line: str, val: str, width: int = 70) -> str:
    """A window of a translation line around a value (Markdown emphasis removed)."""
    t = re.sub(r"\s+", " ", re.sub(r"\*\*|`", "", line)).strip()
    i = -1
    for probe in (val, val.split(" ")[0]):
        m = re.search(r"(?<![\d.])" + re.escape(probe).replace(r"\ ", r"\s*"), norm_units(t))
        if m:
            i = m.start()
            break
    i = max(i, 0)
    s = max(0, i - width // 2)
    e = min(len(t), s + width)
    return ("…" if s else "") + t[s:e] + ("…" if e < len(t) else "")


UNIT_KINDS = {"mass": ("ng", "μg", "mg", "kg", "g"), "volume": ("μL", "mL"), "length": ("nm", "μm", "mm", "cm"),
              "concentration": ("nM", "μM", "mM", "mol"), "time": ("h", "min"), "pressure": ("Pa", "kPa", "MPa", "GPa"),
              "frequency": ("Hz", "kHz", "MHz"), "voltage": ("V", "mV", "kV"), "power": ("W", "mW"),
              "percent": ("%", "wt%")}
_UNIT_KIND = {u: k for k, us in UNIT_KINDS.items() for u in us}


def unit_kind(u: str) -> str:
    """What a unit measures (mg and μg: mass; mM and μM: concentration, not length like mm)."""
    u = u.replace(" ", "")
    return _UNIT_KIND.get(u, u)


def src_snip(num: str, pages_n: dict, near, unit: str = "", width: int = 56) -> str:
    """Where a bare number occurs in the source: first an occurrence with a unit of the same kind (150\u2013250 mm
    for 250 \u03bcm), then any unit, then none; within each, the translation line's page \u00b11 first. Small
    integers with no unit nearby are not shown (they occur everywhere and would only mislead)."""
    rx = re.compile(r"(?<![\d.])" + re.escape(num) + r"(?!\d|\.\d)")
    base = unit_kind(unit) if unit else ""
    best = None
    for p, t in pages_n.items():
        for m in rx.finditer(t):
            after = t[m.end():m.end() + 8]
            um = re.match(r"\s*[-\u2010]?\s*" + UNIT_PAT, after)
            kind = 0 if (um and base and unit_kind(um.group(1)) == base) else (1 if um else 2)
            dist = 0 if near and abs(p - near) <= 1 else 1
            key = (kind, dist, abs(p - (near or p)))
            if best is None or key < best[0]:
                best = (key, p, m)
    if best is None:
        return ""
    (kind, _, _), p, m = best
    if kind == 2 and not ("." in num or len(num) >= 3):
        return ""
    t = pages_n[p]
    s = max(0, m.start() - width // 2)
    e = min(len(t), s + width)
    return f"原文 p{p}：" + ("…" if s else "") + re.sub(r"\s+", " ", t[s:e]) + ("…" if e < len(t) else "")


def details(rows: list) -> str:
    """Indented detail lines under a warning (the first DETAIL_MAX)."""
    out = rows[:DETAIL_MAX]
    if len(rows) > DETAIL_MAX:
        out.append(f"…另 {len(rows) - DETAIL_MAX} 处")
    return "".join(DETAIL_INDENT + r for r in out)


def number_issues(lines: list, L0: int, stop: int, src_text: str, skip: set | None = None,
                  pages: list | None = None, lp: list | None = None) -> list:
    """Numbers in the translation that do not occur anywhere in the source text; number + unit and value \u00b1 sd
    combinations that do not occur in the source (wrong unit, wrong pairing, a value read off a figure); powers
    of ten that probably lost their superscript (1 \u00d7 106, 106 CFU). Headings, image links and references are
    skipped. A number that occurs in the source may still sit in the wrong group: that is not checked.
    Every finding is listed with the translation line around it (and, for combinations, where the bare number
    sits in the source), so most can be judged without opening a file."""
    aug = augment_source(src_text)
    srcnums = set(NUMCHK_RE.findall(norm_digits(aug)))
    spairs, spm = src_num_context(aug)
    skip = skip or set()
    miss, powlost, ctx, in_code = [], [], [], False
    for i, ln in enumerate(lines[:stop]):
        st = ln.strip()
        if st.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not st or st.startswith(("#", "![", "<!--")):
            continue
        t = re.sub(r"<sup>.*?</sup>|\[\s*\d+(?:\s*[,，–\-]\s*\d+)*\s*\]", " ", ln)  # citation markers are not data
        t = norm_digits(re.sub(r"\]\([^)]*\)", "]", t))
        t = re.sub(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:org|com|net|edu|gov|cn|de|uk|io)/\S*|\b10\.\d{4,9}/\S+", " ", t)
        lost_spans = []
        for m in POW_LOST_RE.finditer(t):
            powlost.append((i, m.group(0).strip()))
            lost_spans.append((m.start(), m.end()))
        if i in skip:  # captions are compared with their own original caption by caption_issues
            continue
        missing_here = set()
        for m in NUMCHK_RE.finditer(t):
            if any(a <= m.start() < b for a, b in lost_spans):
                continue
            v = m.group(1)
            if v in srcnums or re.match(r"\s*[万亿千]", t[m.end():]):
                continue
            miss.append((i, v))
            missing_here.add(v)
        tu = norm_units(t)
        for m in PM_RE.finditer(tu):
            a, b = m.group(1), m.group(2)
            if (a, b) not in spm and not ({a, b} & missing_here):
                ctx.append((i, f"{a} ± {b}"))
        for m in NUM_UNIT_RE.finditer(tu):
            a, u = m.group(1), m.group(2).replace(" ", "")
            if a in missing_here or any(x <= m.start() < y for x, y in lost_spans):
                continue
            if (a, u) not in spairs:
                ctx.append((i, f"{a} {m.group(2)}"))

    def page_of(i):
        return lp[i] if lp and i < len(lp) else None

    def rows(items, pages_n=None):
        out, seen = [], set()
        for i, v in items:
            if (i, v) in seen:  # the same value twice on one line: one row
                continue
            seen.add((i, v))
            r = f"L{i + L0} 「{v}」 {line_snip(lines[i], v)}"
            if pages_n and len(out) < DETAIL_MAX:  # only the rows details() shows get a source snippet
                parts = v.split(" ")
                ss = src_snip(parts[0], pages_n, page_of(i), parts[1] if len(parts) == 2 else "")
                if ss:
                    r += f"  ｜{ss}"
            out.append(r)
        return out
    out = []
    if powlost:
        r = rows(powlost)
        out.append(("WARN", f"疑似丢了上标的 10 的幂 {len(r)} 处（应为 10⁶ 之类？对照原页）：" + details(r)))
    if miss:
        r = rows(miss)
        out.append(("WARN", f"原文中找不到的数字 {len(r)} 处（抄错、换算、从图上读数，或译者自己加的说明？）：" + details(r)))
    if ctx:
        r = rows(ctx, {p: norm_units(t) for p, t in (pages or [])})
        out.append(("WARN", f"数字在原文里有，但这个「数字 + 单位」或「均值 ± SD」组合原文没有 {len(r)} 处"
                            f"（单位抄错、配错组、标尺、从图上读数，或原文提取丢了 μ？对照右侧原文）：" + details(r)))
    return out


def key_result_issues(kr: str, pages: list) -> list:
    """Each '；'-separated result must carry [pN]; its numbers must occur on that source page (±1)."""
    if not kr.strip():
        return []
    byno = {p: norm_digits(t) for p, t in pages}
    out = []
    segs = [s.strip() for s in re.split(r"[；;]", kr) if s.strip()]
    untagged = []
    for k, seg in enumerate(segs, 1):
        tags = PTAG_RE.findall(seg)
        nums = [x for x in NUM_TOKEN_RE.findall(norm_digits(PTAG_RE.sub("", seg))) if "." in x or len(x) >= 2]
        if not tags:
            untagged.append(str(k))
            continue
        if not byno or not nums:
            continue
        allowed = set()
        for a, b in tags:
            lo, hi = int(a), int(b) if b else int(a)
            allowed.update(range(lo - 1, hi + 2))
        have = set(NUM_TOKEN_RE.findall(" ".join(byno.get(p, "") for p in allowed)))
        lost = [x for x in nums if x not in have]
        if lost:
            where = "、".join(f"p{a}" + (f"-{b}" if b else "") for a, b in tags)
            out.append(("WARN", f"key_result 第 {k} 条的 {', '.join(lost[:4])} 不在原文 {where}（±1 页）"))
    if untagged:
        sugg = []
        btok = {p: set(NUM_TOKEN_RE.findall(t)) for p, t in byno.items()}
        for k in untagged:
            seg = segs[int(k) - 1]
            nums = {x for x in NUM_TOKEN_RE.findall(norm_digits(seg)) if "." in x or len(x) >= 3}
            if not nums or not byno:
                continue
            have = {p: len(nums & s) for p, s in btok.items()}
            top = max(have.values())
            best = [p for p, c in have.items() if c == top]
            if top and top >= (len(nums) + 1) // 2 and len(best) <= (3 if top >= 2 else 1):
                sugg.append(f"第 {k} 条 " + "/".join(f"[p{p}]" for p in sorted(best)) + f"（{top}/{len(nums)} 个数字在该页）")
        hint = f"（按数字推断：{'；'.join(sugg)}；核对后补上）" if sugg else ""
        out.append(("WARN", f"key_result 第 {'、'.join(untagged)} 条没有页码 [pN]{hint}"))
    return out


def find_fields_file(folder: str) -> str | None:
    d = os.path.abspath(folder)
    for _ in range(4):
        p = os.path.join(d, "_fields.md")
        if os.path.isfile(p):
            return p
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


VALUES_HEAD_RE = re.compile(r"取值|允许值|可选值|候选值|allowed|values?", re.I)


def _fields_table(path: str) -> list:
    """Data rows of the Markdown table(s) in _fields.md as (cells, value_column_or_None); each table's own
    header decides whether it has a value column (取值 / allowed values, third column or later)."""
    rows = [ln.strip() for ln in open(path, encoding="utf-8", errors="replace").read().splitlines()
            if ln.strip().startswith("|")]
    sep_re = re.compile(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?")
    cells = lambda r: [c.strip() for c in r.strip().strip("|").split("|")]  # noqa: E731
    out, vcol = [], None
    for i, r in enumerate(rows):
        if sep_re.fullmatch(r):
            continue
        if i + 1 < len(rows) and sep_re.fullmatch(rows[i + 1]):  # a header row: starts a new table
            vcol = next((k for k, c in enumerate(cells(r)) if k >= 2 and VALUES_HEAD_RE.search(c)), None)
            continue
        out.append((cells(r), vcol))
    return out


def parse_fields_file(path: str) -> list:
    """[(field, description)] from the Markdown table in _fields.md (header row and separator skipped;
    an optional value column is not part of the description)."""
    out = []
    for c, vcol in _fields_table(path):
        if len(c) < 2:
            continue
        name = c[0].strip("`* ")
        if re.fullmatch(r"[A-Za-z_][\w.\-]*", name):
            out.append((name, "|".join(x for k, x in enumerate(c[1:], 1) if k != vcol).strip()))
    return out


def parse_field_values(path: str) -> dict:
    """{field: [allowed values]} from the optional value column (取值 / allowed values) of _fields.md.
    Values are separated by / 、 ， or ；. 「无」 and 「未报告」 are always allowed."""
    out = {}
    for c, vcol in _fields_table(path):
        if vcol is not None and len(c) > vcol and c[vcol].strip():
            vals = [v.strip(" `*") for v in re.split(r"\s*[/、，,；;]\s*", c[vcol]) if v.strip(" `*")]
            if vals:
                out[c[0].strip("`* ")] = vals
    return out


def field_issues(fields: dict, defs: list, values: dict | None = None) -> list:
    out, missing = [], []
    for name, desc in defs:
        v = str(fields.get(name, "")).strip()
        if not v:
            missing.append(name)
            continue
        allowed = (values or {}).get(name)
        if allowed and "①" not in desc:
            ok = set(allowed) | {"无", "未报告"}
            got = [x.strip() for x in re.split(r"\s*[；;、/]\s*", v) if x.strip()]
            bad = [x for x in got if x not in ok and re.sub(r"[（(].*?[)）]$", "", x).strip() not in ok]
            if bad:
                out.append(("WARN", f"字段 {name} 的取值「{'」「'.join(bad[:4])}」不在 _fields.md 列出的取值里"
                                    f"（{' / '.join(allowed)}；多个值用「；」分隔，可加括号注释）"))
        if "①" in desc and v not in ("无", "未报告", "none", "n/a"):
            if not v.startswith(tuple(CIRCLED)):
                out.append(("WARN", f"字段 {name} 要按 ①②③ 编号逐条写（见 _fields.md）"))
                continue
            labels = re.findall(r"‖\s*([^\s：:`|]{1,12})\s*[：:]", desc)
            entries = [e for e in ENTRY_SPLIT_RE.split(v) if e.strip()]
            for lab in labels:
                bad = [e[0] for e in entries if not re.search(rf"‖\s*{re.escape(lab)}\s*[：:]", e)]
                if bad:
                    out.append(("WARN", f"字段 {name} 的第 {''.join(bad)} 条缺「‖ {lab}：」部分"))
    if missing:
        out.append(("WARN", f"缺少工作区字段（_fields.md）：{', '.join(missing)}；没有就写「无」，原文没交代写「未报告」"))
    return out


def prose_blocks(page_text: str) -> list:
    """Real paragraphs of a source page: blocks of 150+ characters that are not captions.
    Labels extracted from inside figures are short fragments and do not count."""
    return [b for b in re.split(r"\n\s*\n", page_text) if len(b.strip()) >= 150 and not CAP_RE.match(b)]


def prose_chars(page_text: str) -> int:
    return sum(len(re.sub(r"\s", "", b)) for b in prose_blocks(page_text))


def page_coverage_issues(lines: list, r: dict, pages: list) -> list:
    """Pages whose source has plenty of running text but whose translation is almost empty. Paragraphs count
    on the page decided by plan_anchors; tables count on the page of their anchor (source tables are text too)."""
    tr = Counter()
    for t, p in zip(r["texts"], r["new"]):
        if p is not None:
            tr[p] += len(re.sub(r"\s", "", t))
    lp = line_pages(lines, r["blocks"])
    for s, e, k in r["blocks"]:
        if k == "table" and lp[s] is not None:
            tr[lp[s]] += sum(len(re.sub(r"[\s|:\-]", "", lines[x])) for x in range(s, e))
    pc = {p: prose_chars(t) for p, t in pages}
    cand = [(p, n) for p, n in pc.items() if n >= 1200 and tr[p] < 0.12 * n]
    if not cand:
        return []
    # A paragraph that starts on page p-1 and runs across a figure page belongs to p-1 (one block, one page),
    # so the page it continues onto looks empty. The numbers found only on this source page survive translation:
    # nearly all of them in the translation just before or at this page = the text is there (a continued
    # paragraph); most of them missing from the whole translation = omission. Words are not used for the credit:
    # English words do not survive translation, so their share cannot tell a whole page from a fragment.
    ptok = {p: sig_tokens(" ".join(prose_blocks(t))) for p, t in pages}
    df = Counter(x for s in ptok.values() for x in s)
    utok = [sig_tokens(t) for t in r["texts"]]
    alltr = set().union(*utok) if utok else set()
    # translation chars per source prose char, from the pages that are not suspect (a missing page must not
    # lower the yardstick it is measured with)
    suspect = {c for c, _ in cand}
    okp = [q for q in pc if q not in suspect and pc[q] >= 300]
    ratio = sum(tr[q] for q in okp) / max(1, sum(pc[q] for q in okp)) if len(okp) >= 2 else None
    thin = []
    for p, n in cand:
        own = {x for x in ptok[p] if df[x] == 1}
        nums = {x for x in own if re.fullmatch(r"\d+(?:\.\d+)?", x)}
        near = set()
        for k, q in enumerate(r["new"]):
            if q is not None and p - 3 <= q <= p + 1:
                near |= utok[k]
        if len(nums) >= 3 and len(nums & near) >= 0.8 * len(nums):
            continue  # continued paragraph: this page's own numbers sit in the translation just before
        if len(nums) < 3 and ratio:  # no numbers to go by: enough translation for this page and the two before?
            win = [q for q in pc if p - 2 <= q <= p]
            if sum(tr[q] for q in win) >= 0.75 * ratio * sum(pc[q] for q in win):
                continue
        note = ""
        if len(nums) >= 3 and len(nums & alltr) < 0.3 * len(nums):
            note = f"；该页独有的 {len(nums)} 个数字译文里只有 {len(nums & alltr)} 个"
        elif len(nums) < 3:
            note = f"；该页没有足够的数字可比对，用 pair --page {p} 对照原文看一眼"
        thin.append(f"p{p}（原文正文 {n} 字，译文 {tr[p]} 字{note}）")
    if thin:
        return [("WARN", f"这些页译文明显偏少，可能漏译：{'；'.join(thin[:6])}")]
    return []


def v3_checks(folder: str, doc: dict, lines: list, L0: int, ref_i, anchors: list) -> list:
    msgs = []
    longl = [i + L0 for i, ln in enumerate(lines) if len(ln) > LINE_CUT]
    if longl:
        msgs.append(("WARN", f"超过 {LINE_CUT} 字的行 {len(longl)} 处（读文件工具会截断；在句末换行，不加空行）："
                             f"行 {', '.join(map(str, longl[:6]))}"))
    pages = source_pages(folder)
    fields = doc["fields"]
    if not pages:
        why = "没有分页标记 <<<PAGE n>>>" if source_path(folder) else "没有 _work/source.txt"
        msgs.append(("WARN", f"{why}：数字、页码锚点和 key_result 页码都没有核对（用原 PDF 运行 extract 后再 check）"))
    if pages:
        stop = ref_i if ref_i is not None else len(lines)
        cmsgs, covered = caption_issues(lines, L0, folder, pages)
        lp_all = line_pages(lines)
        msgs += number_issues(lines, L0, stop, "\n".join(t for _, t in pages), covered, pages, lp_all) + cmsgs
        if anchors:
            r = plan_anchors(doc, pages)
            moved = [(r["units"][k], o, w) for k, (o, w) in enumerate(zip(r["old"], r["new"])) if o != w]
            if moved:
                msgs.append(("WARN", f"页码锚点与原文对不上 {len(moved)} 段（"
                                     + "、".join(f"L{li + L0} p{o}→p{w}" for li, o, w in moved[:4])
                                     + "）→ 运行 anchor 自动校正"))
            if str(fields.get("anchors", "")).strip() != "auto":  # auto-placed pages are too soft to judge omissions
                msgs += page_coverage_issues(lines, r, pages)
    if doc["has_yaml"]:
        kr = str(fields.get("key_result", ""))
        msgs += key_result_issues(kr, pages)
        ff = find_fields_file(folder)
        if ff:
            msgs += field_issues(fields, parse_fields_file(ff), parse_field_values(ff))
    return msgs


def cmd_refresh(a) -> int:
    """Re-run extract for papers whose _work/ came from an older pdftrans, using the PDF path recorded in their
    manifest.json (or the file of the same name in --pdf-dir, given by the user). Rewrites only _work/source.txt,
    refs.md and manifest.json (recorded figures kept); never the translation or the images. Refuses a paper
    whose new page count differs from the old one (the anchors would no longer match)."""
    import contextlib
    import io
    import tempfile
    if not os.path.isdir(a.root):
        die(f"不是文件夹：{a.root}")
    n = done = skipped = failed = 0
    for name, folder in iter_papers(a.root):
        old = load_manifest(folder)
        if not old:
            continue
        n += 1
        rec = old.get("pdf") or ""
        pdf = rec if rec and os.path.isfile(rec) else None
        if not pdf and a.pdf_dir and rec:
            cand = os.path.join(a.pdf_dir, re.split(r"[\\/]", rec)[-1])
            pdf = cand if os.path.isfile(cand) else None
        tool = old.get("tool", "?")
        if not pdf:
            print(f"[跳过] {name}  找不到 manifest.json 记录的 PDF：{rec or '（没有记录）'}"
                  + ("" if a.pdf_dir else "；PDF 搬过家就加 --pdf-dir 指向现在的文件夹"))
            skipped += 1
            continue
        if tool == f"pdftrans {VERSION}" and not a.force:
            print(f"[OK]   {name}  已是 {tool} 提取的")
            continue
        if a.dry_run:
            print(f"[将重提] {name}  {tool} → pdftrans {VERSION}  （{pdf}）")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            try:  # extract reports through die() (SystemExit) and return codes; neither may end the batch
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    rc = cmd_extract(argparse.Namespace(pdf=pdf, outdir=tmp))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
            except Exception as e:  # a damaged PDF: report it and go on with the next paper
                rc = 1
                buf.write(f"\nERROR: {type(e).__name__}: {e}\n")
            notes = [ln.strip() for ln in buf.getvalue().splitlines()
                     if ln.startswith(("WARNING", "ERROR", "整份"))]
            if rc:
                print(f"[失败] {name}  重新提取出错（代码 {rc}），未改动" + "".join(f"\n    {x}" for x in notes))
                failed += 1
                continue
            new = load_manifest(tmp)
            if old.get("pages") and new.get("pages") != old.get("pages"):
                print(f"[拒绝] {name}  新提取 {new.get('pages')} 页，原来 {old.get('pages')} 页：PDF 可能不是同一份，未改动")
                skipped += 1
                continue
            wd = work_dir(folder)
            for fn in ("source.txt", "refs.md", "manifest.json"):
                backup_once(folder, os.path.join(wd, fn), fn.replace(".", ".before-refresh.", 1))
            new["figures"] = old.get("figures", [])
            new["pdf"] = pdf
            for fn in ("source.txt", "refs.md"):
                t = os.path.join(tmp, WORK, fn)
                if os.path.isfile(t):
                    shutil.copyfile(t, os.path.join(wd, fn))
            save_manifest(folder, new)
        fixes = (new.get("symbol_fixes") or {}).get("count", 0)
        print(f"[重提] {name}  {tool} → pdftrans {VERSION}；符号字体还原 {fixes} 处（旧文件在 _work/backup/）"
              + "".join(f"\n    {x}" for x in notes))
        done += 1
    if not n:
        die("没找到带 _work/manifest.json 的文献文件夹", 1)
    tail = "预览，未改动文件" if a.dry_run else f"重新提取 {done} 篇"
    print(f"\n共 {n} 篇，{tail}" + (f"；跳过 {skipped} 篇" if skipped else "") + (f"；失败 {failed} 篇" if failed else ""))
    return 1 if failed else 0


def cmd_selftest(a) -> int:
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    test = os.path.join(here, "..", "tests", "test_pdftrans.py")
    if not os.path.isfile(test):
        die("找不到 tests/test_pdftrans.py（技能包不完整？）", 1)
    return subprocess.call([sys.executable, test, os.path.abspath(__file__)])


# ----------------------------------------------------------------------------- v3.2: wrap
WRAP_AT = 1500   # lines longer than this are wrapped (spec: paragraphs over 1,500 characters)
WRAP_TO = 1000   # target length of the wrapped lines
SENT_END_RE = re.compile(r"(?:[。！？]|(?<=[a-z0-9)\]])\.(?=\s+[A-Z(]))[”’」』）)\]]*")


BLOCK_START_RE = re.compile(r"(?:[-*+]\s|\d{1,4}[.)]\s|#|>|\||```|~~~|!\[|<!--|-{3,}|\*{3,}|_{3,}|\$\$)")


def wrap_line(ln: str) -> list:
    """Split one long paragraph line at sentence ends into lines of about WRAP_TO characters. Joining the
    result (Chinese: nothing, English: the space that was there) gives back the line exactly."""
    cuts, last = [], 0
    for m in SENT_END_RE.finditer(ln):
        e = m.end()
        nxt = ln[e:].lstrip(" ")
        if BLOCK_START_RE.match(nxt):  # a line starting '1. ', '- ', '#', '>', '|' … would become another block
            continue
        if ABBREV_END_RE.search(ln[max(0, m.start() - 12):m.start() + 1]):  # 'et al.', 'Fig.': mid-sentence
            continue
        if e - last >= WRAP_TO and e < len(ln):
            cuts.append(e)
            last = e
    if not cuts:
        return [ln]
    parts, prev = [], 0
    for c in cuts:
        parts.append(ln[prev:c])
        prev = c
    parts.append(ln[prev:])
    out = [parts[0].rstrip(" ")] + [p.lstrip(" ") for p in parts[1:]]
    return out if all(out) else [ln]


def cmd_wrap(a) -> int:
    if not os.path.isdir(a.path):
        die(f"不是文件夹：{a.path}")
    n = changed = refused = 0
    for name, folder in iter_papers(a.path):
        doc = load_translation(folder)
        if not doc:
            continue
        n += 1
        if doc["fatal"] or doc["errs"]:
            print(f"[拒绝] {name}  {doc['fatal'] or 'YAML 头有问题：' + doc['errs'][0]}")
            refused += 1
            continue
        lines = doc["lines"]
        new, hits, skipped = [], [], []
        kinds = {}
        for s, e, k in md_blocks(lines):
            for x in range(s, e):
                kinds[x] = k
        for i, ln in enumerate(lines):
            if (len(ln) > WRAP_AT and kinds.get(i) == "para" and not CAPLINE_RE.match(ln)
                    and not CAPSTART_RE.match(ln) and not ANCHOR_RE.search(ln)):
                parts = wrap_line(ln)
                if len(parts) > 1:
                    hits.append((i + doc["off"], len(ln), len(parts)))
                else:  # over WRAP_AT (the spec's limit for a paragraph line) and no sentence end to break at
                    skipped.append(f"L{i + doc['off']} 段落 {len(ln)} 字（找不到合适的句末）")
                new += parts
            else:
                if len(ln) > LINE_CUT:
                    why = {"table": "表格行", "item": "列表项", "code": "代码", "heading": "标题"}.get(kinds.get(i), "图注或其他")
                    skipped.append(f"L{i + doc['off']} {why} {len(ln)} 字")
                new.append(ln)
        skip_note = ("\n    没有折行、需要人工处理的长行：" + "；".join(skipped[:8])) if skipped else ""
        if not hits:
            print(f"[OK]   {name}  没有可以自动折行的长行{skip_note}")
            continue
        sq = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
        if sq("\n".join(new)) != sq(doc["body"]):  # never change a character, only where lines break
            print(f"[拒绝] {name}  折行后文字与原文不一致（脚本缺陷，未写入），请报告")
            refused += 1
            continue
        verb = "将" if a.dry_run else "已"
        print(f"[折行] {name}  {verb}在句末折开 {len(hits)} 行：" + "、".join(f"L{l}（{c} 字 → {p} 行）" for l, c, p in hits[:6])
              + skip_note)
        if doc["normalized"]:
            print(f"    编码规范化：{'、'.join(doc['normalized'])}（写入时一并生效）")
        if a.dry_run:
            continue
        backup_once(folder, doc["path"], re.sub(r"\.md$", "", doc["name"]) + ".before-wrap.md")
        prefix = doc["text"][:len(doc["text"]) - len(doc["body"])]
        write_atomic(doc["path"], prefix + "\n".join(new))
        changed += 1
    if not n:
        die("没找到译稿", 1)
    tail = "预览，未改动文件" if a.dry_run else f"改动 {changed} 篇（改动前的原稿在 _work/backup/）"
    print(f"\n共 {n} 篇，{tail}" + (f"；拒绝处理 {refused} 篇" if refused else ""))
    return 1 if refused else 0


# ----------------------------------------------------------------------------- v3.2: pair
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，另有 {len(text) - limit} 字）"


def pair_context(folder: str, doc: dict, pages: list) -> dict:
    """Per-paper data pair_view needs, computed once however many pages are shown."""
    blocks = md_blocks(doc["lines"])
    slines = {}
    with open(source_path(folder), encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh):
            m = SRC_PAGE_RE.fullmatch(ln.strip())
            if m:
                slines.setdefault(int(m.group(1)), i + 1)
    return {"clean": dict(strip_running(pages)), "slines": slines, "blocks": blocks,
            "lp": line_pages(doc["lines"], blocks)}


def pair_view(name: str, doc: dict, ctx: dict, p: int, limit: int) -> str:
    """Source page p (running headers/footers left out) next to the translation under the <!-- pN --> anchor,
    plus the paragraph that starts just before the anchor (a paragraph that begins on the previous page and
    runs onto this one is anchored there, so its second half belongs here)."""
    src = ctx["clean"].get(p)
    if src is None:
        return ""
    sline = ctx["slines"].get(p)
    lines, off = doc["lines"], doc["off"]
    blocks, lp = ctx["blocks"], ctx["lp"]
    body = [(s, e, k) for s, e, k in blocks if k not in ("blank", "anchor") and lp[s] == p]
    # the paragraph right before this page's text (figures, captions and tables in between are floats and are
    # skipped; a heading in between means nothing runs over)
    prev = None
    first = body[0][0] if body else next((s for s, e, k in blocks if lp[s] is not None and lp[s] > p), len(lines))
    for s, e, k in reversed([b for b in blocks if b[0] < first]):
        if k in ("blank", "anchor", "image", "table") or CAPLINE_RE.match(lines[s]) or CAPSTART_RE.match(lines[s]):
            continue
        if k in ("para", "item", "quote") and lp[s] is not None and lp[s] < p:
            prev = (s, e, lp[s])
        break
    head = f"== {name} · p{p} =="
    out = [head, f"-- 原文 p{p}（_work/source.txt 第 {sline} 行起；页眉页脚已略去）--" if sline else f"-- 原文 p{p} --",
           _clip(src.strip(), limit)]
    if body:
        span = f"L{body[0][0] + off}–L{body[-1][1] - 1 + off}"
    else:
        span = "这一页的锚点下没有正文"
    out.append(f"-- 译文 p{p}（{span}）--")
    tr = []
    if prev:
        s, e, pg = prev
        tr.append(f"[L{s + off}，p{pg} 起头的段落，后半段可能属于本页] " + " ".join(lines[x].strip() for x in range(s, e)))
    for s, e, k in body:
        txt = " ".join(lines[x].strip() for x in range(s, e))
        if k == "image":
            m = IMG_RE.search(txt)
            txt = f"[图片 {m.group(1) if m else txt}]"
        tr.append(f"[L{s + off}] {txt}")
    out.append(_clip("\n".join(tr) if tr else "（无）", limit))
    return "\n".join(out)


def sample_pages(name: str, doc: dict, ctx: dict, pages: list, k: int, seed) -> list:
    """k body pages with real running text, before the reference list; reproducible for a given seed
    (default: derived from the paper's folder name, so the same pages come back every time)."""
    import random
    import zlib
    lines, blocks, lp = doc["lines"], ctx["blocks"], ctx["lp"]
    ref_page = None
    for s, e, kk in blocks:
        if kk == "heading" and REF_TITLE_RE.search(lines[s]) and lp[s]:
            ref_page = lp[s]
    cands = [p for p, t in pages if prose_chars(t) >= 600 and (ref_page is None or p < ref_page)]
    rnd = random.Random(seed if seed is not None else zlib.crc32(name.encode("utf-8")))
    return sorted(rnd.sample(cands, min(k, len(cands))))


def cmd_pair(a) -> int:
    if not os.path.exists(a.path):
        die(f"找不到：{a.path}")
    path = os.path.dirname(a.path) if os.path.isfile(a.path) else a.path
    picks = [x for x in (a.page, a.line, a.sample) if x is not None]
    if len(picks) != 1:
        die("要指定且只能指定一个：--page N、--line L 或 --sample K", 1)
    if picks[0] < 1:
        die("--page / --line / --sample 要是正整数", 1)
    shown = 0
    for name, folder in iter_papers(path):
        doc = load_translation(folder)
        pages = source_pages(folder)
        if not doc or doc["fatal"] or not pages:
            print(f"== {name} ==\n（{'没有译稿' if not doc else doc['fatal'] or '没有带分页标记的 source.txt'}）\n")
            continue
        ctx = pair_context(folder, doc, pages)
        if a.page:
            want = [a.page]
        elif a.line:
            i = a.line - doc["off"]
            lp = ctx["lp"]
            if not 0 <= i < len(lp) or lp[i] is None:
                print(f"== {name} ==\n（第 {a.line} 行不在正文页码锚点范围内）\n")
                continue
            want = [lp[i]]
        else:
            want = sample_pages(name, doc, ctx, pages, a.sample, a.seed)
        if not want:
            print(f"== {name} ==\n（没有正文足够长的页可抽）\n")
        for p in want:
            v = pair_view(name, doc, ctx, p, a.max_chars)
            if not v:
                print(f"== {name} · p{p} ==\n（原文没有第 {p} 页；共 {len(pages)} 页）\n")
                continue
            print(v + "\n")
            shown += 1
    if not shown:
        die("没有可对照的页", 1)
    return 0


# ----------------------------------------------------------------------------- v3: find
ROLE_RES = [("方法", re.compile(r"方法|材料与|实验部分|实验设计|实验方案|动物实验|methods?\b|materials|experimental|procedures?", re.I)),
            ("结果", re.compile(r"结果|results?\b|findings", re.I)),
            ("讨论", re.compile(r"讨论|discussion", re.I)),
            ("结论", re.compile(r"结论|总结|结语|conclusions?", re.I)),
            ("摘要", re.compile(r"摘要|abstract|summary", re.I)),
            ("引言", re.compile(r"引言|前言|背景|introduction|background", re.I))]
ROLE_WEIGHT = {"方法": 3, "结果": 3, "摘要": 2, "讨论": 1, "结论": 1, "": 1, "引言": 0}
CITE_RE = re.compile(r"\[\d+(?:\s*[,，–\-]\s*\d+)*\]|(?<=[A-Za-z一-鿿）)\].,，。])[¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3}(?:[,，–-][¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3})*"
                     r"|et al\.?,?\s*\(?\d{4}|等[，,]\s*\d{4}")
NEG_ZH_RE = re.compile(r"(非|未|无|不|没有)\S{0,2}$")
NEG_EN_RE = re.compile(r"\b(non|not|without|no|un)[\s-]?\w{0,4}[\s-]?$", re.I)


def _src_blocks(slines: list) -> tuple:
    """Blocks of source.txt as (first_line_index, text, first_line), plus the page of every line.
    A <<<PAGE n>>> marker ends the block before it and is never part of a block (the first paragraph of
    every page is searchable)."""
    out, cur, first, page, spage = [], [], None, None, []

    def flush():
        if cur:
            out.append((first, " ".join(cur), cur[0]))
            cur.clear()

    for i, ln in enumerate(slines):
        m = SRC_PAGE_RE.fullmatch(ln.strip())
        if m:
            flush()
            page = int(m.group(1))
            spage.append(page)
            continue
        spage.append(page)
        if not ln.strip():
            flush()
            continue
        if not cur:
            first = i
        cur.append(ln.strip())
    flush()
    return out, spage


def _snippet(text: str, rx, width: int, ands: list | None = None) -> str:
    """Window around the main match; with --and terms, the closest main/and pair is shown together
    (two short windows joined by ' … ' when they are far apart), so the reader sees why it matched."""
    t = re.sub(r"\*\*|`|<!--.*?-->", "", text)  # underscores stay: they are part of names like in_vivo
    t = re.sub(r"\s+", " ", t).strip()
    mains = [m.span() for m in rx.finditer(t)] or [(0, 0)]
    others = [m.span() for r in (ands or []) for m in r.finditer(t)]
    if others:
        a, b = min(((x, y) for x in mains for y in others), key=lambda p: abs(p[0][0] - p[1][0]))
        lo, hi = min(a[0], b[0]), max(a[1], b[1])
        if hi - lo <= width - 10:
            s = max(0, lo - (width - (hi - lo)) // 2)
            e = min(len(t), s + width)
            return ("…" if s else "") + t[s:e] + ("…" if e < len(t) else "")
        h = width // 2
        parts = []
        for sp in sorted([a, b]):
            s = max(0, sp[0] - h // 3)
            parts.append(t[s:min(len(t), s + h)])
        return "…" + " … ".join(parts) + "…"
    s = max(0, mains[0][0] - width // 3)
    e = min(len(t), s + width)
    return ("…" if s else "") + t[s:e] + ("…" if e < len(t) else "")


def _labels(text: str, rxs: list) -> list:
    lab = []
    if CITE_RE.search(text):
        lab.append("[引]")
    for rx in rxs:
        for m in rx.finditer(text):
            before = text[max(0, m.start() - 12):m.start()]
            if NEG_ZH_RE.search(before) or NEG_EN_RE.search(before):
                lab.append("[否]")
                break
        if "[否]" in lab:
            break
    return lab


def _units_for_scope(blocks: list, scope: str, group_of) -> list:
    """Group blocks into the units within which --and / --not apply ('section' groups by group_of)."""
    if scope == "para":
        return [[b] for b in blocks]
    if scope == "paper":
        return [blocks] if blocks else []
    groups = defaultdict(list)
    for b in blocks:
        groups[group_of(b[0])].append(b)
    return list(groups.values())


def find_in_paper(name: str, folder: str, a, rx, ands: list, nots: list) -> dict:
    res = {"paper": name, "zh": [], "src": [], "cards": [], "n": {"zh": 0, "src": 0, "cards": 0}, "problems": []}
    allrx = [rx] + ands

    def qualifies(text):
        return rx.search(text) and all(r.search(text) for r in ands) and not any(r.search(text) for r in nots)

    def collect(units, mk):
        hits = []
        for u in units:
            if qualifies(" ".join(t for _, t in u)):
                hits += [mk(i, t) for i, t in u if rx.search(t)]
        return hits

    doc = load_translation(folder)
    if doc and doc["fatal"] and ("zh" in a.sources or "cards" in a.sources):
        res["problems"].append("译稿无法读取（不是 UTF-8）")
        doc = None
    if doc is None and ("zh" in a.sources or "cards" in a.sources) and not res["problems"]:
        res["problems"].append("没有译稿")
    if doc and "zh" in a.sources:
        lines, off = doc["lines"], doc["off"]
        mb = md_blocks(lines)
        heads = [(s, len(m.group(1)), m.group(2)) for s, e, k in mb if k == "heading" for m in [HEAD_RE.match(lines[s])]]
        ref_i = next((i for i, lv, t in heads if lv <= 3 and REF_TITLE_RE.search(t)), None)
        stop = ref_i if (ref_i is not None and not a.refs) else len(lines)
        lp = line_pages(lines, mb)

        def sect_of(i):
            h = [x for x in heads if x[0] <= i]
            return h[-1][0] if h else -1

        def role_of(i):
            chain = []
            for hi, lv, t in reversed([x for x in heads if x[0] <= i]):
                if not chain or lv < chain[-1][0]:
                    chain.append((lv, t))
            for _, t in chain:
                for role, r in ROLE_RES:
                    if r.search(t):
                        return role
            return ""

        # searchable: paragraphs (soft-wrapped lines joined), list items, quotes, tables; captions are paragraphs
        blocks = [(s, " ".join(lines[x].strip() for x in range(s, e))) for s, e, k in mb
                  if s < stop and k in ("para", "item", "quote", "table")]

        def mk(i, t):
            role = role_of(i)
            lab = _labels(t, allrx)
            score = ROLE_WEIGHT.get(role, 1) - 2 * ("[引]" in lab) - 2 * ("[否]" in lab)
            return {"line": i + off, "page": lp[i], "role": role, "labels": lab, "score": score,
                    "text": _snippet(t, rx, a.width, ands)}
        res["zh"] = collect(_units_for_scope(blocks, a.scope, sect_of), mk)
    if "src" in a.sources:
        sp = source_path(folder)
        if not sp:
            res["problems"].append("没有原文 source.txt")
        else:
            slines = open(sp, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").split("\n")
            sblocks, spage = _src_blocks(slines)
            rk = running_keys((spage[i], t) for i, t, _ in sblocks)  # journal lines, URLs, 'Page 3 of 9'
            sblocks = [b for b in sblocks if not is_running(b[1], rk)]
            if not a.refs:  # stop at the last reference-list heading of the source, if recognisable
                cuts = [i for i, _, f in sblocks if re.fullmatch(r"(?:\d+\.?\s*)?(references?|bibliography|参考文献)\s*:?", f, re.I)]
                if cuts:
                    sblocks = [b for b in sblocks if b[0] < cuts[-1]]
            blocks = [(i, t) for i, t, _ in sblocks]

            def mk2(i, t):
                lab = _labels(t, allrx)
                return {"line": i + 1, "page": spage[i], "role": "", "labels": lab,
                        "score": 1 - 2 * ("[引]" in lab) - 2 * ("[否]" in lab), "text": _snippet(t, rx, a.width, ands)}
            # the source has no reliable section headings: 'section' scope means the same page there
            res["src"] = collect(_units_for_scope(blocks, a.scope, lambda i: spage[i]), mk2)
    if doc and "cards" in a.sources and doc["fields"]:
        units = []
        for k, v in doc["fields"].items():
            entries = [e.strip() for e in ENTRY_SPLIT_RE.split(str(v)) if e.strip()] or [str(v)]
            if a.scope == "para":
                units += [[(k, e)] for e in entries]  # numbered entries are matched one by one
            else:
                units.append([(k, e) for e in entries])  # section = the whole field
        if a.scope == "paper":
            units = [[x for u in units for x in u]]
        res["cards"] = [{"field": k, "labels": [], "score": 2, "text": _snippet(f"{k}: {e}", rx, a.width, ands)}
                        for u in units if qualifies(" ".join(e for _, e in u)) for k, e in u if rx.search(e)]
    for s in ("zh", "src", "cards"):
        res["n"][s] = len(res[s])
    return res


def find_glossary(root: str) -> str | None:
    """_glossary.md in the library root (or up to three folders above it)."""
    d = os.path.abspath(root)
    for _ in range(4):
        p = os.path.join(d, "_glossary.md")
        if os.path.isfile(p):
            return p
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def glossary_groups(path: str) -> list:
    """Term groups from _glossary.md: each line / table row `original | translation` is one group; a cell may hold
    variants (a / b、c) and parenthesised forms (骨髓炎（osteomyelitis）) – all become members of the group."""
    groups = []
    for ln in open(path, encoding="utf-8", errors="replace").read().splitlines():
        if "|" not in ln or re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", ln):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|") if c.strip()]
        if len(cells) < 2 or any(re.fullmatch(r"(英文|原文|中文|译名|统一中文.*|english|original|term|translation)", c, re.I)
                                 for c in cells[:2]):
            continue
        terms = set()
        for c in cells[:2]:
            for part in re.split(r"\s*[/、;；]\s*", c):
                inner = re.findall(r"[（(]([^）)]+)[）)]", part)
                main = re.sub(r"[（(][^）)]*[）)]", "", part).strip(" *`")
                for t in [main] + [x.strip(" *`") for i in inner for x in re.split(r"[,，]", i)]:
                    if len(t) >= 2 and not t.isdigit():
                        terms.add(t)
        if len(terms) >= 2:
            groups.append(terms)
    return groups


def expand_with_glossary(pattern: str, groups: list, flags: int) -> tuple:
    """Add the glossary counterparts of every plain alternative in pattern (颅骨 → calvaria, skull …).
    Only plain alternatives are expanded (no regex syntax inside); returns (new_pattern, added_terms)."""
    alts = pattern.split("|")
    if not groups or any(re.search(r"[\\()\[\]{}*+?^$.]", x) for x in alts):
        return pattern, []
    rx = re.compile(pattern, flags)
    added = []
    for a in (x.strip() for x in alts if x.strip()):
        al = a.lower()
        for g in groups:
            # equal, or the term starts with the word typed (Latin stem of 5+ letters, CJK word of 2+ characters)
            stem = re.fullmatch(r"[A-Za-z\-]{5,}", a) or re.fullmatch(f"[{CJK}]{{2,}}", a)
            hit = any(t.lower() == al or (stem and t.lower().startswith(al)) for t in g)
            if not hit:
                continue
            for t in sorted(g):
                if not rx.search(t) and t not in added:
                    added.append(t)
    if not added:
        return pattern, []
    return pattern + "|" + "|".join(re.escape(t) for t in added), added


def cmd_find(a) -> int:
    if not os.path.isdir(a.root):
        die(f"不是文件夹：{a.root}")
    flags = 0 if a.case else re.I
    try:
        re.compile(a.pattern, flags)
        for p in a.and_ + a.not_:
            re.compile(p, flags)
    except re.error as exc:
        die(f"检索式不是合法的正则表达式：{exc}")
    expanded, orig_pat, orig_ands = [], a.pattern, list(a.and_)
    gl = None if a.no_expand else find_glossary(a.root)
    if gl:
        groups = glossary_groups(gl)
        a.pattern, add = expand_with_glossary(a.pattern, groups, flags)
        expanded += add
        new_ands = []
        for p in a.and_:
            p2, add = expand_with_glossary(p, groups, flags)
            new_ands.append(p2)
            expanded += add
        a.and_ = new_ands
    rx = re.compile(a.pattern, flags)
    ands = [re.compile(p, flags) for p in a.and_]
    nots = [re.compile(p, flags) for p in a.not_]
    if a.brief:
        a.per_paper, a.width = 1, min(a.width, 60)
    a.sources = {s.strip() for s in a.in_.split(",") if s.strip()}
    bad = a.sources - {"zh", "src", "cards"}
    if bad or not a.sources:
        die(f"--in 只能是 zh、src、cards 的组合（逗号分隔），收到：{a.in_}")
    papers = list(iter_papers(a.root))
    if not papers:
        die("没找到文献文件夹", 1)
    results = [find_in_paper(n, f, a, rx, ands, nots) for n, f in papers]
    hit = [r for r in results if any(r["n"].values())]
    miss = [r["paper"] for r in results if not any(r["n"].values())]
    incomplete = {r["paper"]: r["problems"] for r in results if r["problems"]}
    scope_txt = {"para": "同一段", "section": "同一节（原文按同一页）", "paper": "同一篇"}[a.scope]
    desc = f"检索：{orig_pat}" + "".join(f"  且 {p}" for p in orig_ands) + "".join(f"  非 {p}" for p in a.not_)
    desc += f"  ·范围：{scope_txt}  ·来源：{','.join(sorted(a.sources))}"
    if a.json:
        print(json.dumps({"query": desc, "expanded": expanded, "hit": hit, "miss": miss, "total": len(results),
                          "incomplete": incomplete},
                         ensure_ascii=False, indent=1))
        return 0
    print(desc)
    if expanded:
        print(f"术语表扩展（{os.path.basename(gl)}，--no-expand 关闭）：+" + " +".join(expanded[:20])
              + (" …" if len(expanded) > 20 else ""))

    def report_incomplete():
        if incomplete:
            print(f"未完整检索 {len(incomplete)} 篇（结论不能覆盖这些篇）："
                  + "；".join(f"{k}：{'、'.join(v)}" for k, v in list(incomplete.items())[:20]))

    if not hit:
        print(f"NO MATCH（本库 {len(results)} 篇中都没有命中；换同义词、英文词干或缩写再试）")
        report_incomplete()
        return 0
    for r in hit:
        allh = r["zh"] + r["src"] + r["cards"]
        r["best"] = max(h["score"] for h in allh)
    hit.sort(key=lambda r: (-r["best"], -sum(r["n"].values()), r["paper"]))
    for r in hit:
        cnt = " · ".join(f"{ {'zh': '译文', 'src': '原文', 'cards': '卡片'}[s] } {r['n'][s]}" for s in ("zh", "src", "cards")
                         if s in a.sources)
        show = sorted(r["zh"], key=lambda h: (-h["score"], h["line"]))[:a.per_paper]
        if not show:
            show = sorted(r["src"], key=lambda h: (-h["score"], h["line"]))[:a.per_paper]
            pre = "src "
        else:
            pre = ""
        show = show or r["cards"][:a.per_paper]
        rows = []
        for h in show:
            if "field" in h:
                rows.append(f"卡片  {h['text']}")
                continue
            pg = f" p{h['page']}" if h.get("page") else ""
            tag = " ".join(x for x in [h["role"]] + h["labels"] if x)
            rows.append(f"{pre}L{h['line']}{pg}{' ' + tag if tag else ''}  {h['text']}")
        if a.brief:  # one line per paper: the cheap first pass for "which papers…" questions
            print(f"== {r['paper']}  {cnt} | {rows[0] if rows else ''}")
            continue
        print(f"\n== {r['paper']}  {cnt}")
        for row in rows:
            print(f"   {row}")
    print(f"\n命中 {len(hit)} 篇 / 未命中 {len(miss)} 篇（共 {len(results)} 篇）")
    if miss:
        print("未命中：" + ("、".join(miss) if len(miss) <= 40 else f"{len(miss)} 篇（--json 可看全部）"))
    if {"zh", "src"} <= a.sources:
        only_src = [r["paper"] for r in hit if r["n"]["src"] and not r["n"]["zh"]]
        only_zh = [r["paper"] for r in hit if r["n"]["zh"] and not r["n"]["src"]]
        if only_src:
            print("仅原文命中（译名可能不同，或译文漏了这句）：" + "、".join(only_src))
        if only_zh:
            print("仅译文命中（原文用词不同，可补英文同义词再查）：" + "、".join(only_zh))
    report_incomplete()
    print("标签：[引] 段内有引用标记，可能在转述别人的工作；[否] 命中词前有否定词。按标签排序，未做过滤。")
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

    p = sub.add_parser("trim", help="紧裁白边并限制像素（默认原地覆盖，首次覆盖前把原图备份到 .orig/）")
    p.add_argument("images", nargs="+"); budget(p)
    p.add_argument("--out", help="输出文件；多张图时为输出目录（不改动原图）")
    p.add_argument("--no-backup", action="store_true", help="原地覆盖时不备份原图")
    p.add_argument("--pad", type=int, default=8)
    p.set_defaults(fn=cmd_trim)

    p = sub.add_parser("brief", help="速览：目录（行号/页）+ 摘要 + 图表注 + 结论")
    p.add_argument("path", help="文献文件夹、译稿文件或工作区根目录")
    p.add_argument("--cap-chars", type=int, default=0, help="每条图表注最多保留的字符数（0 = 全文）")
    p.add_argument("--no-conclusion", action="store_true")
    p.add_argument("--toc-only", action="store_true", help="只输出卡片信息和目录（约 500 token）")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("anchor", help="按原文复核页码锚点：证据确凿才改；没有锚点时整篇自动插入")
    p.add_argument("path", help="文献文件夹或工作区根目录")
    p.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    p.add_argument("--auto", action="store_true", help="不参考已有锚点，整篇按原文重新推定")
    p.add_argument("--show", type=int, default=12, help="每篇最多列出多少条改动")
    p.set_defaults(fn=cmd_anchor)

    p = sub.add_parser("find", help="全库检索：译文 + 原文 + 卡片，按篇聚合")
    p.add_argument("root", help="工作区根目录（或单篇文件夹）")
    p.add_argument("pattern", help="正则，忽略大小写；中英文和缩写都写上，如 \"颅骨|calvari|skull\"")
    p.add_argument("--and", dest="and_", action="append", default=[], help="同一范围内还必须命中（可多次）")
    p.add_argument("--not", dest="not_", action="append", default=[], help="同一范围内不能出现（可多次）")
    p.add_argument("--scope", choices=["para", "section", "paper"], default="para", help="--and/--not 的作用范围（默认同一段）")
    p.add_argument("--in", dest="in_", default="zh,src", help="来源：zh（译文）、src（原文）、cards（卡片字段），逗号分隔")
    p.add_argument("--per-paper", type=int, default=2, help="每篇显示几条片段")
    p.add_argument("--brief", action="store_true", help="每篇一行（计数 + 最相关的一条短片段），查全类问题先用它")
    p.add_argument("--no-expand", action="store_true", help="不按工作区 _glossary.md 自动补同义词")
    p.add_argument("--width", type=int, default=80, help="片段字数")
    p.add_argument("--refs", action="store_true", help="也查参考文献列表")
    p.add_argument("--case", action="store_true", help="区分大小写")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("wrap", help="把超过 1500 字的行在句末折开（只加换行，不改字；改前备份）")
    p.add_argument("path", help="文献文件夹或工作区根目录")
    p.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    p.set_defaults(fn=cmd_wrap)

    p = sub.add_parser("pair", help="原文页与译文页并排输出，供抽检或核对某条 WARN")
    p.add_argument("path", help="文献文件夹（--sample 时也可以是工作区根目录）")
    p.add_argument("--page", type=int, help="原文页码")
    p.add_argument("--line", type=int, help="译稿行号（check 报的 L 号），自动定位到它所在的页")
    p.add_argument("--sample", type=int, help="每篇随机抽 K 页正文（不含参考文献页），同一篇每次抽到同样的页")
    p.add_argument("--seed", type=int, help="换一批抽样页")
    p.add_argument("--max-chars", type=int, default=9000, help="原文、译文各最多输出多少字（默认 9000）")
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("check", help="校验译稿")
    p.add_argument("path"); budget(p)
    p.add_argument("--lang", default="zh", help="译稿未声明 target_lang 时的默认目标语言")
    p.add_argument("--strict", action="store_true", help="WARN 也算不通过")
    p.add_argument("-q", "--quiet", action="store_true", help="只显示有问题的文献")
    p.add_argument("--log", help="报告另存到此文件")
    p.add_argument("--json", action="store_true", help="结构化输出：每篇的 FAIL/WARN（message + details）和总计数")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("cards", help="汇总 cards.jsonl；--list 只打印紧凑表")
    p.add_argument("root"); p.add_argument("--md", action="store_true"); p.add_argument("--out")
    p.add_argument("--list", action="store_true", help="每篇一行打印（文件夹 | 年 | 类型 | 一句话），不写文件")
    p.add_argument("--with", dest="with_", help="--list 时附带的字段，逗号分隔，如 in_vivo,key_result")
    p.set_defaults(fn=cmd_cards)

    p = sub.add_parser("refresh", help="按 manifest.json 记录的 PDF 重新提取旧版本生成的 _work（不碰译稿和图片）")
    p.add_argument("root", help="文献文件夹或工作区根目录")
    p.add_argument("--dry-run", action="store_true", help="只列出会重提哪些篇")
    p.add_argument("--pdf-dir", help="PDF 搬过家时，按文件名到这个文件夹里找")
    p.add_argument("--force", action="store_true", help="已是当前版本提取的也重提")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("selftest", help="运行随包的回归测试（合成样本，不读写你的文件）；升级后跑一次")
    p.set_defaults(fn=cmd_selftest)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
