#!/usr/bin/env python3
"""Install the pdf-paper-translation skill (cross-platform, no PowerShell needed).

Usage:
  python install.py --for claude          -> ~/.claude/skills/pdf-paper-translation
  python install.py --for dsh             -> ~/.dsh/skills/pdf-paper-translation
  python install.py --target <skills dir>/pdf-paper-translation
  add --deps to also pip-install pymupdf and pillow
  python install.py --zip                 -> build pdf-paper-translation.zip (for uploading in Claude apps)

Upgrading = run it again: files are overwritten in place, stale files from older versions removed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

NAME = "pdf-paper-translation"
HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = ["SKILL.md", "README.md", "CHANGELOG.md", "LICENSE", "requirements.txt", "install.py", "scripts", "references"]
PRESETS = {"claude": os.path.join("~", ".claude", "skills", NAME), "dsh": os.path.join("~", ".dsh", "skills", NAME)}


def copy(target: str) -> None:
    target = os.path.abspath(os.path.expanduser(target))
    if os.path.basename(target) != NAME:
        print(f"note: 目录名不是 {NAME}；部分平台要求目录名与 SKILL.md 的 name 一致")
    if os.path.abspath(HERE) == target:
        print("源目录就是目标目录，无需复制")
        return
    os.makedirs(target, exist_ok=True)
    for item in PAYLOAD:
        src, dst = os.path.join(HERE, item), os.path.join(target, item)
        if not os.path.exists(src):
            continue
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)  # replace, never nest (scripts/scripts)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    print(f"已安装到 {target}")


def make_zip() -> None:
    out = os.path.join(os.path.dirname(HERE), f"{NAME}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in PAYLOAD:
            src = os.path.join(HERE, item)
            if os.path.isfile(src):
                z.write(src, f"{NAME}/{item}")
            elif os.path.isdir(src):
                for root, dirs, files in os.walk(src):
                    dirs[:] = [d for d in dirs if d != "__pycache__"]
                    for f in files:
                        if f.endswith(".pyc"):
                            continue
                        p = os.path.join(root, f)
                        z.write(p, f"{NAME}/" + os.path.relpath(p, HERE).replace(os.sep, "/"))
    print(f"已打包 {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install the pdf-paper-translation skill")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--for", dest="preset", choices=sorted(PRESETS))
    g.add_argument("--target")
    ap.add_argument("--deps", action="store_true", help="pip install -r requirements.txt")
    ap.add_argument("--zip", action="store_true", help=f"build {NAME}.zip next to this folder")
    a = ap.parse_args()
    if not (a.preset or a.target or a.zip or a.deps):
        ap.print_help()
        return 1
    if a.preset or a.target:
        copy(a.target or PRESETS[a.preset])
    if a.zip:
        make_zip()
    if a.deps:
        r = subprocess.call([sys.executable, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt")])
        if r:
            print("依赖安装失败，请手动执行：pip install pymupdf pillow")
            return r
    try:
        import pymupdf  # noqa: F401
        import PIL  # noqa: F401
        print("依赖检查：pymupdf、pillow 已就绪")
    except ImportError as exc:
        print(f"依赖检查：缺少 {exc.name}，请运行 python install.py --deps 或 pip install pymupdf pillow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
