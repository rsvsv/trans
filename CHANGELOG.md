# Changelog

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
