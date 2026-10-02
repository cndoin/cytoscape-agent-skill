#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AST 提取官方 Python 客户端（py4cytoscape）的公开函数面。

为什么用 AST 而不是 import：
    py4cytoscape 依赖 requests / pandas / chardet，本机没装，`import` 会直接炸。
   AST 只读语法树、不执行任何一行代码 —— 安全且不依赖运行时环境。

为什么不把 wheel 放进仓库：
    那是第三方分发包，vendoring 会引入许可与体积问题。本脚本按需在本地找，
   找不到就明确告诉你去哪下。**证据文件才是要留档的东西**（函数面清单）。

用法：
    # 先取包（一次性，不进仓库）
    python -m pip download py4cytoscape==1.13.0 --no-deps -d .cache/upstream

    python tools/audit/upstream_client_surface.py
    python tools/audit/upstream_client_surface.py --whl .cache/upstream/py4cytoscape-1.13.0-py3-none-any.whl

退出码：0 提取成功 / 3 找不到 wheel
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # skill 根目录
OUT = ROOT / "evidence" / "py4cytoscape-1.13.0-surface.json"
PINNED = "1.13.0"

#: wheel 的候选搜索位置（按优先级）
SEARCH_DIRS = (
    ROOT / ".cache" / "upstream",
    ROOT.parent / ".cache" / "upstream",
)


def find_wheel(explicit=None):
    """定位 py4cytoscape 的 wheel；找不到返回 None。"""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        hits = sorted(d.glob("py4cytoscape-*.whl"))
        if hits:
            return hits[-1]
    return None


def _is_target(name: str) -> bool:
    """只要包内 py4cytoscape/ 下的 .py，以及顶层 py4cytoscape.py；排除 __init__。"""
    if not name.endswith(".py") or name.endswith("__init__.py"):
        return False
    return name.startswith("py4cytoscape/") or name == "py4cytoscape.py"


def extract(whl: Path):
    """遍历 wheel，返回 {模块名: {function_count, functions, constants}}。"""
    surface = {}
    with zipfile.ZipFile(whl) as z:
        for name in sorted(n for n in z.namelist() if _is_target(n)):
            src = z.read(name).decode("utf-8", "replace")
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                surface[name] = {"error": str(exc)}
                continue
            fns, consts = [], []
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    fns.append({"name": node.name,
                                "args": [a.arg for a in node.args.args]})
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id.isupper():
                            consts.append(t.id)
            surface[name] = {
                "function_count": len(fns),
                "functions": [f["name"] for f in fns],
                "constants": consts,
            }
    return surface


def main(argv=None):
    ap = argparse.ArgumentParser(description="提取 py4cytoscape 公开函数面")
    ap.add_argument("--whl", default=None, help="wheel 路径（默认自动搜索）")
    ap.add_argument("--version", default=PINNED, help="期望版本（默认 %(default)s）")
    ap.add_argument("--out", default=str(OUT), help="证据输出路径")
    args = ap.parse_args(argv)

    whl = find_wheel(args.whl)
    if whl is None:
        print("[audit] 找不到 py4cytoscape wheel。先下载：", file=sys.stderr)
        print("        python -m pip download py4cytoscape==%s --no-deps "
              "-d %s" % (args.version, SEARCH_DIRS[0]), file=sys.stderr)
        return 3

    print("[audit] wheel: %s" % whl)
    surface = extract(whl)
    total = sum(v.get("function_count", 0) for v in surface.values())

    # 文件名里的版本要与 --version 一致，否则证据会被张冠李戴
    if args.version not in whl.name:
        print("[audit] 警告：wheel 文件名 %r 里不含期望版本 %r —— "
              "请确认证据标注的版本号。" % (whl.name, args.version), file=sys.stderr)

    print("[audit] 模块数: %d" % len(surface))
    print("[audit] 公开函数总数: %d" % total)
    for name, v in sorted(surface.items(), key=lambda x: -x[1].get("function_count", 0)):
        print("  %-42s %3d" % (name.replace("py4cytoscape/", ""),
                               v.get("function_count", 0)))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "package": "py4cytoscape",
        "version": args.version,
        "source_wheel": whl.name,
        "extraction": "AST（不执行代码）",
        "module_count": len(surface),
        "function_total": total,
        "modules": surface,
    }
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("[audit] 已写入: %s (%d 字节)" % (out, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
