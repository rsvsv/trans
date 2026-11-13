"""Regression tests for pdftrans (anchor / find / check / pair / wrap / extract). Each test builds its own
synthetic fixture in a temp dir; nothing outside it is read or written.
Run:  python scripts/pdftrans.py selftest      (or: python tests/test_pdftrans.py [path/to/pdftrans.py])
Covers the defects found by independent reviews, the end-to-end translation run and the v3.2 changes."""
import importlib.util
import io
import itertools
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
_ARGS = [x for x in sys.argv[1:] if not x.startswith("-")]  # the only argument is the script path (options ignored)
SCRIPT = os.path.abspath(_ARGS[0]) if _ARGS else os.path.join(HERE, "..", "scripts", "pdftrans.py")
if not os.path.isfile(SCRIPT):
    sys.exit(f"pdftrans.py not found: {SCRIPT}  (usage: python test_pdftrans.py [path/to/pdftrans.py])")
spec = importlib.util.spec_from_file_location("pdftrans", SCRIPT)
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)

FAILS = []


def ok(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(*args):
    # run the way the skill runs it (no -I: PyMuPDF may live in the user site-packages); cwd is a fresh temp dir
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, encoding="utf-8",
                       cwd=tempfile.gettempdir())
    return r.returncode, r.stdout + r.stderr


def paper(root, name, body, src_pages, yaml_head='---\ntitle: "T"\ntitle_orig: "T"\ntarget_lang: "zh"\n---\n',
          encoding="utf-8"):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "_work"), exist_ok=True)
    with open(os.path.join(d, name + ".zh.md"), "w", encoding=encoding, newline="\n") as fh:
        fh.write(yaml_head + body)
    with open(os.path.join(d, "_work", "source.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"<<<PAGE {i}>>>\n{t}\n\n" for i, t in enumerate(src_pages, 1)))
    return d, os.path.join(d, name + ".zh.md")


def section(out, key):
    """A warning line containing key, with its indented detail lines (v3.2 lists findings one per line)."""
    lines = out.splitlines()
    for i, x in enumerate(lines):
        if key in x and ("WARN" in x or "FAIL" in x):
            j = i + 1
            while j < len(lines) and lines[j].startswith("          "):
                j += 1
            return "\n".join(lines[i:j])
    return ""


def with_captions(d, caps, pages, figs):
    """Empty figure files plus a manifest with the original captions, as extract would leave them."""
    os.makedirs(os.path.join(d, "figures"))
    for n in figs:
        open(os.path.join(d, "figures", n + ".png"), "wb").write(b"")
    json.dump({"pages": pages, "captions": caps}, open(os.path.join(d, "_work", "manifest.json"), "w"))


def strip_anchors(t):
    t = pt.ANCHOR_RE.sub("", t)
    t = re.sub(r'(?m)^anchors: ".*"\n', "", t)
    return [ln for ln in t.split("\n") if ln.strip()]


P1 = "Alpha study of Zeolite scaffolds measured 12.34 and 56.78 in Kelvinator chambers. Lorem ipsum."
P2 = "Bravo results: Quixotic membranes reached 98.76 with 43.21 in Yarrowia cultures. Dolor sit."
P3 = "Charlie discussion of Wombat implants gave 11.11 and 22.22 using Xylophone probes. Amet."


def t_encoding(tmp):
    print("non-UTF-8 file is refused, not rewritten")
    d, f = paper(tmp, "gbk", "# 标题\n\n<!-- p1 -->\n\n## 摘要\n\n中文段落 12.34 56.78 Zeolite\n", [P1], encoding="gbk")
    before = open(f, "rb").read()
    code, out = run("anchor", d)
    ok(code == 1 and "拒绝" in out, f"anchor exits 1 with [拒绝] (got {code})")
    ok(open(f, "rb").read() == before, "file bytes unchanged")


def t_yaml(tmp):
    print("malformed YAML is refused")
    d, f = paper(tmp, "badyaml", "# T\n\n## 摘要\n\n段落 12.34 56.78 Zeolite\n", [P1],
                 yaml_head='---\ntitle: "T\ntitle_orig: T\n')
    before = open(f, "rb").read()
    code, out = run("anchor", d)
    ok(code == 1 and "拒绝" in out, "anchor refuses unclosed YAML")
    ok(open(f, "rb").read() == before, "file bytes unchanged")


def t_src_first_paragraph(tmp):
    print("find --in src sees the first paragraph of every page")
    d, _ = paper(tmp, "srcfirst", "# T\n\n<!-- p1 -->\n\n## 摘要\n\n文字\n",
                 ["Title of the paper\n\nSecond paragraph here.", "Introduction text first on page two.\n\nMore."])
    for pat in ("Title of the paper", "Introduction text"):
        code, out = run("find", d, pat, "--in", "src")
        ok("NO MATCH" not in out and "原文 1" in out, f"'{pat}' found")
    code, out = run("find", d, "Introduction text", "--in", "src", "--json")
    ok('"page": 2' in out, "page of first paragraph on page 2 is 2")


def t_blocks_not_split(tmp):
    print("anchor never splits a soft-wrapped paragraph, a table, or touches code")
    body = ("# T\n\n<!-- p1 -->\n\n## 摘要\n\n" + "第一段 Zeolite 12.34 56.78 Kelvinator。\n\n"
            "跨行段落开头 Quixotic 98.76，\n接着写 Yarrowia 43.21。\n\n"
            "| a | b |\n|---|---|\n| Wombat 11.11 | 22.22 |\n<!-- p3 -->\n| x | y |\n\n"
            "```\n<!-- p2 -->\n# not a heading\n```\n\n"
            "最后一段 Wombat 11.11 22.22 Xylophone。\n")
    d, f = paper(tmp, "blocks", body, [P1, P2, P3])
    code, out = run("anchor", d)
    t = open(f, encoding="utf-8").read()
    ok("跨行段落开头 Quixotic 98.76，\n接着写 Yarrowia 43.21。" in t, "soft-wrapped paragraph intact")
    ok("```\n<!-- p2 -->\n# not a heading\n```" in t, "code block untouched")
    ok(re.search(r"\| Wombat 11\.11 \| 22\.22 \|\n\| x \| y \|", t) is not None, "table rows rejoined, no anchor inside")
    body_anchors = [int(x) for x in re.findall(r"(?m)^<!-- p(\d+) -->$", t.split("```")[0] + t.split("```")[-1])]
    ok(body_anchors == [1, 2, 3], f"one standalone anchor per page outside code (got {body_anchors})")
    ok(strip_anchors(t) == strip_anchors(open(os.path.join(d, "_work", "backup", "blocks.zh.before-anchor.md"),
                                               encoding="utf-8").read()), "text identical apart from anchors")
    code, out = run("anchor", d)
    ok("[OK]" in out and open(f, encoding="utf-8").read() == t, "second run: OK and file unchanged (idempotent)")


def t_cross_page(tmp):
    print("a paragraph that starts on p1 and runs onto p2 stays on p1")
    src = ["Opening words Aardvark Borealis Cumulus start here and continue",
           "onto the next page with 77.71 88.82 99.93 Dromedary Eucalyptus values. Then page two text Fjord 31.41."]
    body = ("# T\n\n<!-- p1 -->\n\n## 摘要\n\n"
            "开头 Aardvark Borealis Cumulus 的长段落一直延续到下一页，" + "中文" * 40 + "数值 77.71、88.82、99.93，Dromedary Eucalyptus。\n\n"
            "<!-- p2 -->\n\n第二页正文 Fjord 31.41。\n")
    d, f = paper(tmp, "crosspage", body, src)
    code, out = run("anchor", d, "--dry-run")
    ok("[OK]" in out, f"no move to the continuation page ({out.strip().splitlines()[0] if out.strip() else ''})")


def t_moves_on_evidence(tmp):
    print("a paragraph wholly on the next page is moved")
    body = ("# T\n\n<!-- p1 -->\n\n## 摘要\n\n第一段 Zeolite 12.34 56.78 Kelvinator。\n\n"
            "放错页的段落 Quixotic 98.76 43.21 Yarrowia。\n\n<!-- p2 -->\n\n## 讨论\n\n"
            "第三段 Wombat 11.11 22.22 Xylophone。\n")
    d, f = paper(tmp, "move", body, [P1, P2 + " " + P3] + [f"Filler page {k} Vortex{k} {k}{k}.{k}" for k in range(3, 7)])
    code, out = run("anchor", d)
    ok("[校正]" in out, "reported as correction")
    t = open(f, encoding="utf-8").read()
    ok(t.index("<!-- p2 -->") < t.index("放错页的段落"), "p2 anchor now before the moved paragraph")


def t_duplicates_and_meta(tmp):
    print("duplicate anchors are repaired; check sees them without a manifest; OK writes metadata")
    body = ("# T\n\n<!-- p1 -->\n\n## 摘要\n\n第一段 Zeolite 12.34 56.78 Kelvinator。\n\n<!-- p2 -->\n\n"
            "第二段 Quixotic 98.76 43.21 Yarrowia。\n\n<!-- p2 -->\n\n第三段 Wombat 11.11 22.22 Xylophone。\n")
    d, f = paper(tmp, "dup", body, [P1, P2, P3])
    code, out = run("check", d)
    ok("不是严格递增" in out, "check reports duplicate p2 from source.txt page count")
    code, out = run("anchor", d)
    ok("[整理]" in out or "[校正]" in out, "anchor does not say OK")
    t = open(f, encoding="utf-8").read()
    ok(re.findall(r"<!-- p(\d+) -->", t) == ["1", "2", "3"], "anchors 1,2,3 afterwards")
    ok('anchors: "checked"' in t, "metadata written")
    d2, f2 = paper(tmp, "okmeta", "# T\n\n<!-- p1 -->\n\n## 摘要\n\n第一段 Zeolite 12.34 56.78 Kelvinator。\n\n"
                   "<!-- p2 -->\n\n第二段 Quixotic 98.76 43.21 Yarrowia。\n", [P1, P2])
    run("anchor", d2)
    ok('anchors: "checked"' in open(f2, encoding="utf-8").read(), "OK case writes anchors: checked")


def t_bom_crlf(tmp):
    print("BOM / CRLF normalisation is reported")
    d, f = paper(tmp, "bom", "", [P1, P2])
    open(f, "wb").write(("﻿---\r\ntitle: \"T\"\r\ntitle_orig: \"T\"\r\ntarget_lang: \"zh\"\r\n---\r\n# T\r\n\r\n"
                         "<!-- p1 -->\r\n\r\n## 摘要\r\n\r\n第一段 Zeolite 12.34 56.78。\r\n\r\n<!-- p2 -->\r\n\r\n"
                         "第二段 Quixotic 98.76 43.21。\r\n").encode("utf-8"))
    code, out = run("anchor", d)
    ok("编码规范化" in out, "normalisation reported")
    raw = open(f, "rb").read()
    ok(not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw, "written as UTF-8 without BOM, LF")


def t_auto_assign():
    print("auto_assign is optimal (brute force)")
    S = [[0.5, 0.63, -0.89, -0.71], [1.06, 0.57, 1.64, 3.59]]
    ok(pt.auto_assign(S) == [1, 3], f"reviewer counterexample → [1, 3] (got {pt.auto_assign(S)})")
    rnd = random.Random(7)
    bad = 0
    for _ in range(400):
        n, P = rnd.randint(1, 5), rnd.randint(1, 4)
        S = [[rnd.uniform(-2, 4) for _ in range(P)] for _ in range(n)]

        def score(a):
            return sum(S[i][a[i]] for i in range(n)) - 0.3 * a[0] - 0.3 * sum(a[i] - a[i - 1] for i in range(1, n))
        best = max((a for a in itertools.product(range(P), repeat=n) if list(a) == sorted(a)), key=score)
        got = pt.auto_assign(S)
        if abs(score(got) - score(best)) > 1e-9 or got != sorted(got):
            bad += 1
    ok(bad == 0, f"400 random cases optimal and monotone ({bad} bad)")


def t_fields(tmp):
    print("_fields.md parsing and per-label reporting")
    f = os.path.join(tmp, "_fields_t.md")
    open(f, "w", encoding="utf-8").write("| Field | Desc |\n|:--|--:|\n| in_vivo | ①… ‖ 感染：… ‖ 时间：… |\n")
    defs = pt.parse_fields_file(f)
    ok([n for n, _ in defs] == ["in_vivo"], f"short separator, header skipped (got {[n for n, _ in defs]})")
    iss = pt.field_issues({"in_vivo": "①大鼠 ‖ 感染：无 ‖ 时间：4 周 ②小鼠 ‖ 时间：2 周"}, defs)
    txt = " ".join(m for _, m in iss)
    ok("第 ② 条缺「‖ 感染：」" in txt.replace("第 ②", "第 ②") or "第②条缺「‖ 感染：」" in txt.replace(" ", ""),
       f"missing label reported by name ({txt})")
    ok("时间" not in txt, "present label not reported")


def t_find_misc(tmp):
    print("find: --in validation, code fences, underscores, incomplete report")
    root = os.path.join(tmp, "lib")
    os.makedirs(root)
    paper(root, "a", "# T\n\n<!-- p1 -->\n\n## 方法\n\n```\n# 参考文献\n```\n\n真实段落 calvaria 感染 in_vivo_x。\n", [P1],
          yaml_head='---\ntitle: "T"\nin_vivo: "①大鼠 颅骨 ‖ 感染：S. aureus ②大鼠 颅骨 ‖ 感染：无"\n---\n')
    os.makedirs(os.path.join(root, "b", "_work"))
    open(os.path.join(root, "b", "b.zh.md"), "w", encoding="utf-8").write("# T\n\n## 方法\n\ncalvaria 感染\n")
    code, out = run("find", root, "x", "--in", "foo")
    ok(code == 2, "--in foo rejected with exit 2")
    code, out = run("find", root, "calvaria", "--and", "感染", "--in", "zh")
    ok("a  译文 1" in out, "heading inside code fence does not cut the search")
    ok("in_vivo_x" in out, "underscores kept in snippets")
    code, out = run("find", root, "calvaria", "--in", "zh,src")
    ok("未完整检索" in out and "b" in out, "paper without source.txt reported as incompletely searched")
    code, out = run("find", root, "颅骨", "--and", "感染：(?!无|未报告)", "--in", "cards")
    ok(out.count("卡片  in_vivo") == 1, "card entries matched one by one")


def t_coverage_table(tmp):
    print("a page whose content is a translated table is not reported as untranslated")
    rows = "\n".join(f"Row{i} Alpha{i} {i}.{i}1 Beta{i} {i}.{i}2" for i in range(60))
    trows = "\n".join(f"| Row{i} | Alpha{i} | {i}.{i}1 | Beta{i} | {i}.{i}2 |" for i in range(60))
    body = ("# T\n\n<!-- p1 -->\n\n## 结果\n\n第一段 Zeolite 12.34 56.78 Kelvinator。\n\n<!-- p2 -->\n\n"
            "| a | b | c | d | e |\n|---|---|---|---|---|\n" + trows + "\n\n**表 1** 数据\n")
    d, f = paper(tmp, "cov", body, [P1, rows.replace("\n", " ")])
    code, out = run("check", d)
    ok("可能漏译" not in out, "no omission warning for the table page")


def t_number_context(tmp):
    print("v3.1: number + unit and value ± sd checked against the source")
    src = ["Cells were treated with 20 μg/mL drug at 37 ◦C for 6 and 24 h. Ca/P was 1.50 ± 0.02 in group A and 2.10 ± 0.05 in group B.",
           "Bacteria at 1 × 10⁶ CFU/mL."]
    body = ("# T\n\n<!-- p1 -->\n\n## 方法\n\n细胞用 20 mg/mL 药物在 37 °C 处理 6 h 与 24 h。A 组 Ca/P 为 1.50 ± 0.02，"
            "B 组为 1.50 ± 0.05（数都在原文，但配错了对）。\n\n<!-- p2 -->\n\n细菌浓度 106 CFU/mL。\n")
    d, f = paper(tmp, "numctx", body, src)
    code, out = run("check", d)
    line = section(out, "组合原文没有")
    ok("20 mg" in line, "wrong unit 20 mg/mL (source 20 μg/mL) reported")
    ok("1.50 ± 0.05" in line, "value ± sd pair not in source reported")
    vals = re.findall(r"「([^」]+)」", line)  # the reported values (detail lines also quote the context)
    ok(not {"37 °C", "6 h", "24 h"} & set(vals),
       "37 ◦C = 37 °C and '6 and 24 h' list are not false alarms")
    ok("106 CFU" in out and "上标" in out, "106 CFU reported as lost superscript")


def t_captions(tmp):
    print("v3.1: captions compared with the original captions")
    src = ["Text. Figure 1. SEM images of scaffold A with pore size 12.5 μm and porosity 81.3%. Data are 3.14 ± 0.2.",
           "Figure 2. Release profile of drug B at pH 5.5 and 7.4 showing 45.6% and 67.8% release with 1.23 ± 0.04."]
    caps = [{"page": 1, "kind": "fig", "num": "1", "text": src[0][6:]},
            {"page": 2, "kind": "fig", "num": "2", "text": src[1]},
            {"page": 2, "kind": "suppfig", "num": "S1", "text": "Figure S1. Extra 9.87 data."}]
    body = ("# T\n\n<!-- p1 -->\n\n## 结果\n\n正文。\n\n![图 1](figures/fig01.png)\n\n"
            "**图 1** 支架 A 的 SEM 图像，孔径 12.5 μm。标尺 50 μm。数据为 3.14 ± 0.2。\n\n<!-- p2 -->\n\n"
            "![图 2](figures/fig02.png)\n\n**图 2** 药物 B 在 pH 5.5 与 7.4 下的释放曲线，释放 45.6% 与 67.8%，1.23 ± 0.04。\n\n"
            "**补充文件 1：图 S1** 额外数据 9.87。\n")
    d, f = paper(tmp, "caps", body, src)
    with_captions(d, caps, 2, ("fig01", "fig02"))
    code, out = run("check", d)
    ok(re.search(r"图 1 L\d+ \[标尺\] 「50 μm」", section(out, "原图注没有的数字")) is not None, "scale bar added to caption 1 reported")
    ok(re.search(r"漏译半句.*图 1 L\d+：81\.3", out) is not None, "81.3 missing from caption 1 reported")
    ok("图 2" not in section(out, "原图注没有的数字") + section(out, "漏译半句"),
       "faithful caption 2 not reported")
    ok("图 1 L" not in out.split("张冠李戴")[1] if "张冠李戴" in out else True, "no false swap")
    # swap captions 1 and 2 bodies -> swapped warning
    t = open(f, encoding="utf-8").read()
    c1 = re.search(r"\*\*图 1\*\*(.*)", t).group(1)
    c2 = re.search(r"\*\*图 2\*\*(.*)", t).group(1)
    t = t.replace("**图 1**" + c1, "**图 1**@@2").replace("**图 2**" + c2, "**图 2**" + c1).replace("@@2", c2)
    open(f, "w", encoding="utf-8").write(t)
    code, out = run("check", d)
    ok("张冠李戴" in out and "图 1" in out.split("张冠李戴")[1], "swapped captions reported")
    ok("补充文件" not in out and "图 S1" not in " ".join(x for x in out.splitlines() if "原图注" in x),
       "'补充文件 1：图 S1' compared with Figure S1, not Figure 1")


def t_glossary_brief(tmp):
    print("v3.1: find expands terms from _glossary.md; --brief prints one line per paper")
    root = os.path.join(tmp, "lib")
    os.makedirs(root)
    open(os.path.join(root, "_glossary.md"), "w", encoding="utf-8").write(
        "| 英文 | 中文 |\n|---|---|\n| osteomyelitis | 骨髓炎（osteomyelitis） |\n| calvarial defect | 颅骨缺损 |\n")
    paper(root, "a", "# T\n\n<!-- p1 -->\n\n## 方法\n\n文字\n\n第二段\n", ["Mice developed osteomyelitis in the calvarial defect."])
    paper(root, "b", "# T\n\n<!-- p1 -->\n\n## 方法\n\n文字\n", ["Nothing relevant here at all."])
    code, out = run("find", root, "骨髓炎", "--in", "src")
    ok("+osteomyelitis" in out and "命中 1 篇" in out, "骨髓炎 expanded to osteomyelitis, source hit")
    code, out = run("find", root, "骨髓炎", "--in", "src", "--no-expand")
    ok("NO MATCH" in out, "--no-expand turns it off")
    code, out = run("find", root, "颅骨", "--in", "src", "--brief")
    rows = [x for x in out.splitlines() if x.startswith("== ")]
    ok(len(rows) == 1 and " | " in rows[0], "颅骨 → 颅骨缺损 group → calvarial defect; one line per paper")
    code, out = run("find", root, "osteo(myel)?itis", "--in", "src")
    ok("术语表扩展" not in out, "patterns with regex syntax are not expanded")


# ----------------------------------------------------------------------------- v3.2
HDR = "Journal of Things 2024, 15, 1234 {n} of 4"
FTR = "www.things-journal.org"


def t_running(tmp):
    print("v3.2: running headers/footers are skipped by find, body hits unchanged")
    words = ["alpha", "bravo", "charlie", "delta"]
    pages = [f"{HDR.format(n=i)}\n\nBody paragraph on {words[i - 1]} Zeolite scaffolds.\n\n{FTR}" for i in range(1, 5)]
    d, _ = paper(tmp, "run", "# T\n\n<!-- p1 -->\n\n## 摘要\n\n文字\n", pages)
    code, out = run("find", d, "Journal of Things|things-journal", "--in", "src")
    ok("NO MATCH" in out, "header/footer lines not returned")
    code, out = run("find", d, "Zeolite", "--in", "src", "--per-paper", "9")
    ok("原文 4" in out, "all 4 body hits still returned")
    keys = pt.running_keys(pt.page_paras([(i + 1, t) for i, t in enumerate(pages)]))
    ok(len(keys) == 2, f"two running keys found (got {len(keys)})")
    short = [(1, "Intro text that is long enough to count here."), (2, "Results")]
    ok(not pt.running_keys(short), "2-page document: nothing is a running line")


def t_number_forms(tmp):
    print("v3.2: spelled-out numbers, unit words, decimal commas, 'No.123', exponents are not false alarms")
    src = ["One milliliter of suspension was added. Four-micrometer-thick sections were cut. Twenty micrograms of "
           "protein were loaded. Mice were exposed for at least 5 minutes. Coated with 0,1 mg mL⁻¹ fibronectin. "
           "Approved by the committee (No.30013). Rate 3.36 mg L⁻¹ min⁻¹ was found. Pores of 150–250 mm were made."]
    body = ("# T\n\n<!-- p1 -->\n\n## 方法\n\n加入 1 mL 悬液。切成 4 μm 厚的切片。上样 20 μg 蛋白。小鼠暴露至少 5 min。"
            "用 0.1 mg/mL 纤连蛋白包被。经委员会批准（编号 30013）。速率为 3.36 mg L−1 min−1。制备 150–250 μm 的孔。\n")
    d, _ = paper(tmp, "forms", body, src)
    code, out = run("check", d)
    vals = re.findall(r"「([^」]+)」", section(out, "组合原文没有") + section(out, "原文中找不到"))
    ok(not {"1 mL", "4 μm", "20 μg", "5 min", "0.1 mg", "30013", "1 min"} & set(vals), f"no false alarms (got {vals})")
    ok("250 μm" in vals, "250 μm vs source 250 mm still reported")
    ok("原文 p1：" in section(out, "组合原文没有") and "250 mm" in section(out, "组合原文没有"),
       "the source occurrence (150–250 mm) is shown next to it")


def t_number_details(tmp):
    print("v3.2: every number warning lists the line with its context")
    src = ["The plates were diluted and counted. Value was 12.34 in group A."]
    body = "# T\n\n<!-- p1 -->\n\n## 方法\n\n将培养物稀释 104 倍后涂板。A 组为 12.34，B 组为 56.78。\n"
    d, _ = paper(tmp, "det", body, src)
    code, out = run("check", d)
    sec = section(out, "原文中找不到")
    ok(re.search(r"L\d+ 「104」 .*稀释 104 倍", sec) is not None, "104 listed with its context")
    ok(re.search(r"L\d+ 「56.78」", sec) is not None, "56.78 listed")
    code, js = run("check", d, "--json")
    data = json.loads(js[js.index("{"):])
    w = data["papers"][0]["warn"]
    ok(data["total"]["warn"] == len(w) and any(x["details"] for x in w), "--json: counts match, details separate")


def t_continued_page(tmp):
    print("v3.2: a page that only continues a paragraph is not reported thin; deleting its text is")
    p2_nums = "Peaks at 953.98, 953.59, 934.74, 934.57, 932.63 and 932.49 eV shifted."
    filler = "Filler words describing the experimental context in detail without numbers. " * 22
    src = ["Alpha study of Zeolite scaffolds measured 12.34 and 56.78 in chambers. " + filler,
           "continued: " + p2_nums + " " + filler,
           "Charlie section reports 11.11 and 22.22 in Wombat implants. " + filler]
    zh_fill = "这是一段没有数字的中文说明文字，" * 60
    body = ("# T\n\n<!-- p1 -->\n\n## 结果\n\n甲研究 Zeolite 支架测得 12.34 与 56.78。" + zh_fill +
            "峰位 953.98、953.59、934.74、934.57、932.63 与 932.49 eV 发生位移。" + zh_fill +
            "\n\n<!-- p2 -->\n\n<!-- p3 -->\n\n丙部分 Wombat 植入物为 11.11 与 22.22。" + zh_fill + "\n")
    d, f = paper(tmp, "cont", body, src, yaml_head='---\ntitle: "T"\ntitle_orig: "T"\ntarget_lang: "zh"\nanchors: "checked"\n---\n')
    code, out = run("check", d)
    ok("可能漏译" not in out, "continuation page p2 credited")
    t = open(f, encoding="utf-8").read().replace("峰位 953.98、953.59、934.74、934.57、932.63 与 932.49 eV 发生位移。", "")
    open(f, "w", encoding="utf-8").write(t)
    code, out = run("check", d)
    ok(re.search(r"p2（.*独有的 6 个数字", out) is not None, "after deleting its sentence p2 is reported, with the reason")


def t_key_result_hint(tmp):
    print("v3.2: key_result without [pN] gets a page suggestion from its numbers")
    src = ["Intro without data.", "The release reached 45.6% and 67.8% at day 7."]
    head = '---\ntitle: "T"\ntitle_orig: "T"\ntarget_lang: "zh"\nkey_result: "第 7 天释放 45.6% 与 67.8%"\n---\n'
    d, _ = paper(tmp, "kr", "# T\n\n<!-- p1 -->\n\n## 摘要\n\n文字\n\n<!-- p2 -->\n\n释放 45.6% 与 67.8%。\n", src, yaml_head=head)
    code, out = run("check", d)
    ok("第 1 条 [p2]" in out, "suggests [p2]")


def t_pair(tmp):
    print("v3.2: pair shows a source page next to its translation")
    filler = "Long running text that describes the experiment in some detail so that the page has real prose. " * 8
    pages = [f"{HDR.format(n=i)}\n\nSource text of page {i} with value {i}1.5 here, {'xyz' * i}. {filler}" for i in range(1, 5)]
    body = "# T\n\n" + "".join(f"<!-- p{i} -->\n\n第 {i} 页译文，数值 {i}1.5。\n\n" for i in range(1, 4)) + "## 参考文献\n\n1. A ref.\n\n<!-- p4 -->\n"
    d, f = paper(tmp, "pair", body, pages)
    code, out = run("pair", d, "--page", "2")
    ok("Source text of page 2" in out and "第 2 页译文" in out and "Journal of Things" not in out,
       "page 2: source (no running header) + translation")
    ok("第 1 页译文" in out and "p1 起头的段落" in out, "the paragraph starting on the previous page is shown")
    lines = open(f, encoding="utf-8").read().split("\n")
    ln = next(i + 1 for i, x in enumerate(lines) if "第 3 页译文" in x)
    code, out = run("pair", d, "--line", str(ln))
    ok("· p3 ==" in out, "--line maps to the page of that line")
    code, a1 = run("pair", d, "--sample", "2")
    code, a2 = run("pair", d, "--sample", "2")
    heads = re.findall(r"· p(\d+) ==", a1)
    ok(a1 == a2 and len(heads) == 2 and "4" not in heads, "--sample reproducible, skips the reference page")
    code, out = run("pair", d)
    ok(code != 0, "no mode given: error")


def t_field_values(tmp):
    print("v3.2: _fields.md value column")
    root = os.path.join(tmp, "lib")
    os.makedirs(root)
    open(os.path.join(root, "_fields.md"), "w", encoding="utf-8").write(
        "| 字段 | 填写说明 | 取值 |\n|---|---|---|\n| animal | 本文自己做的体内实验动物 | 大鼠 / 小鼠 / 兔 |\n")
    for name, v in (("good", "大鼠；兔"), ("bad", "SD 大鼠"), ("none", "无")):
        paper(root, name, "# T\n\n<!-- p1 -->\n\n## 摘要\n\n文字\n", ["Text."],
              yaml_head=f'---\ntitle: "T"\ntitle_orig: "T"\ntarget_lang: "zh"\nanimal: "{v}"\n---\n')
    code, out = run("check", root)
    blocks = dict(re.findall(r"\] (\w+)  .*?\n((?:    .*\n?)*)", out))
    ok("取值" in blocks.get("bad", "") and "SD 大鼠" in blocks.get("bad", ""), "SD 大鼠 reported with the allowed values")
    ok("取值" not in blocks.get("good", "") and "取值" not in blocks.get("none", ""), "大鼠；兔 and 无 accepted")


def t_wrap(tmp):
    print("v3.2: wrap breaks long lines at sentence ends, changes no character, backs up, is idempotent")
    para = "".join(f"这是第 {i} 句，内容用于测试折行功能是否正确处理中文句号。" for i in range(80))
    eng = " ".join(f"Sentence {i} is here for the test (value {i}.5)." for i in range(60))
    body = f"# T\n\n<!-- p1 -->\n\n## 正文\n\n{para}\n\n{eng}\n\n| a | b |\n|---|---|\n| {'x' * 1600} | y |\n"
    d, f = paper(tmp, "wrap", body, ["Text."])
    before = open(f, encoding="utf-8").read()
    code, out = run("wrap", d, "--dry-run")
    ok(open(f, encoding="utf-8").read() == before and "将在句末折开 2 行" in out, "--dry-run: reports 2 lines, file unchanged")
    code, out = run("wrap", d)
    after = open(f, encoding="utf-8").read()
    ok(re.sub(r"\s", "", after) == re.sub(r"\s", "", before), "no character changed")
    ok(max(len(x) for x in after.split("\n") if not x.startswith("|")) <= 1500, "no prose line over 1500 left")
    ok("x" * 1600 in after, "table row untouched")
    ok(os.path.isfile(os.path.join(d, "_work", "backup", "wrap.zh.before-wrap.md")), "backup written")
    code, out = run("wrap", d)
    ok("没有可以自动折行" in out and open(f, encoding="utf-8").read() == after, "second run changes nothing")
    lst = "以下为要点说明文字。" * 100 + "1. 第一点内容。" + "继续说明。" * 60
    ok(all(not pt.BLOCK_START_RE.match(x) for x in pt.wrap_line(lst)[1:]), "never starts a wrapped line with '1. '")
    eng = " ".join(f"Sentence {i} is here." for i in range(120))
    d2, f2 = paper(tmp, "wrap2", f"# T\n\n<!-- p1 -->\n\n## 正文\n\n{eng}\n", ["Text."])
    run("wrap", d2)
    code, out = run("check", d2)
    ok("硬折行" not in out, "English lines wrapped at '. ' are not reported as hard wraps")
    code, out = run("check", d)
    ok("超过 2000" not in out, "check no longer reports long lines")


def t_extract_smoke(tmp):
    print("v3.2: extract on a generated PDF (skipped without PyMuPDF)")
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            print("  skip  PyMuPDF not installed")
            return
    pdf = os.path.join(tmp, "gen.pdf")
    doc = fitz.open()
    for i in range(1, 5):
        pg = doc.new_page()
        pg.insert_text((72, 40), f"Journal of Things 2024, 15, 1234   {i} of 4", fontsize=8)
        pg.insert_textbox(fitz.Rect(72, 100, 520, 700), f"Body text of page {i}. " * 40 + f" Value {i}2.75 mg.", fontsize=10)
        pg.insert_text((72, 810), "www.things-journal.org", fontsize=8)
    doc.save(pdf)
    out_dir = os.path.join(tmp, "paper")
    code, out = run("extract", pdf, out_dir)
    src = os.path.join(out_dir, "_work", "source.txt")
    ok(code == 0 and os.path.isfile(src), "extract ran")
    txt = open(src, encoding="utf-8").read() if os.path.isfile(src) else ""
    ok(txt.count("<<<PAGE") == 4 and "32.75" in txt, "4 pages, numbers kept")
    ok(len(pt.running_keys(pt.page_paras(pt.source_pages(out_dir)))) >= 1, "running header recognised in the extracted text")
    # refresh: an old _work is re-extracted from the recorded PDF; the translation is not touched
    man = os.path.join(out_dir, "_work", "manifest.json")
    m = json.load(open(man, encoding="utf-8"))
    m["tool"], m["figures"] = "pdftrans 2.0", [{"name": "fig01", "page": 1, "method": "manual"}]
    json.dump(m, open(man, "w", encoding="utf-8"))
    open(os.path.join(out_dir, "_work", "source.txt"), "w", encoding="utf-8").write("<<<PAGE 1>>>\nold\n")
    tr = os.path.join(out_dir, "paper.zh.md")
    open(tr, "w", encoding="utf-8").write("# T\n\n<!-- p1 -->\n\n正文\n")
    code, out = run("refresh", out_dir, "--dry-run")
    ok("将重提" in out and "old" in open(os.path.join(out_dir, "_work", "source.txt"), encoding="utf-8").read(),
       "refresh --dry-run lists the paper, changes nothing")
    code, out = run("refresh", out_dir)
    m2 = json.load(open(man, encoding="utf-8"))
    ok("[重提]" in out and m2["tool"] != "pdftrans 2.0" and m2["figures"] and m2["figures"][0]["name"] == "fig01",
       "refresh re-extracts and keeps the recorded figures")
    ok(os.path.isfile(os.path.join(out_dir, "_work", "backup", "source.before-refresh.txt"))
       and open(tr, encoding="utf-8").read() == "# T\n\n<!-- p1 -->\n\n正文\n", "old source backed up, translation untouched")
    m2["tool"], m2["pages"] = "pdftrans 2.0", 7
    json.dump(m2, open(man, "w", encoding="utf-8"))
    code, out = run("refresh", out_dir)
    ok("[拒绝]" in out, "a different page count is refused")


def t_code_review_fixes(tmp):
    print("v3.2: code review findings (refresh errors, number forms, running lines, wrap, unit kinds)")
    # refresh: a PDF that cannot be extracted is reported, the next paper is still processed, exit code 1
    for name in ("a_bad", "b_bad"):
        d = os.path.join(tmp, "lib", name)
        os.makedirs(os.path.join(d, "_work"))
        bad = os.path.join(d, "broken.pdf")
        open(bad, "wb").write(b"not a pdf")
        json.dump({"pdf": bad, "pages": 1, "tool": "pdftrans 2.0"}, open(os.path.join(d, "_work", "manifest.json"), "w"))
        open(os.path.join(d, "_work", "source.txt"), "w", encoding="utf-8").write("<<<PAGE 1>>>\nkept\n")
    code, out = run("refresh", os.path.join(tmp, "lib"))
    ok(code == 1 and out.count("[失败]") == 2 and "失败 2 篇" in out, f"both broken PDFs reported, batch finished (rc={code})")
    ok(open(os.path.join(tmp, "lib", "a_bad", "_work", "source.txt"), encoding="utf-8").read().endswith("kept\n"),
       "source.txt of a failed paper untouched")
    # number forms that must not vouch for a wrong translation
    ok(("2", "g") not in pt.src_num_context(pt.augment_source("Two gram-variable isolates were tested."))[0],
       "'Two gram-variable' does not become 2 g")
    ok("4-μm-thick" in pt.augment_source("Four-micrometer-thick sections."), "'Four-micrometer-thick' still 4-μm")
    for s_, want in (("5- micron-thick slices", "5- μm-thick"), ("5 micron-thick slices", "5 μm-thick"),
                     ("two hour-long sessions", "2 h-long")):
        ok(want in pt.augment_source(s_), f"'{s_}' becomes '{want}'")
    ok("10 g" not in pt.augment_source("Ten Gram-positive and 5 Gram-negative strains."), "Gram-positive/negative stay")
    aug = pt.augment_source("as reported [1,2] and in refs 3,4. Coated with 0,1 mg fibronectin.")
    nums = set(pt.NUMCHK_RE.findall(pt.norm_digits(aug)))
    ok("0.1" in nums and not {"1.2", "3.4"} & nums, f"decimal comma only before a unit (got {sorted(nums)})")
    pairs = pt.NUM_UNIT_RE.findall(pt.norm_units("a 10−3 mM solution, 1 × 10−3 mg, 10-30 mM, 5 mg"))
    ok(("3", "mM") not in pairs and ("3", "mg") not in pairs and ("30", "mM") in pairs and ("5", "mg") in pairs,
       f"10−3 is an exponent, 10-30 a range (got {pairs})")
    # v3.2.1: in the source a hyphenated modifier before a number does not hide it
    t = pt.norm_units(pt.augment_source("Scale bar: top-2 mm, bottom-500 μm."))
    got = {(a, m.group(2)) for m in pt.NUM_LIST_UNIT_RE.finditer(t) for a in re.findall(pt._NUM, m.group(1))}
    ok({("2", "mm"), ("500", "μm")} <= got, f"source 'top-2 mm, bottom-500 μm' keeps both (got {got})")
    ok(("5", "%") in pt.src_num_context(pt.augment_source("Glass: 80% SiO2-15% CaO-5% P2O5."))[0],
       "source 'CaO-5% P2O5' keeps 5 %")
    srcs = ["Text. Figure 1. Histology of the defect. Scale bars: top-2 mm, bottom-500 μm."]
    bods = ("# T\n\n<!-- p1 -->\n\n## 结果\n\n正文。\n\n![图 1](figures/fig01.png)\n\n"
            "**图 1** 缺损的组织学图像。标尺：上 2 mm，下 500 μm。\n")
    ds, _ = paper(tmp, "hyphenscale", bods, srcs)
    with_captions(ds, [{"page": 1, "kind": "fig", "num": "1", "text": srcs[0][6:]}], 1, ("fig01",))
    code, out = run("check", ds)
    ok("原图注没有的数字" not in out, "scale bars written 'top-2 mm' in the original caption are not reported")
    # running lines: a plain footer with no digits on every other page is still one
    pages = [(i, f"Body text number {i} " * 20 + ("\n\nPaper RSC Advances" if i % 2 == 0 else "")) for i in range(1, 11)]
    ok(pt.running_keys(pt.page_paras(pages)) == {"paper rsc advances"}, "digit-free footer on 5 of 10 pages found")
    pages = [(i, f"Body text number {i} " * 20 + ("\n\nDay 7 group A" if i % 2 == 0 else "")) for i in range(1, 11)]
    ok(not pt.running_keys(pt.page_paras(pages)), "label with a fixed number on 5 of 10 pages is content")
    # running lines: a label repeated with the same digits on 3 of 5 pages is content
    pages = [(i, f"Body text number {i} " * 20 + ("\n\nDay 7 group A" if i <= 3 else "")) for i in range(1, 6)]
    ok(not pt.running_keys(pt.page_paras(pages)), "repeated figure label is not a running line")
    pages = [(i, f"Body text {i} " * 20 + f"\n\nJ. Things, 2024, 15, {100 + i}") for i in range(1, 6)]
    ok(len(pt.running_keys(pt.page_paras(pages))) == 1, "journal line with a changing page number still found")
    # wrap / hard wraps: abbreviations are not sentence ends
    parts = pt.wrap_line("A" * 1000 + " as shown by Wang et al. (2020) the data. Next " + "B" * 600)
    ok(len(parts) == 2 and parts[0].endswith("the data."), "wrap does not break after 'et al.'")
    ok(pt.hard_wraps(["如 Wang et al.", "(2020) 所述。"]) == [0], "a line broken after 'et al.' is a hard wrap")
    # source snippet: mM is a concentration, not a length
    ss = pt.src_snip("250", {1: "pores of 150–250 mm were made. " + "x " * 40 + "and 250 μM drug"}, 1, "mM")
    ok("250 μM" in ss and "150–250 mm" not in ss, f"same kind of unit preferred (got {ss})")
    # wrap lists an unbreakable paragraph over 1500 characters
    d, f = paper(tmp, "nowrap", "# T\n\n<!-- p1 -->\n\n" + "无句末的长段落" * 230 + "\n", ["Text."])
    code, out = run("wrap", d, "--dry-run")
    ok("找不到合适的句末" in out, "1,610-character paragraph without a sentence end is listed")


def t_review_fixes(tmp):
    print("v3.2: review findings (missing last page, Gram-positive, scale-bar scope, two _fields tables)")
    fill = "Running text without any numbers that describes the study in plain words for the reader. "
    src = [fill * 25, fill * 21, fill * 23]
    zh = "没有数字的中文说明文字，" * 75
    body = f"# T\n\n<!-- p1 -->\n\n## 结果\n\n{zh}\n\n<!-- p2 -->\n\n{zh[:744]}\n\n<!-- p3 -->\n"
    d, _ = paper(tmp, "lastmissing", body, src,
                 yaml_head='---\ntitle: "T"\ntitle_orig: "T"\ntarget_lang: "zh"\nanchors: "checked"\n---\n')
    code, out = run("check", d)
    ok(re.search(r"p3（", out) is not None, "an anchored but empty last page is still reported")
    a = pt.augment_source("Ten gram-positive isolates and Gram-negative ones.")
    ok(("10", "g") not in pt.src_num_context(a)[0], "'Ten gram-positive' does not become 10 g")
    ok(pt.augment_source("two hundred mL") == "two hundred mL", "'two hundred' is left alone")
    f = os.path.join(tmp, "_fields.md")
    open(f, "w", encoding="utf-8").write("| 字段 | 说明 | 备注 |\n|---|---|---|\n| subject | 研究对象 | 必填 |\n\n"
                                          "| 字段 | 说明 | 取值 |\n|---|---|---|\n| animal | 动物 | 大鼠 / 小鼠 |\n")
    ok(pt.parse_field_values(f) == {"animal": ["大鼠", "小鼠"]}, "value column belongs to its own table only")
    ok(dict(pt.parse_fields_file(f))["subject"].endswith("必填"), "first table keeps its third column in the description")
    srcc = ["Text. Figure 1. Histology of the defect at week 4 stained with HE."]
    bodyc = ("# T\n\n<!-- p1 -->\n\n## 结果\n\n正文。\n\n![图 1](figures/fig01.png)\n\n"
             "**图 1** 第 4 周缺损的 HE 染色组织学图像。标尺：100 μm；切片厚度为 30 μm。误差棒 bar 表示 25.5 μm。\n")
    dc, _ = paper(tmp, "scalecap", bodyc, srcc)
    with_captions(dc, [{"page": 1, "kind": "fig", "num": "1", "text": srcc[0][6:]}], 1, ("fig01",))
    code, out = run("check", dc)
    sec = section(out, "原图注没有的数字")
    ok("[标尺] 「100 μm」" in sec, "100 μm after 标尺 tagged")
    ok(re.search(r"「(?:[^」]*、)?30 μm", sec) is not None and "[标尺] 「100 μm、30 μm" not in sec and "25.5 μm" in sec
       and "[标尺] 「25.5" not in sec, "30 μm (words in between) and 'error bar 25.5 μm' not tagged as scale bars")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for t in (t_encoding, t_yaml, t_src_first_paragraph, t_blocks_not_split, t_cross_page, t_moves_on_evidence,
                  t_duplicates_and_meta, t_bom_crlf, t_fields, t_find_misc, t_coverage_table,
                  t_number_context, t_captions, t_glossary_brief,
                  t_running, t_number_forms, t_number_details, t_continued_page, t_key_result_hint, t_pair,
                  t_field_values, t_wrap, t_extract_smoke, t_review_fixes, t_code_review_fixes):
            sub = os.path.join(tmp, t.__name__)
            os.makedirs(sub)
            try:
                t(sub)
            except Exception as exc:  # a crash is a failure, keep going
                ok(False, f"{t.__name__} crashed: {exc!r}")
        t_auto_assign()
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
