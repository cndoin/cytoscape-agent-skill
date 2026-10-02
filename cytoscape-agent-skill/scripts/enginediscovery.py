#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enginediscovery.py —— 跨平台发现 Cytoscape 引擎与 Java 17。

为什么需要它：
    「自带引擎」不是唯一路线。用户机器上很可能**已经装了** Cytoscape，
    或者已经有 JDK 17。强迫所有人先下 580 MB 是错的。本模块负责把已有的
    安装找出来，让 --engine system / --engine auto 真正可用。

设计原则：
    * 不猜路径 —— 覆盖各平台**已知的**安装位置，外加一次性名字过滤后的浅层遍历。
      过滤是必须的：直接递归 C:\\Program Files 会扫上千个目录、耗时以分钟计。
    * 不猜版本 —— Java 版本一律实际执行 `java -version` 读出来，不靠目录名。
    * 发现失败不报错 —— 返回空列表，由调用方决定是提示还是阻断。

仅使用标准库。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

__all__ = [
    "platform_key",
    "probe_java",
    "find_java_installations",
    "find_cytoscape_installations",
    "best_java",
    "best_cytoscape",
    "xvfb_status",
    "display_available",
    "port_in_use",
    "disk_free_bytes",
]

#: 低于这个 major 的 Java 无法运行 Cytoscape 3.10+
MIN_JAVA_MAJOR = 17


def platform_key():
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

_JAVA_VERSION_RE = re.compile(r'version "?(\d+)(?:\.(\d+))?[^"]*"?')


def probe_java(java_exe, timeout=30):
    """执行 java -version 读出真实版本。失败返回 None（不抛异常）。"""
    if not java_exe or not Path(java_exe).exists():
        return None
    try:
        proc = subprocess.run([str(java_exe), "-version"],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:                            # noqa: BLE001
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    m = _JAVA_VERSION_RE.search(text)
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):                # 旧式 "1.8.0_503"
        major = int(m.group(2))
    return {
        "path": str(Path(java_exe).resolve()),
        "major": major,
        "ok_for_cytoscape": major >= MIN_JAVA_MAJOR,
        "raw": text.strip().splitlines()[0] if text.strip() else "",
    }


def _java_home_candidates(home):
    """各平台 JVM 安装根目录下的候选 java 可执行文件。"""
    exe = "java.exe" if os.name == "nt" else "java"
    out = []

    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("ProgramW6432")):
            if not base:
                continue
            for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Amazon Corretto",
                           "Zulu", "BellSoft", "Semeru", "RedHat"):
                root = Path(base) / vendor
                if root.is_dir():
                    out.extend(str(p) for p in root.glob("*/bin/" + exe))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for vendor in ("Programs/Eclipse Adoptium", "Programs/Microsoft",
                           "Programs/Amazon Corretto", "Programs/Zulu"):
                root = Path(local) / vendor
                if root.is_dir():
                    out.extend(str(p) for p in root.glob("*/bin/" + exe))
        # scoop / chocolatey
        out.append(str(Path(home) / "scoop/apps/openjdk/current/bin" / exe))
        out.append("C:/ProgramData/chocolatey/lib/temurin17/tools/bin/" + exe)
        # SDKMAN on Windows（Git Bash 环境）与便携式 JDK
        out.append(str(Path(home) / ".sdkman/candidates/java/current/bin" / exe))
    elif sys.platform == "darwin":
        jvm = Path("/Library/Java/JavaVirtualMachines")
        if jvm.is_dir():
            out.extend(str(p) for p in jvm.glob("*/Contents/Home/bin/" + exe))
        for brew in ("/opt/homebrew/opt", "/usr/local/opt"):
            bp = Path(brew)
            if bp.is_dir():
                out.extend(str(p) for p in bp.glob("openjdk*/bin/" + exe))
        out.append("/Library/Internet Plug-Ins/JavaAppletPlugin.plugin/Contents/Home/bin/java")
    else:
        jvm = Path("/usr/lib/jvm")
        if jvm.is_dir():
            out.extend(str(p) for p in jvm.glob("*/bin/" + exe))
        out.extend(["/usr/bin/java", "/usr/local/bin/java",
                    "/snap/bin/java", "/opt/java/openjdk/bin/java"])
        # sdkman
        sd = Path(home) / ".sdkman/candidates/java"
        if sd.is_dir():
            out.extend(str(p) for p in sd.glob("*/bin/" + exe))

    return out


def find_java_installations(home=None, runtime_dir=None, include_path=True):
    """返回去重后的 Java 安装列表（含版本实测），按 major 降序。

    :param runtime_dir: cyctl 自带 JRE 的根目录（runtime/），优先列出
    """
    home = home or str(Path.home())
    exe = "java.exe" if os.name == "nt" else "java"
    seen, out = set(), []

    def add(path, source):
        if not path:
            return
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            return
        seen.add(key)
        info = probe_java(path)
        if info:
            info["source"] = source
            out.append(info)

    # ① cyctl 自带（版本可控，最优先）
    if runtime_dir:
        jre = Path(runtime_dir) / "jre"
        add(jre / "bin" / exe, "cyctl-bundled")
        for sub in sorted(jre.glob("*/bin/" + exe)):
            add(sub, "cyctl-bundled")

    # ② JAVA_HOME
    jh = os.environ.get("JAVA_HOME")
    if jh:
        add(Path(jh) / "bin" / exe, "JAVA_HOME")

    # ③ 各平台已知位置
    for cand in _java_home_candidates(home):
        add(cand, "known-location")

    # ④ PATH
    if include_path:
        add(shutil.which("java"), "PATH")

    out.sort(key=lambda d: (-d["major"], d["source"]))
    return out


def best_java(home=None, runtime_dir=None, min_major=MIN_JAVA_MAJOR):
    """挑选最合适的 Java：优先满足 min_major，且偏好 cyctl 自带（版本可控）。"""
    installs = find_java_installations(home=home, runtime_dir=runtime_dir)
    ok = [i for i in installs if i["major"] >= min_major]
    if not ok:
        return None
    ok.sort(key=lambda d: (0 if d["source"] == "cyctl-bundled" else 1, -d["major"]))
    return ok[0]


# ---------------------------------------------------------------------------
# Cytoscape
# ---------------------------------------------------------------------------

def _launcher_names():
    if os.name == "nt":
        return ["cytoscape.bat", "Cytoscape.bat"]
    return ["cytoscape.sh", "Cytoscape.sh"]


def _walk_limited(root, targets, max_depth=5):
    """在 root 下做有深度上限的遍历，找 targets 里的文件名。"""
    root = Path(root)
    if not root.is_dir():
        return []
    hits = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(d))
        except (OSError, PermissionError):
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and e.name in targets:
                    hits.append(str(Path(e.path)))
            except OSError:
                continue
    return sorted(hits)


def _name_matches(name, patterns):
    low = name.lower()
    return any(p in low for p in patterns)


def find_cytoscape_installations(home=None, runtime_dir=None, extra_roots=None):
    """发现所有可用的 Cytoscape 启动脚本。

    返回 [{launcher, dir, source, version_hint, platform}]
    """
    home = home or str(Path.home())
    plat = platform_key()
    targets = set(_launcher_names())
    out, seen = [], set()

    def add(launcher, source):
        if not launcher:
            return
        p = Path(launcher)
        key = os.path.normcase(os.path.normpath(str(p)))
        if key in seen:
            return
        seen.add(key)
        out.append({
            "launcher": str(p),
            "dir": str(p.parent),
            "source": source,
            "version_hint": _version_hint(str(p)),
            "platform": plat,
            "executable": os.access(str(p), os.X_OK),
        })

    # ① cyctl 自带
    if runtime_dir:
        for h in _walk_limited(Path(runtime_dir) / "cytoscape", targets, 4):
            add(h, "cyctl-bundled")

    # ② 各平台已知根目录：先按名字过滤顶层目录，再浅遍历（性能关键）
    roots, name_patterns = [], []
    if plat == "windows":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA"), "C:/"):
            if base:
                roots.append(Path(base) if base != "C:/" else Path("C:/"))
        roots.append(Path(home) / "scoop/apps")
        name_patterns = ["cytoscape"]
    elif plat == "darwin":
        roots = [Path("/Applications"), Path(home) / "Applications", Path("/opt")]
        name_patterns = ["cytoscape"]
    else:
        roots = [Path("/opt"), Path("/usr/local"), Path("/usr/share"),
                 Path("/snap"), Path(home), Path("/flatpak")]
        name_patterns = ["cytoscape"]

    for root in roots + [Path(r) for r in (extra_roots or [])]:
        if not root.is_dir():
            continue
        try:
            entries = list(os.scandir(root))
        except (OSError, PermissionError):
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            if not _name_matches(e.name, name_patterns):
                continue
            for h in _walk_limited(Path(e.path), targets, 5):
                add(h, "known-location")

    # ③ PATH
    for name in ("cytoscape", "Cytoscape"):
        w = shutil.which(name)
        if w:
            add(w, "PATH")

    return out


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _version_hint(path):
    m = _VERSION_RE.search(str(path))
    return m.group(1) if m else None


def best_cytoscape(home=None, runtime_dir=None, prefer_bundled=True):
    """挑选最合适的 Cytoscape 安装。

    默认偏好自带（版本锁定 = 结果可复现），除非显式要求 system。
    """
    installs = find_cytoscape_installations(home=home, runtime_dir=runtime_dir)
    if not installs:
        return None
    if prefer_bundled:
        bundled = [i for i in installs if i["source"] == "cyctl-bundled"]
        if bundled:
            return bundled[0]
    return installs[0]


# ---------------------------------------------------------------------------
# 显示环境 / 系统探测
# ---------------------------------------------------------------------------

def display_available():
    """当前进程是否有可用的图形显示。"""
    plat = platform_key()
    if plat == "windows":
        # Windows 上 GUI 进程需要会话；CI 里通常也是有的（除非跑在无桌面的 service 里）
        return bool(os.environ.get("SESSIONNAME")) or True
    if plat == "darwin":
        return True                              # macOS 总有 WindowServer
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def xvfb_status():
    """探测虚拟显示方案（Linux 无头必需）。"""
    xvfb_run = shutil.which("xvfb-run")
    xvfb = shutil.which("Xvfb")
    if xvfb_run and xvfb:
        return {"available": True, "xvfb_run": xvfb_run, "xvfb": xvfb, "method": "xvfb-run"}
    if xvfb:
        return {"available": True, "xvfb_run": None, "xvfb": xvfb, "method": "Xvfb"}
    return {
        "available": False, "xvfb_run": None, "xvfb": None,
        "hint": "Debian/Ubuntu: apt-get install -y xvfb ；"
                "RHEL/CentOS: yum install -y xorg-x11-server-Xvfb",
    }


def port_in_use(host="127.0.0.1", port=1234, timeout=1.0):
    """端口是否已被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


def disk_free_bytes(path):
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None
