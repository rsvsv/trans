# pdf-paper-translation

**English** | [简体中文](#简体中文)

An [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) that turns an academic
PDF into a **faithful, page-verifiable Markdown translation** — default target language Chinese, any other
language on request.

The scripts do the mechanical work (column-aware text extraction, figure location and cropping, structure
validation); the model only translates and judges. Works in Claude and any client that loads the
`SKILL.md` format.

## What you get

For each paper, one folder:

```
2025_Wang_mineralized-collagen-scaffold/
├── 2025_Wang_mineralized-collagen-scaffold.zh.md   the translation
├── figures/fig01.png …                             figures cropped from the vector source
└── _work/
    ├── source.txt                                  reading-order text, page-delimited (keep: search + number checks)
    ├── refs.md / manifest.json                     references one per line; caption list, reference estimate (keep)
    └── figures_overview_*.png                      all crops on one sheet for review (deletable)
```

The translation is a single Markdown file with:

- **YAML card header** — title, venue, DOI, one-liner, subject / method / key result. `cards` rolls these
  up into `cards.jsonl` so a whole library can be searched without reading every file.
- **Page anchors** `<!-- p3 -->` on every page, verified against the source by `anchor`, so any sentence can be
  traced back to the original page.
- **Searchable cards**: keywords, key results tagged with their page `[p9]`, and the library's own fields
  (declared once in `_fields.md`) — `cards --list` and `find --in cards` filter a whole library in a few
  thousand tokens.
- **Full captions** translated in place, figures kept as-is (never redrawn, never overlaid with translated text).
- **References** with the same count and numbering as the original.

## Install

Requires Python ≥ 3.9.

```bash
git clone https://github.com/rsvsv/trans ~/.claude/skills/pdf-paper-translation
pip install pymupdf pillow
```

The repository is called `trans`, but the skill directory must be named `pdf-paper-translation`
(it has to match `name` in `SKILL.md`) — hence the explicit clone target above.
To install in the Claude app, download **`pdf-paper-translation.zip` from the
[latest release](https://github.com/rsvsv/trans/releases/latest)** and upload it; GitHub's own
"Download ZIP" wraps everything in a `trans-main/` folder, which the app will not recognise.

Or, from a downloaded copy:

```bash
python install.py --for claude --deps     # ~/.claude/skills/pdf-paper-translation
python install.py --for dsh --deps        # ~/.dsh/skills/pdf-paper-translation
python install.py --target <skills-dir>/pdf-paper-translation
python install.py --zip                   # build a zip for uploading in the Claude app
```

Keep the directory name `pdf-paper-translation` — it must match `name` in `SKILL.md`.
Upgrading = run the install again: `scripts/` and `references/` are replaced wholesale (never nested),
top-level files are overwritten. Files that only existed in an older version's top level are left alone.

Verify: `python scripts/pdftrans.py --version`

## Use

In chat, just ask: *"translate this paper into Chinese"* with the PDF path. The skill runs
extract → figures → translate → anchor → check → spot check (`pair` + an independent reviewer) → cards.

The CLI is usable on its own:

| Command | Purpose |
|---|---|
| `extract PDF DIR` | column-ordered text, references split one per line (`refs.md`), caption list, symbol-font Greek restored |
| `figures PDF DIR [--only fig03] [--tables]` | auto-locate and crop every figure + overview sheet |
| `pages PDF DIR --pages 3,5 [--grid]` | render pages; `--grid` overlays PDF-point coordinates |
| `render PDF DIR --page N --clip x0,y0,x1,y1 --name fig03` | manual crop (auto-trim, budget-aware) |
| `trim IMG… [--out FILE\|DIR]` | trim margins and cap pixels; in place by default, originals kept in `.orig/` |
| `anchor PATH [--dry-run] [--auto]` | verify page anchors against the source; fix only on strong evidence; insert when missing |
| `check PATH [--strict] [--json]` | structure, numbers vs. source, card fields; every finding listed with its context; exit code 1 on FAIL |
| `pair DIR --page N \| --line L \| --sample K` | a source page (running headers removed) next to its translation, for spot checks and for judging a warning |
| `wrap PATH [--dry-run]` | wrap lines over 1,500 characters at sentence ends; only line breaks change, original backed up |
| `refresh ROOT [--dry-run] [--pdf-dir DIR]` | re-extract `_work/` made by an older version from the recorded PDF; translations and images untouched |
| `selftest` | bundled regression tests on synthetic fixtures; run once after upgrading |
| `brief PATH [--toc-only] [--cap-chars N]` | reading view: outline with file lines / pages / sizes, abstract, every caption, conclusion (~10–20% of a full paper) |
| `cards ROOT [--md] [--list [--with F,..]]` | build the card index; `--list` prints one line per paper |
| `find ROOT PATTERN [--and P] [--not P] [--in zh,src,cards] [--brief]` | search translations + source text, grouped by paper, with line, page, section and `[引]/[否]` labels; expands terms from `_glossary.md` |

## Design notes

**Image budget.** Multimodal readers downscale large images, so what matters is not dpi but how much of
the frame is actual figure content. Figures are re-rendered from the vector source, tightly cropped, and
capped at 1,150,000 px total / 1568 px long side — safe for most readers. Override with
`--max-px / --max-side` or `PDFTRANS_MAX_PX / PDFTRANS_MAX_SIDE`.

**Figure location.** Captions are found first, then the graphics above (or below) them, including vector
axis text, full-width figures captioned in one column, and figures sitting on the previous page. Each crop
records how it was located; low-confidence ones are flagged for human review instead of silently trusted.

**Validation.** `check` catches missing pages, missing/extra figure and table numbers, broken reference
numbering, missing captions, untranslated sentences, encoding problems and over-budget images — and,
against `_work/source.txt`, numbers that do not occur in the original, powers of ten that lost their
superscript (`1 × 106`), number + unit and value ± sd combinations the source does not have, captions that add
or drop quantities compared with the original caption, key results whose numbers are not on the cited page,
and pages left untranslated (a page that merely continues a paragraph across a figure page is recognised by
its own numbers sitting in the translation just before it). Each finding comes with the translation line around
it and, for unit mismatches, the nearest occurrence in the source, so most can be judged at a glance.

**Spot checks.** What a script cannot see — a dropped sentence, two groups' values swapped, a negation reversed,
an invented clause — is left to reading. `pair` puts a source page next to its translation; the skill hands two
random pages per paper (plus any page flagged as thin) to a reviewer that did not translate it. In a blind test
with 4 such errors hidden in 6 pages, the reviewer found all 4 with no false alarm on the clean pages. It is a
sample, and reported as one.

**No fabrication.** Numeric tables may only contain values explicitly stated in the source. Reading
values off bar heights or curves is forbidden, and figures are never redrawn.

**Reading a translated library.** Four layers, each only when the previous one is not enough:
`cards --list` to pick papers → `find` to locate passages (translation and source together, with page and
line) → `brief` (outline, abstract, captions, conclusion) → read only the needed section by line range.
"Which papers did X" questions pool candidates from a workspace field and `find`, then judge each one on its
snippet — two keyword-based routes agreeing is not verification.

## Requirements & limits

- Python ≥ 3.9, `pymupdf`, `pillow` (`pyyaml` optional, for stricter front-matter validation).
- Scanned PDFs without a text layer are refused rather than guessed — run OCR first.
- A rotated or continued caption occasionally needs a manual crop (`pages --grid` then `render`).
- Older translations without a YAML header still work with `check` and `cards`, but their cards only carry
  what the header block states (title, original title, venue, year, DOI) — no one-liner or key result.

## License

MIT — see [LICENSE](LICENSE). Translations produced with this skill are for study and reference;
copyright of the original papers stays with their publishers.

---

## 简体中文

把 PDF 文献全文翻译成**忠实、完整、可逐页核对**的 Markdown 译稿，默认中文，也可指定其他语言。
符合 Agent Skills 通用格式，Claude、DSH 等支持该格式的客户端都能加载。

脚本负责机械活和查漏，模型只负责翻译和判断。

### 产物

每篇一个文件夹：译稿 `<文件夹名>.zh.md`、`figures/fig01.png …`（从矢量源紧裁的配图）、
`_work/`（`source.txt`、`refs.md`、`manifest.json` 要保留，检索和数字核对靠它们；总览图可删）。

译稿里有：

- **YAML 卡片头**：标题、出处、DOI、一句话结论、研究对象 / 方法 / 关键结果。`cards` 可把整个文库汇总成
  `cards.jsonl`，做跨文献比较时先读索引，不必通读全库。
- **页码锚点** `<!-- p3 -->`：每页都有，`anchor` 按原文复核过，任何一句都能回查原文第几页。
- **可检索的卡片**：关键词、带页码 `[p9]` 的关键结果、本库自定义字段（在 `_fields.md` 里声明一次），
  `cards --list` 和 `find --in cards` 几千 token 就能筛完整个库。
- **完整图注**：图按原样裁出，不重绘、不在图上叠加译文。
- **参考文献**：条数、编号与原文完全一致。

### 安装

需要 Python ≥ 3.9。

```bash
git clone https://github.com/rsvsv/trans ~/.claude/skills/pdf-paper-translation
pip install pymupdf pillow
```

仓库名是 `trans`，但技能目录必须叫 `pdf-paper-translation`（要与 `SKILL.md` 里的 `name` 一致），
所以 clone 时要像上面那样指定目标目录。

装进 Claude 应用：下载 [最新 release](https://github.com/rsvsv/trans/releases/latest) 里的
**`pdf-paper-translation.zip`** 上传即可。GitHub 自带的「Download ZIP」外层是 `trans-main/`，
应用不认这个目录名。

或下载后运行 `python install.py --for claude --deps`（DSH 用 `--for dsh`）。
升级重跑一遍即可：`scripts/`、`references/` 整体替换（不会嵌套），根目录文件覆盖；旧版根目录里独有的文件不会被删。

### 用法

对话里直接说「把这篇论文翻译成中文」并给出 PDF 路径即可，技能会按
提取 → 出图 → 翻译 → 锚点复核 → 校验 → 抽检（`pair` 对照 + 独立子代理核对）→ 卡片索引 走完。命令行也可单独使用，见上方命令表。

### 几点设计

- **读图预算**：多模态模型会缩放大图，所以重要的是图内文字占画面的比例，而不是 dpi。配图从矢量源重新
  渲染并紧裁，默认限制 115 万像素、长边 1568 px，可用 `--max-px / --max-side` 调整。
- **配图定位**：先找图注，再找它上方（或下方）的图形，覆盖矢量坐标轴文字、图注在单栏而图跨整页、
  图在上一页等版式。每张图记录定位方式，可信度低的标出来请人核对，不默默采信。
- **校验**：漏页、漏图注、图表编号对不上、参考文献编号断裂、疑似未译的外文句子、编码问题、超预算图片；
  并对照原文查出原文里没有的数字、丢了上标的 10 的幂（`1 × 106`）、不在所标页码上的关键结果、漏译的页。
  每条问题都带译文上下文，单位不符的还给出原文里最接近的写法，多数一眼可判。
- **抽检**：漏一句、两组数值对调、否定译反、凭空加一句，脚本看不出来。`pair` 把原文一页和对应译文并排，
  每篇随机 2 页（加上被报译文偏少的页）交给没参与翻译的子代理逐句核对。盲测：6 页里藏 4 处这类错误，全部找出，
  干净的页没有误报。这是抽样，报告里也按抽样写。
- **不编数据**：数值表只收原文明确给出的数值，禁止从柱高或曲线估读。
- **读译文库**：分四层，上一层不够用才往下走。`cards --list` 选文献 → `find` 定位（译文和原文一起查，带页码和行号）
  → `brief`（目录、摘要、全部图注、结论）→ 按行号只读需要的那一节。“库里哪些文献做了 X”要把工作区字段和 `find` 两路的候选合起来，逐篇看片段判断（两路都命中不等于已核实）。

### 限制

扫描件没有文字层时会直接停下来问，不会凭页面图片编内容；个别旋转图注或续图需要手动出图
（`pages --grid` 看坐标，再 `render`）。

没有 YAML 头的旧译稿仍可运行 `check` 和 `cards`，但卡片只有页眉信息块里写明的字段（标题、原文标题、出处、
年份、DOI），没有一句话结论和关键结果。

`trim` 默认原地覆盖，首次覆盖前把原图备份到同目录的 `.orig/`；用 `--out 目录` 可输出到别处、完全不动原图。

### 许可

MIT，见 [LICENSE](LICENSE)。用本技能产出的译稿仅供学习参考，原文版权归原出版方所有。
