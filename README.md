# pdf-paper-translation

把 PDF 文献翻成**「AI 读得省、你查得准」**的中文 Markdown，并附一套能自己校验译稿的工具。

> 一个 PDF 进去，出来是一份带页码锚点、带卡片头、图注完整、数字可核对的中文译稿；读一篇 10 页论文，AI 的花费从 3–4 万 token 降到 2 万出头，查一整个库从「读全部」变成「检索一次」。

**这一页就是全部说明。** 仓库里其他文件都是这个技能自己用的，你不需要逐个看。

---

## 一、它解决什么问题

AI 读 PDF 有三个硬伤：**排版乱**（分栏、跨页打断）、**图看不清**（图注丢失、字被缩没）、**无法核对**（说翻好了，但漏没漏没人知道）。

这个技能把 PDF 先转成结构化文本、把图裁到 AI 读得清的尺寸，再按固定格式产出译稿，最后用脚本机械校验：漏页、漏图注、数字对不上、单位抄错、上标丢失、参考文献条数、超长行——都由脚本查，不靠 AI 自觉。

实测规模：38 篇文献 / 851 页 / 479 张图 / 3,241 条参考文献。

---

## 二、安装（Windows，三步）

需要 Python ≥ 3.9。

**第 1 步：拿一份**

- 直接下载：[`dist/pdf-paper-translation-3.2.1.zip`](dist/pdf-paper-translation-3.2.1.zip)（116 KB，解压就是完整技能目录）
- 或者 `git clone https://github.com/rsvsv/trans`

**第 2 步：装依赖**

```bash
pip install pymupdf pillow
```

**第 3 步：放进技能目录**

```bash
python install.py --for dsh       # 装到 ~/.dsh/skills/pdf-paper-translation（DeepSeek Harness）
python install.py --for claude    # 装到 ~/.claude/skills/pdf-paper-translation
python install.py --target <任意技能目录>/pdf-paper-translation
python install.py --zip           # 重新打一个 zip（上传到 Claude App 用）
```

加 `--deps` 可让脚本顺手装依赖。

**验证**：`python scripts/pdftrans.py --version` → 应显示 `pdftrans 3.2.1`
**自检**：`python scripts/pdftrans.py selftest` → 应显示 `ALL PASSED`（112 项）

> 目录名必须是 `pdf-paper-translation`，要跟 `SKILL.md` 里的 `name` 一致。
> 升级 = 再跑一次安装命令：文件整体覆盖，旧版残留文件会被清掉。

---

## 三、怎么用

**最省事**：在对话里说「把这篇 PDF 翻成中文」并给出路径，技能自己走完全流程。

**自己掌控**（脚本都叫 `pdftrans.py`）：

| 想做的事 | 命令 |
|---|---|
| 提取文本 + 图 + 清单 | `pdftrans extract 论文.pdf 输出目录` |
| 自动裁出所有配图 | `pdftrans figures 输出目录` |
| 看一篇的速览（目录+摘要+图表注+结论） | `pdftrans brief 论文目录` |
| 核对页码锚点 | `pdftrans anchor 论文目录`（加 `--dry-run` 只报告不改） |
| 校验译稿 | `pdftrans check 论文目录`（整库就传库根目录） |
| 原文页 ↔ 译文页并排看 | `pdftrans pair 论文目录 --page 7` |
| 全库检索 | `pdftrans find 库目录 "关键词1\|关键词2"` |
| 折超长行 | `pdftrans wrap 库目录`（只加换行，不改字，改前备份） |
| 汇总卡片索引 | `pdftrans cards 库目录` |
| 旧版提取的库刷到新版 | `pdftrans refresh 库目录`（不碰译稿和图片） |
| 压缩图片到读图预算 | `pdftrans trim 图片或目录` |
| 渲染原页目视核对 | `pdftrans pages 论文.pdf --pages 3,7` |

全部 15 个子命令：`extract / figures / render / pages / trim / brief / anchor / find / wrap / pair / check / cards / refresh / selftest`。

---

## 四、翻完之后得到什么

一篇论文一个文件夹：

```
论文标题/
├── 论文标题_中文翻译.md        ← 译稿（带 YAML 卡片头、每页锚点）
├── figures/fig01.png …         ← 从 PDF 里裁出来的图
└── _work/
    ├── source.txt              ← 原文分页文本（留着：检索和数字核对的依据）
    ├── refs.md                 ← 参考文献一行一条
    └── manifest.json           ← 图注清单、页数、参考文献条数
```

译稿里有什么：

- **YAML 卡片头**：标题、原题、期刊、年、DOI、一句话、主题、方法、关键结果（带 `[pN]` 页码）
- **页码锚点** `<!-- p3 -->`：每页一个，脚本可核，任何一句话都能追回原文页
- **完整图注**：照原图注翻译，图保持原样（不重绘、不往上叠中文）
- **Markdown 表格**、Unicode 上标（`μg mL⁻¹`、`10⁶`、`p < 0.0001`）
- **参考文献**：条数与编号跟原文一致

好处是**三层读法**：`cards`（几千 token 看全库）→ `brief`（一篇的 10–20%）→ 读正文某一段；找不到就 `find` 全库检索。

---

## 五、`check` 到底查什么

| 查什么 | 说明 |
|---|---|
| 漏页 / 页面译文偏少 | 某页原文 1200 字以上而译文几乎为空 |
| 漏图注 / 图注编号异常 | 每张图后面必须有 `**图 N**`；译稿编号要在原图注清单里 |
| 图注数字 | 图注里出现了原图注没有的数字或单位（标尺会单独标出） |
| 数字对不上 | 译稿里的数字在原文中找不到 |
| 数字 + 单位组合 | 数在、但「250 μm」这种组合原文没有（单位抄错、配错组） |
| 均值 ± SD | 这个组合原文没有 |
| 丢了上标 | `10⁶` 写成 `106`、`10⁻¹` 写成 `10-1` |
| 参考文献条数 | 与原文估计条数不符 |
| 页码锚点 | 与原文分页对不上时给出建议 |
| key_result 页码 | 每条是否带 `[pN]`，且该条数字确实在那一页 |
| 超长行 | 超过 2000 字的行（读文件工具会截断） |

结论只有三种：`FAIL`（必须改）、`WARN`（对着原文看一眼）、`OK`。全库跑一次约 5 秒。

---

## 六、仓库里每个文件是干嘛的

| 文件 | 作用 | 你需不需要看 |
|---|---|---|
| `README.md` | 本页，全部说明 | **看这个就够** |
| `SKILL.md` | 给 AI 看的工作流程说明书（触发时加载） | 不用 |
| `references/output-spec.md` | 译稿格式规范（AI 写译稿时按这个） | 想了解格式再看 |
| `references/figure-spec.md` | 配图尺寸与裁切规范 | 不用 |
| `scripts/pdftrans.py` | 全部功能，单文件约 3,500 行 | 不用 |
| `tests/test_pdftrans.py` | 112 项回归测试 | 不用 |
| `install.py` | 安装脚本 | 装的时候用 |
| `CHANGELOG.md` | 每一版改了什么 | 想知道细节再看 |
| `dist/*.zip` | 打包好的技能，一个文件下载 | 装的时候用 |
| `LICENSE` | MIT，随便用 | — |

---

## 七、限制（说实话的部分）

- **不做 OCR**：扫描件会认出来并提示你「需要先 OCR」，但不会自己识别。
- **不解析表格结构**：表格按原文留着，`check` 校不了表格单元格里的数。
- **不查「正文引用 ↔ 参考文献」对应**：不会验证正文里的 `[37]` 在参考文献里是否对得上。
- **图注与图片的对应靠人**：脚本查编号和数字，不判断这张图是不是该配这段说明。
- **只查硬错误，不管学术对错**：「这句译得对不对」只能靠人对照原文读。
- 脚本是单文件，长期扩展会变得拥挤。

---

## 八、版本历史

| 版本 | 主要变化 |
|---|---|
| v2.0 | 稳定提取：分栏、图注、参考文献 |
| v2.1 | `brief` 速览层；外部评审 6 个缺陷修复 |
| v3.0 | 页码锚点、`pair` 逐页对照、`find` 全库检索 |
| v3.2 | 检索层提速、抽检流程、91 项回归测试进包 |
| v3.2.1 | 批处理不中断（坏 PDF 只报错跳过）、数字与页眉判定修正、单位词歧义修正、112 项测试 |

细节见 [CHANGELOG.md](CHANGELOG.md)。

---

## 九、许可

MIT。作者：rsvsv。

---

<details>
<summary>English</summary>

**pdf-paper-translation** turns academic PDFs into faithful, verifiable, searchable Markdown (Chinese by default)
so an AI can read them cheaply and you can check every claim against the original page. Scripts handle column
text extraction, figure cropping, page anchors, and mechanical validation (missing pages, missing captions,
number/unit mismatches, lost superscripts, reference counts, over-long lines). One paper in, one Markdown file
with a card header, page anchors and full captions out — plus a library layer (`cards`, `brief`, `find`, `pair`)
for querying dozens of papers precisely. MIT licensed. Install:
`python install.py --for claude|dsh`, or upload `dist/pdf-paper-translation-3.2.1.zip` to the Claude app.

</details>