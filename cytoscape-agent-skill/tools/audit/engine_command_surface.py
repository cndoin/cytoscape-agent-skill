#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""枚举活引擎的完整命令面（Commands API），落成证据文件。

为什么不用 `cyctl commands <ns>` 来枚举：
   那个子命令为了绕开「无参命令 = 执行」的坑，走的是**只读存在性查询**路径。
   本脚本直接读 help 端点（`Accept: text/plain`），拿到的是引擎自己声明的清单，
   不经过我们任何一层转述 —— 覆盖度审计的事实源就该是上游原话。

事实源：`GET /v1/commands`（命名空间清单）→ `GET /v1/commands/{ns}`（该命名空间命令清单）

用法：
    python tools/audit/engine_command_surface.py            # 默认 http://localhost:1234/v1
    python tools/audit/engine_command_surface.py --base-url http://localhost:1234/v1

退出码：0 全部命名空间枚举成功 / 4 有命名空间枚举失败（引擎未起或端口不对）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # skill 根目录
sys.path.insert(0, str(ROOT / "scripts"))

import cyrest  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:1234/v1"
OUT = ROOT / "evidence" / "engine-command-surface.json"

#: help 类端点只接受 text/plain；发 application/json 会拿到 HTTP 406。
HELP_ACCEPT = "text/plain"
TIMEOUT = 30


def help_text(base_url: str, path: str):
    """请求一个 help 端点，返回 (文本, 状态码)；失败返回 (None, 原因)。

    `cyrest.call_operation` 遇到 4xx/5xx 会抛 CyRestError，所以这里统一兜住。
    """
    try:
        parsed, raw, status = cyrest.call_operation(
            path.lstrip("/"), base_url,
            method="GET", accept=HELP_ACCEPT, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 —— 审计脚本要如实记录任何失败，不能中断
        return None, repr(exc)
    if isinstance(parsed, str):
        return parsed, status
    return (raw if isinstance(raw, str) else json.dumps(parsed)), status


def parse_namespaces(text: str):
    """从 `/commands` 的 help 文本里抽命名空间名。

    真实返回形如：
        Available commands:
          network
          layout
        ...
    缩进 >= 2 个空格的单 token 行即命名空间。
    """
    return re.findall(r"^\s{2,}(\S+)\s*$", text, re.M)


def parse_commands(text: str):
    """从 `/commands/{ns}` 的 help 文本里抽命令名，剥掉表头和空行。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("available"):
            continue
        out.append(line)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="枚举活引擎的命令面")
    ap.add_argument("--base-url", default=DEFAULT_BASE,
                    help="CyREST 基址（默认 %(default)s）")
    ap.add_argument("--out", default=str(OUT), help="证据输出路径")
    args = ap.parse_args(argv)

    base = args.base_url.rstrip("/")
    ns_text, status = help_text(base, "commands")
    if ns_text is None or status != 200:
        print("[audit] 取命名空间失败：status=%s %s" % (status, ns_text), file=sys.stderr)
        print("[audit] 引擎起来了吗？先跑 cyctl start && cyctl wait", file=sys.stderr)
        return 4

    namespaces = parse_namespaces(ns_text)
    print("[audit] 命名空间数: %d" % len(namespaces))

    surface, total, failed = {}, 0, []
    for ns in namespaces:
        text, st = help_text(base, "commands/%s" % ns)
        if text is None:
            surface[ns] = {"error": st, "count": 0, "commands": []}
            failed.append(ns)
            print("  %-20s ERROR %s" % (ns, st))
            continue
        cmds = parse_commands(text)
        surface[ns] = {"count": len(cmds), "commands": cmds}
        total += len(cmds)
        print("  %-20s %3d" % (ns, len(cmds)))

    print("[audit] 命令总数: %d" % total)

    doc = {
        "base_url": base,
        "source": "Commands API help endpoints (Accept: text/plain)",
        "namespace_count": len(namespaces),
        "command_count": total,
        "failed_namespaces": failed,
        "namespaces": surface,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("[audit] 已写入: %s (%d 字节)" % (out, os.path.getsize(out)))
    return 4 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
