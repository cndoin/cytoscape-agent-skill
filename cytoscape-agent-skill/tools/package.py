#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 cyctl 技能包：产出 dist/ 下的 zip 与 tar.gz，并生成 SHA256SUMS。

用法：
    python tools/package.py                 # 打包
    python tools/package.py --verify        # 打包后解压到临时目录并跑全量测试
    python tools/package.py --out ../dist   # 指定输出目录

为什么需要这个脚本而不是随手 zip 一下 —— 每一条规则都对应一个真实踩过的坑：

  1. **排除 runtime/**。本机实测 runtime/ 已 488 MB（引擎 370 MB + 下载缓存
     348 MB + JRE 117 MB）。打进包里既传不出去，也会让 SHA256SUMS 失去意义。
  2. **逐字节写入，绝不做行尾转换**。Windows 上若用文本模式处理，
     .sh / .bat 与校验清单会被改写成 CRLF，Linux 侧 shebang 变成
     `#!/bin/sh\\r` 直接无法执行；而带哈希清单的项目一旦行尾漂移，
     所有校验会同时失效，且极难定位。
  3. **排除 __pycache__ / *.pyc / *.cyctl-bak / dist**。前者是构建产物，
     后者是 cyctl 改写宿主配置前留的备份，都属"本机状态"，不该分发。
  4. **包内路径统一用正斜杠**（zip 规范要求），保证跨平台解压一致。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # skill 根目录
PKG_NAME = "cytoscape-agent-skill"

#: 打包时排除的路径片段（按目录名匹配）
EXCLUDE_DIRS = {"runtime", "__pycache__", "dist", ".git", ".pytest_cache", "runs"}
#: 排除的文件名后缀
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".cyctl-bak"}
#: 排除的文件名模式
EXCLUDE_RE = re.compile(r"(^\.cyctl-bak$|\.part$|cytoscape\.pid$|cytoscape\.out\.log$)")

#: **允许**保留 CRLF 的文件。判据不是「图方便」，而是「这里的 CRLF 是上游原样字节」。
CRLF_ALLOWED = {
    # CyREST 的 swagger JSON 用**平台行分隔符**序列化：Windows 上实测返回 CRLF
    # （实测头 15 字节 `{\r\n  "swagger"`，6636 个 CR / 6636 个 LF），Linux 上会是 LF。
    # 也就是说这个文件的哈希本来就跨平台不一致 —— 这是上游行为（见 docs/08 §7.8）。
    # 留档要留**原样字节**，不能为了让打包自检好看而改写上游产物。
    #
    # 补充：这个文件的哈希**连同一平台内都不可复现** —— 另起一次会话重取，路径顺序、
    # operationId 去重后缀的归属、`/v1/commands` 描述里的 MIME 字符串都会变
    # （实测差 4 字节，见 docs/08 §7.10）。所以豁免的理由是「保真」而非「可复现」，
    # 不要拿它的 sha256 当回归基准。
    #
    # `.gitattributes` 里也要同步 `-text` 锁字节，否则仓库存的会是被 * text=auto
    # 规范化后的 LF 版本，「原样字节」就成了假话。
    "evidence/engine-swagger-3.10.5.json",
}


def _version():
    """从 scripts/cyctl.py 读 CYCTL_VERSION，保持单一事实源。"""
    src = (ROOT / "scripts" / "cyctl.py").read_text(encoding="utf-8")
    m = re.search(r'^CYCTL_VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else "0.0.0"


def _collect():
    """收集要打包的文件，返回 [(绝对路径, 包内相对路径)]，按路径排序。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if Path(fn).suffix in EXCLUDE_SUFFIX or EXCLUDE_RE.search(fn):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(ROOT).as_posix()
            out.append((full, rel))
    return sorted(out, key=lambda x: x[1])


def _write_zip(files, dest):
    """写 zip：逐条写入原始字节，固定时间戳，保证可复现。"""
    # 固定时间戳 —— 否则同一个包每次打出来哈希都不同，无法比对
    fixed = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full, rel in files:
            info = zipfile.ZipInfo(PKG_NAME + "/" + rel, date_time=fixed)
            info.external_attr = 0o644 << 16
            if rel.endswith(".sh"):
                info.external_attr = 0o755 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, full.read_bytes())


def _write_targz(files, dest, epoch=315532800):
    """写 tar.gz：同样逐字节、固定 mtime。"""
    import gzip
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for full, rel in files:
            data = full.read_bytes()
            ti = tarfile.TarInfo(name="%s/%s" % (PKG_NAME, rel))
            ti.size = len(data)
            ti.mtime = epoch
            ti.mode = 0o755 if rel.endswith(".sh") else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
    with open(dest, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=epoch) as gz:
            gz.write(raw.getvalue())


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _selfcheck(files, archives):
    """打包后自检：归档内容与源文件逐字节一致，且归档内无被排除项。"""
    problems = []
    expect = {rel: full.read_bytes() for full, rel in files}

    with zipfile.ZipFile(archives["zip"]) as zf:
        names = zf.namelist()
        got = {n[len(PKG_NAME) + 1:]: zf.read(n) for n in names if not n.endswith("/")}
    if set(got) != set(expect):
        only_a = sorted(set(expect) - set(got))
        only_b = sorted(set(got) - set(expect))
        problems.append("zip 文件集合不一致: 缺少 %s / 多出 %s" % (only_a[:5], only_b[:5]))
    for rel, data in expect.items():
        if rel in got and got[rel] != data:
            problems.append("zip 内 %s 字节与源文件不一致" % rel)
        if (rel in got and b"\r\n" in data
                and rel.endswith((".sh", ".py", ".md", ".json"))
                and rel not in CRLF_ALLOWED):
            problems.append("%s 含 CRLF（会在 Linux 上引发 shebang 失效）" % rel)

    with tarfile.open(archives["tar.gz"], "r:gz") as tf:
        tnames = tf.getnames()
    tgot = {n[len(PKG_NAME) + 1:] for n in tnames if not n.endswith("/")}
    if tgot != set(expect):
        problems.append("tar.gz 文件集合不一致")

    for rel in got:
        parts = Path(rel).parts
        if any(p in EXCLUDE_DIRS for p in parts):
            problems.append("归档里出现了应排除的目录: %s" % rel)
        if EXCLUDE_RE.search(Path(rel).name):
            problems.append("归档里出现了应排除的文件: %s" % rel)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="打包 cyctl 技能包")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录")
    ap.add_argument("--verify", action="store_true",
                    help="打包后解压到临时目录并跑全量测试")
    args = ap.parse_args(argv)

    ver = _version()
    outdir = Path(args.out).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    files = _collect()
    total = sum(f.stat().st_size for f, _ in files)
    print("[package] 版本 %s，%d 个文件，原始 %.1f KB" % (ver, len(files), total / 1024))

    stem = "%s-%s" % (PKG_NAME, ver)
    zpath = outdir / (stem + ".zip")
    tpath = outdir / (stem + ".tar.gz")
    _write_zip(files, zpath)
    _write_targz(files, tpath)

    archives = {"zip": zpath, "tar.gz": tpath}
    print("[package] %s  %.1f KB" % (zpath.name, zpath.stat().st_size / 1024))
    print("[package] %s  %.1f KB" % (tpath.name, tpath.stat().st_size / 1024))

    problems = _selfcheck(files, archives)
    if problems:
        print("[package] 自检失败：")
        for p in problems:
            print("   -", p)
        return 1
    print("[package] 自检通过：归档内容与源文件逐字节一致，无遗漏与多余")

    lines = ["%s  %s" % (_sha256(p), p.name) for p in (zpath, tpath)]
    sums = outdir / "SHA256SUMS"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("[package] SHA256SUMS:")
    for l in lines:
        print("   ", l)

    if args.verify:
        tmp = Path(tempfile.mkdtemp(prefix="cyctl-verify-"))
        try:
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(tmp)
            root = tmp / PKG_NAME
            print("[verify] 解压到 %s" % root)
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               cwd=str(root), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env, timeout=600)
            tail = (r.stderr or "").strip().splitlines()[-6:]
            print("[verify] 测试输出：")
            for l in tail:
                print("   ", l)
            if r.returncode != 0:
                print("[verify] 失败")
                return 1
            print("[verify] 解压后的包可独立运行测试，通过")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
