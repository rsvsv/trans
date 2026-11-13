# Changelog

## v3.2.1

Fixes from a second code review of v3.2 and from a test of the first fix build on the 38-paper library.
Regression checks: 112 (`pdftrans selftest`; v3.2 shipped with 91).

### Fixed

- `refresh`: a PDF that `extract` cannot read (encrypted, damaged, no text layer) no longer stops the batch or
  overwrites `source.txt`; it is reported as `[失败]` with extract's own warning, the next paper is processed,
  and the exit code is 1. Extract warnings (scanned pages) are shown under `[重提]`.
- Number check, fewer false passes: decimal commas are read only before a unit or ± (`[1,2]`, `refs 3,4` stay
  lists); `gram-` + a word after a number (`Two gram-variable`, `5 Gram-negative`) is the Gram stain, not grams,
  replacing the positive/negative/stain/scale word list (other units keep their hyphenated adjective:
  `5 micron-thick`, `5- micron-thick`, `Four-micrometer-thick` are 5 μm / 5 μm / 4 μm); `10−3 mM`
  and `1 × 10−3 mg` (true minus sign) are exponents, not `3 mM`/`3 mg` (`10-30 mM` with a hyphen is still a range).
  In the source only a true minus sign blocks a number, so `top-2 mm, bottom-500 μm` and `CaO-5% P2O5` keep
  their quantities (the first fix build also blocked the ASCII hyphen there and produced two false caption /
  number warnings on the library).
- Running lines: the 30% rule applies to a line with a changing number (page number), a year, a URL or
  "et al."; a line with no digits needs 50%, as in v3.2; a line whose digits never change (`Day 7 group A`)
  needs 70% 〔自订阈值〕, so a repeated figure label stays visible in `pair` and `find`. (Before, any digit made a
  line "typical", and the first fix build raised digit-free footers such as `Paper RSC Advances` to 70% too.)
- `wrap` does not break after abbreviations (`et al.`, `Fig.`, `e.g.`, `vs.` …), and the hard-wrap check no
  longer treats a line ending in one as a finished sentence.
- `wrap` lists every paragraph line over 1,500 characters it could not break (before, only those over 2,000;
  `LONG_LINE` was defined twice, 1,500 and 2,000 — the 2,000 reader limit is now `LINE_CUT`).
- The source snippet next to a number warning compares units by what they measure (mM with μM, not with mm).
- `tests/test_pdftrans.py` ignores options (`-q`) and says so when the pdftrans.py path it is given does not exist.

### Changed

- `VERSION` 3.2.1: `refresh` without `--force` re-extracts `_work/` made by 3.2.
- Internal: one backup helper for `anchor`/`wrap`/`refresh`; per-paper work in `pair` done once, not per page;
  source snippets built only for the rows shown.

Measured on an 18-paper copy of a library (实测, this build against v3.2): `check`, `check --json`,
`pair --sample 3`, `find --in src`, `wrap --dry-run` and `anchor --dry-run` print byte-identical output.
The (number, unit) pairs read from the sources: none added, 3 dropped, all exponents v3.2 misread
(`3.3 × 10−3 MPa` as 3 MPa, `10−8 mol/L` as 8 mol, `mL−1, 30 min` as 1 min).

## v3.2

From hands-on feedback on v3.0/v3.1 (the 38-paper, 851-page library). Every change measured read-only on a copy
of that library; the library itself was not modified. Regression tests now ship inside the package
(`tests/test_pdftrans.py`, 91 checks on synthetic fixtures, run with `pdftrans selftest`). An independent code
review of the change found 12 issues (a missing last page hidden by the new thin-page credit, `Gram-positive`
read as grams, `wrap` able to start a line with `1. `, a value column leaking across tables, among others);
all are fixed and the serious ones have tests.

### Added

- `pair DIR --page N | --line L | --sample K`: a source page (running headers/footers removed) next to the
  translation under its anchor, plus the paragraph that starts on the previous page and may run onto this one.
  `--sample` picks body pages before the reference list, the same ones every time for a given paper (`--seed`
  for another set). Writes nothing.
- `SKILL.md` step 6, **spot check**: two random pages per paper, plus any page flagged as thin, go to a reviewer
  that did not translate the paper, with a fixed brief (omissions, mistranslations, additions, numbers; known
  pitfalls of the page view spelled out). Blind test: 4 errors that `check` cannot see (two groups' values
  swapped, a sentence dropped, a verb reversed, an invented clause) hidden in 6 pages from 6 papers — the
  reviewer found all 4, no false alarm on the 3 clean pages; its two "uncertain" notes were real (a term
  silently "corrected" by the translator, lost superscripts). About 5–6k tokens per page. Reported as a sample.
- `check`: every number and caption finding is listed on its own line with the translation context; unit
  mismatches also show the nearest occurrence of that number in the source, preferring the same page and the
  same kind of unit (`L83 「250 μm」 …150–250 μm…  ｜原文 p3：…(150–250 mm)…`).
- `check --json`: the same report as structured data (message + details per finding, totals).
- `check`: `key_result` entries without `[pN]` get a suggested page from where their numbers occur
  (23 of the 38 such warnings on the library).
- `wrap PATH [--dry-run]`: lines over 1,500 characters are broken at sentence ends, never so that a line would
  start like a list item, heading, quote or table; the text without line breaks is verified identical before
  writing, the original goes to `_work/backup/`, a second run changes nothing; long lines it cannot break
  (tables, list items, one endless sentence) are listed for manual handling.
  On three library copies: 13 lines wrapped, `check` no longer reports long lines, `anchor` unchanged.
- `_fields.md` may have a third column 「取值」 (allowed values) for categorical fields; `check` reports values
  outside it (`SD 大鼠` when the list says `大鼠 / 小鼠 / 兔`), 「无」/「未报告」 always allowed, a bracketed note
  after a value is fine. Synonyms for search go to `_glossary.md` (already expanded by `find`), not to a new
  alias column. Numbered (①②) fields are not value-checked.
- `refresh ROOT [--dry-run] [--pdf-dir DIR]`: re-extracts `_work/` made by an older pdftrans from the PDF path
  recorded in `manifest.json` (or the file of that name in a folder the user names, when the PDFs have moved).
  Only `source.txt`, `refs.md` and `manifest.json` are rewritten (old ones to `_work/backup/`, recorded figures
  kept); a paper whose page count changed is refused. All 38 library papers had been extracted by pdftrans 2.0,
  before symbol-font Greek was restored. On a copy: 36 refreshed (2 PDFs live elsewhere), translations
  byte-identical, `anchor` still reports every paper consistent, and the check got sharper in both directions:
  false unit alarms caused by the old extraction disappeared (`100 μg` ascorbic acid, `5 μm` sections, `150–250 μm`
  particles — the PDF prints μ in a symbol font) while a real error the old extraction had hidden appeared
  (translation `2 mL` of water for a contact-angle drop; the PDF says `2 μL` — the translator had copied the
  extraction error).
- `selftest` subcommand; `install.py` now ships `tests/`.

### Changed

- Running headers and footers (journal line, URL, `Page 3 of 9`, `Wang et al.`) are recognised in the source:
  short paragraphs that recur on ≥3 pages and ≥30% of the pages once digits are masked (≥50% when the line
  has no number, URL or "et al.", so a repeated table header is not mistaken for one). `find` (source side)
  and `pair` skip them; `source.txt` keeps them, because the volume, article number and DOI in them are what
  the number check matches the header block of a translation against. On the library: noise hits for
  `"RSC Advances|www\."` 279 → 38 (the rest are first-page mastheads and data-availability URLs, which are
  content), `"Nature Communications"` 57 → 3; content searches unchanged (`osteomyelitis` 70, `calvari` 210,
  `MRSA` 52, `S\. aureus` 258 hits before and after). By characters the running lines are 1.4% of the source,
  not the 5–7% estimated from line counts.
- Number check, fewer false alarms from the way the source is written: spelled-out numbers and unit words
  (`One milliliter` = 1 mL, `Four-micrometer-thick` = 4 μm, `Twenty micrograms` = 20 μg, `5 minutes` = 5 min),
  decimal commas (`0,1 mg`), `No.30013`, a lost space (`into5 mL`); and an exponent is no longer read as a
  quantity (`mg L⁻¹ min⁻¹` is not "1 min"). These only add candidates to the source side. On the library:
  number + unit findings 17 → 6, numbers absent from the source 5 → 4; every one left was checked and is real
  or worth a look (two `104` for 10⁴, a translator's note, an equation printed as an image, the μ/m cases above).
- Captions: quantities added after 标尺 / scale bar are tagged `[标尺]` in the same warning (they are read off
  the figure, which the spec forbids; delete them). Not suppressed.
- "Translation looks thin" no longer fires for a page that only continues a paragraph across a figure page:
  the numbers found only on that source page sitting in the translation just before it (or, for pages without
  numbers, enough translation for that page and the two before, measured against the translation density of the
  pages that are not suspect) mean the text is there. When it still fires it
  says why: 「该页独有的 N 个数字译文里只有 M 个」 (omission) or 「没有足够的数字可比对」 (look with `pair`).
  The three pages reported on the Janus sonozyme paper were continuation pages, not figure-only pages; all
  three are now credited. Omission test: 114 pages deleted one at a time across 38 papers — v3.1 reported
  110, v3.2 reports 109.
- `SKILL.md`: WARN table updated for the context lines; `wrap` for long lines; `pair --line` for checking a
  key number when answering from the library; `wrap` joins `anchor` among commands that rewrite translations
  and are run on a library only when the user asks.

Not done, with reasons: removing running lines from `source.txt` at extraction (they carry the article
metadata the number check needs; filtering where text is read gives the same search result without rewriting
38 existing files); lowering the weight of numbers next to "scale/μm/mm" (on the library those were scale
bars and figure read-outs, which the spec forbids — the check is right to report them); `extract --all`
guessing PDF locations (rejected in v2.2; `refresh` uses the recorded path or a folder the user names); an alias column in `_fields.md` (the glossary already does this); discipline templates
in the package (they belong to the user's workspace).

## v3.1

Sharper checks and cheaper search, measured read-only on the 38-paper library (the library itself was not
changed by these measurements) and covered by 48 regression checks in `tests/test_v3.py`.

### Added

- `check`: **number + unit and value ± sd** are compared with the source, not just bare numbers. `20 mg/mL`
  when the source says `20 μg/mL`, or `1.50 ± 0.05` when the source pairs 1.50 with 0.02, are reported; small
  integers with a unit are checked this way too. Units in lists and ranges (`6 and 24 h`, `20 to 50 °C`,
  `12-h`, `565/578 nm`) and the many ways a degree sign or micro sign is extracted (`37 ◦C`, `µm`) do not cause
  false alarms. On the library: 8 papers flagged; the cases sampled were real (a concentration absent from the
  source, values read off a figure) or source-extraction losses worth a look at the page (μ printed, `mm`
  extracted). `106 CFU` (lost superscript, no `×`) is now caught with the other powers of ten.
- `check`: **captions compared with the original caption of the same number** (manifest caption list, completed
  from `source.txt` up to the next caption on the page): quantities the original caption does not have (scale
  bars, p-values, schematic labels read off the figure — the spec forbids adding them), quantities of the
  original missing from the translation (a dropped half-sentence), and captions whose distinctive numbers all
  belong to another figure (swapped). Captions split into parts (`图 4a、b` / `图 4c、d`) are compared as one;
  `**补充文件 1：图 S1**` is matched with Figure S1. On the library: 15 papers with added quantities (mostly
  scale bars and schematic details), 0 swapped and 0 missing-half-sentence alarms after fixing the false ones
  found on the way; a deliberately swapped pair of captions is caught in the test suite.
- `find --brief`: one line per paper (counts + the best short snippet). On the benchmark question the output
  went from 15.3k to 8.1k characters.
- `find` expands plain search words from the workspace `_glossary.md` (the same file used for consistent
  terminology): `骨髓炎` also searches `osteomyelitis`, `颅骨` reaches the `颅骨缺损 | calvarial defect` row. The
  added terms are printed; patterns that use regex syntax are not expanded; `--no-expand` turns it off. Tested
  with the user's 132-row glossary on a library copy: Chinese-only searches of the English source went from no
  hits to 10–26 papers.

### Changed

- `SKILL.md`: legacy translations are upgraded (`anchor`, headers) **only when the user explicitly asks**;
  reading, searching and answering never rewrite files. (An agent had run `anchor` on a whole existing library
  on its own initiative after v3.0 was installed.)
- WARN table, `find` guidance (`--brief` first for exhaustive questions, glossary expansion) and the caption
  rule in `output-spec.md` updated.

Tried and not shipped: a paragraph-level omission check. Comparing each source paragraph with the whole
translation caught 1 of 34 deliberately deleted paragraphs; aligning paragraphs by position caught 9 of 12 but
flagged about a third of correctly translated ones (source paragraphs are split and merged by columns and page
breaks). The page-level "translation looks thin" check stays.

## v3.0

A retrieval layer: translations produced from now on are easy to search, cite to the page, and check
against the source. Designed from an external proposal; measured on a 38-paper library (copies only), two
sample translations, one full end-to-end translation, and a regression suite (`tests/test_v3.py`, 35 checks,
kept outside the skill package). Numbers below are from that test set, not guarantees.

### Added

- `anchor PATH`: checks every page anchor against `_work/source.txt` using what survives translation
  (numbers, Latin words, figure numbers). The translator's pages are kept; a paragraph is moved to the
  neighbouring page only when that page holds several of its tokens the translator's page lacks, the overall
  evidence clearly favours it, **and the opening of the paragraph points there too** — a paragraph that
  starts on one page and runs onto the next stays on the page it starts on. A monotone repair then moves the
  fewest other paragraphs. Anchors are inserted only between blocks (never inside a soft-wrapped paragraph,
  table, list item, quote or code block; anchors inside code are left alone). Duplicate, out-of-range,
  missing or inline anchors are rebuilt as one per page (`[整理]`). Files that are not UTF-8 or have a broken
  YAML header are refused (exit 1) and never rewritten; writes are atomic; the first pre-change version is
  kept in `_work/backup/`; BOM/CRLF normalisation is reported. On the two sample translations it made 16
  corrections (1 + 15), each checked against the source; a second run reports OK and changes nothing. Without
  anchors it places them from scratch: 96–97% of paragraphs on the same page as the checked version, all
  within ±1 (two papers, 148 paragraphs). On 38 library copies: under 1.2 s, text identical apart from anchors.
- `find ROOT PATTERN [--and] [--not] [--scope para|section|paper] [--in zh,src,cards]`: searches the
  translations and `source.txt` together, grouped by paper, each hit with file line and page (and section role
  for translation hits); `[引]` / `[否]` labels rank but never filter. Reports hit / missed papers, papers matched
  only in the source or only in the translation, and papers that could not be fully searched (no translation,
  no `source.txt`, not UTF-8). `section` scope means the same section in the translation and the same page in
  the source. Card fields written as numbered entries (①②) are matched entry by entry. On the question "which
  papers used an in-vivo infected calvarial model" it returned 32 of 38 papers, including all 15 provisional
  positives (a manually assembled list, not an exhaustively adjudicated gold standard); the false positives
  (in-vitro infection + uninfected model, citations, reviews) are why every candidate must be judged on its
  snippet.
- `cards --list [--with F1,F2]`: one line per paper on stdout, nothing written.
- `brief --toc-only`: card facts and outline only.
- Workspace field table `_fields.md`: discipline fields declared per library. `check` verifies each is filled
  («无» / «未报告» count as filled) and, when the description asks for numbered entries with `‖ label：`, that
  every entry has each label (reported per label). Blind test with 8 papers including 4 traps: the yes/no
  answer to the target question was right for all 8 after switching to one numbered record per model; some
  entries still contained details inferred from neighbouring text, hence the new "do not infer" rule.
- `check` new warnings: numbers absent from the source (decimals and integers of 3+ digits only; 1–2 digit
  integers are not checked; URLs, DOIs and citation markers skipped), powers of ten that lost their superscript
  (`1 × 106`), `key_result` entries without `[pN]` or whose numbers are not on that page (±1), missing or
  malformed workspace fields, anchors that disagree with the source, duplicate / inline anchors (page count
  from `manifest.json` or `source.txt`), HTML comments other than anchors (e.g. the `refs.md` header),
  pages whose running text looks untranslated (tables count; translator-placed anchors only), lines over 2,000
  characters, and an explicit warning when `source.txt` is missing or unpaged so nothing was verified. These
  checks show that a number **occurs** in the original, not that it belongs to the right group or unit.
  On the 38-paper library FAIL counts are unchanged from v2.2; the extra warnings are the new standard applied
  to old translations (no `[pN]` in key results, numbers to review).

### Changed

- Spec: card header written after the full text; `keywords` (omitted when the original has none);
  `key_result` entries carry `[pN]`; entity names bilingual; figures go right after the paragraph that first
  cites them; paragraphs over 1,500 characters wrapped at sentence ends without a blank line; citations as
  `[n]`; exponents always superscript; adjacent anchors with no text between them are normal; the `refs.md`
  header comment is removed. `source.txt`, `refs.md` and `manifest.json` must be kept.
- `SKILL.md`: step 4 (`anchor`), WARN handling table, four-layer reading protocol; "which papers did X"
  questions pool candidates from the workspace field and `find` and judge each on its snippet (agreement of
  the two routes is not verification); wording for zero hits and for incompletely searched papers.

Known limits: a cross-page paragraph that an earlier run already moved to its continuation page is not moved
back (the check is deliberately conservative); in very short documents (2–3 pages) the evidence rarely reaches
the threshold, so anchors there are mostly left as written; thousands separators written with a full-width
comma are not normalised.

Not done (from the proposal): a query DSL with enums and alias tables; a separate `facts` command (folded
into `check`, checking against the source rather than the translation); multiple profiles in one library.

## v2.2

From hands-on feedback on v2.1 (RSC Advances paper), verified against the 38-paper library.

- `extract` writes `_work/refs.md`: the numbered reference list cut into one entry per line, numbered
  1..N, with `<!-- 缺第 N 条 -->` placeholders for anything it could not cut. On the 38-paper library it
  produced complete lists for 37 papers (3,190 entries, every number present); the 38th uses author-year
  citations and is left to the model. Handles numbers on their own line, entries at the very top of a page,
  section headings / affiliations numbered like references, and years in parentheses after the last entry.
- Greek letters from symbol-encoded fonts without a Unicode map are restored at extraction (`b-TCP` →
  `β-TCP`, `150 mm` → `150 μm`, `2q` → `2θ`). Only fonts verified to use the Symbol encoding are remapped
  (`Symbol*`, Advent `AdvPS*`); TeX math fonts are left alone. Every fix is listed in
  `manifest.json` → `symbol_fixes`. In the test paper this showed that `100 mg mL⁻¹ ascorbic acid` is
  `100 μg mL⁻¹` in the PDF, while `100 mg mL⁻¹ streptomycin` really is printed as mg.
- `source.txt`: paragraphs over 1,500 characters are wrapped at sentence ends (file readers cut lines at
  ~2,000), and words hyphenated across lines are rejoined keeping the hyphen.
- `SKILL.md`: separate rules for extraction errors (follow the page) and errors in the original (copy,
  optionally add「（原文如此）」, never "fix" units from domain knowledge); delivery report must state that
  caption ↔ figure correspondence is checked by eye, not by `check`; brief size given as 10–20% with
  `--cap-chars 120` for a lighter pass.

Not done: falling back to a PDF found inside the paper folder when the given path is wrong (the path is
the caller's input, and guessing a file silently is worse than a clear error); a `check` warning on
mm/μm differences between translation and source (restoring the symbols at extraction removes the cause).

## v2.1

### Added: reading a translated library

- `brief PATH`: a compact reading view of one paper (or a whole library): card facts, outline with
  1-based **file line ranges**, page numbers and section sizes, the abstract, every figure/table caption,
  and the conclusion. About 15% of the full text on a 38-paper test library (median ~5.9k of ~37.6k
  characters). Line ranges can be passed straight to a file reader as offset/limit.
- `SKILL.md` now describes a three-layer reading workflow (cards → brief → section by line range) and a
  procedure for backfilling YAML headers on legacy translations; the skill description triggers on
  questions about an existing translated library as well.

### Fixed

Fixes from an external review run against a 38-paper library (all six defects reproduced before fixing).

- `trim` no longer destroys inputs silently: before overwriting a file in place it copies the original to
  `<dir>/.orig/<name>` (first time only; `--no-backup` to skip). `--out DIR` now works with many images
  and leaves the inputs untouched.
- `cards` fills missing fields from the header block (`**原文标题**` / `**出处**` / `**DOI**`), so
  legacy translations without YAML get title, original title, venue, year and DOI. A one-liner is never
  synthesised from the abstract; such cards are marked `"source": "header-block"`.
- `check`: reference lists numbered `(1)` were reported as "no entries".
- `check`: any bold line after an image counted as its caption (e.g. `**关键词**：…`). Now it must be a
  real caption — bold label plus number in any language, or an unnumbered graphical abstract / TOC.
- `check`: Chinese hard wraps ending in `，`/`、` were missed.
- `check`: line numbers in warnings were one too low.
- Docs: exit code 4 is only for a PDF with no text layer at all; partially scanned PDFs exit 0 with a
  warning, and both cases stop for OCR. Upgrade and legacy-compatibility wording made precise.

Not changed: the trim threshold for dark images (no evidence of over-cropping, and the PDF-rendered crops
sit on white page background) and an `img`/`fig` filename prefix option (re-running `figures` on an old
folder is a regeneration; the "unreferenced file" warning is correct).

## v2.0

Rewritten from `pdf文献翻译_skill_v1.0` to be general-purpose: any platform that loads
the Agent Skills format, any target language, any discipline.

### Fixed

- The two validation scripts contradicted each other (the old `validate_translation.py`
  rejected files starting with YAML front matter). Merged into a single `check`.
- Unquoted YAML values broke on titles containing a colon. Values are now always quoted,
  and `check` verifies it.
- Trimmed figures were written to `_trimmed/` under names that no longer matched the figure
  numbers. `figures` now writes `figNN.png` directly.
- Console output crashed on Chinese Windows (GBK) when printing special characters.
  Unprintable characters are now replaced instead of raising.
- Whole pages were rendered at 300 dpi. Rendering is now on demand and within the image budget.
- `install.ps1` produced mojibake under PowerShell 5.1 and nested directories on upgrade.
  Replaced by `install.py`.

### Added

- Automatic figure location, including vector axis text and layouts where the figure sits on
  the previous page and its caption on the next. Figures are re-rendered from the vector source
  within the pixel budget, plus one overview image for a single-pass review.
- Page rendering with a PDF-point coordinate grid, for manual crop boxes.
- `check` now catches: pages without an anchor (possibly untranslated), figures/tables present in
  the source but missing from the translation (and vice versa), reference counts that disagree
  with the source, and suspected untranslated foreign-language sentences.
- Reference-count estimation supporting `[n]`, `(n)`, `n.` and heading-less layouts.
- Superscript/subscript restoration from PDF span geometry (`10⁸`, `mL⁻¹`, `H₂O₂`).

### Removed bindings

- Platform: no hard-coded PowerShell, backslash paths or client-specific front-matter fields.
- Discipline: generic YAML slots (`subject / method / key_result`); add your own fields freely.
- Language: target language is configurable (default Chinese).
- Model: the image budget is a parameter, defaulting to a value safe for most readers.

### Changed defaults

- Three figure tiers (none / standard / detailed), default `standard`. Per-figure review tables,
  panel close-ups and numeric tables moved to `detailed`.
- Numeric tables may only contain values explicitly stated in the source. Reading values off
  bar heights or curves is forbidden.
- Reference entries stay in the original language unless the user asks otherwise.

### Compatibility

`check` and `cards` still recognise v1.0 outputs: `*_中文翻译.md`, `原文提取.txt`, and the old
`title_zh / title_en / journal` fields. Old folders have no `_work/manifest.json`, so figure and
reference-count comparisons are skipped until you run `extract` once on them.
