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
└── _work/                                          intermediates, deletable at any time
    ├── source.txt                                  reading-order text, page-delimited
    ├── manifest.json                               caption list, reference estimate, crop log
    └── figures_overview_*.png                      all crops on one sheet for review
```

The translation is a single Markdown file with:

- **YAML card header** — title, venue, DOI, one-liner, subject / method / key result. `cards` rolls these
  up into `cards.jsonl` so a whole library can be searched without reading every file.
- **Page anchors** `<!-- p3 -->` on every page, so any sentence can be traced back to the original page.
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
Upgrading = run the install again; old files are replaced, never nested.

Verify: `python scripts/pdftrans.py --version`

## Use

In chat, just ask: *"translate this paper into Chinese"* with the PDF path. The skill runs
extract → figures → translate → check → cards.

The CLI is usable on its own:

| Command | Purpose |
|---|---|
| `extract PDF DIR` | column-ordered text, caption list, reference estimate |
| `figures PDF DIR [--only fig03] [--tables]` | auto-locate and crop every figure + overview sheet |
| `pages PDF DIR --pages 3,5 [--grid]` | render pages; `--grid` overlays PDF-point coordinates |
| `render PDF DIR --page N --clip x0,y0,x1,y1 --name fig03` | manual crop (auto-trim, budget-aware) |
| `trim IMG… [--out]` | trim white margins and cap pixels on external images |
| `check PATH [--strict]` | structural validation; exit code 1 on FAIL |
| `cards ROOT [--md]` | build the card index |

## Design notes

**Image budget.** Multimodal readers downscale large images, so what matters is not dpi but how much of
the frame is actual figure content. Figures are re-rendered from the vector source, tightly cropped, and
capped at 1,150,000 px total / 1568 px long side — safe for most readers. Override with
`--max-px / --max-side` or `PDFTRANS_MAX_PX / PDFTRANS_MAX_SIDE`.

**Figure location.** Captions are found first, then the graphics above (or below) them, including vector
axis text, full-width figures captioned in one column, and figures sitting on the previous page. Each crop
records how it was located; low-confidence ones are flagged for human review instead of silently trusted.

**Validation.** `check` catches missing pages, missing/extra figure and table numbers, broken reference
numbering, missing captions, untranslated sentences, encoding problems and over-budget images. It checks
structure only — semantic spot-checking is still the model's job.

**No fabrication.** Numeric tables may only contain values explicitly stated in the source. Reading
values off bar heights or curves is forbidden, and figures are never redrawn.

## Requirements & limits

- Python ≥ 3.9, `pymupdf`, `pillow` (`pyyaml` optional, for stricter front-matter validation).
- Scanned PDFs without a text layer are refused rather than guessed — run OCR first.
- A rotated or continued caption occasionally needs a manual crop (`pages --grid` then `render`).

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
`_work/`（中间文件，可随时删除重建）。

译稿里有：

- **YAML 卡片头**：标题、出处、DOI、一句话结论、研究对象 / 方法 / 关键结果。`cards` 可把整个文库汇总成
  `cards.jsonl`，做跨文献比较时先读索引，不必通读全库。
- **页码锚点** `<!-- p3 -->`：每页都有，任何一句都能回查原文第几页。
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
升级重跑一遍即可，不会嵌套。

### 用法

对话里直接说「把这篇论文翻译成中文」并给出 PDF 路径即可，技能会按
提取 → 出图 → 翻译 → 校验 → 卡片索引 走完。命令行也可单独使用，见上方命令表。

### 几点设计

- **读图预算**：多模态模型会缩放大图，所以重要的是图内文字占画面的比例，而不是 dpi。配图从矢量源重新
  渲染并紧裁，默认限制 115 万像素、长边 1568 px，可用 `--max-px / --max-side` 调整。
- **配图定位**：先找图注，再找它上方（或下方）的图形，覆盖矢量坐标轴文字、图注在单栏而图跨整页、
  图在上一页等版式。每张图记录定位方式，可信度低的标出来请人核对，不默默采信。
- **校验**：漏页、漏图注、图表编号对不上、参考文献编号断裂、疑似未译的外文句子、编码问题、超预算图片。
  只查结构，语义仍需抽查关键数字。
- **不编数据**：数值表只收原文明确给出的数值，禁止从柱高或曲线估读。

### 限制

扫描件没有文字层时会直接停下来问，不会凭页面图片编内容；个别旋转图注或续图需要手动出图
（`pages --grid` 看坐标，再 `render`）。

### 许可

MIT，见 [LICENSE](LICENSE)。用本技能产出的译稿仅供学习参考，原文版权归原出版方所有。
