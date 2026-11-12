# Changelog

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
