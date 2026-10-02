#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 逐入口实跑取证 —— 输出契约的守门人。

判据（`docs/` 里写死的输出契约）：
  - 退出码 ∈ {0,2,3,4,5}（**1 是禁止的**：它意味着异常穿透到了兜底分支）
  - stdout 必须能被 `json.loads` 解析成**一个对象**
  - stdout 里除了那个 JSON 不能有别的东西

每个子命令都跑「正常路径」；能安全跑的再补「错误路径」。
破坏性动作（stop / prune --execute）用**不会伤到当前引擎**的参数探。

关于端口类用例（这里踩过坑，别改回去）：
  `start --port <空闲端口>` 会**真的启动第二个引擎** —— 上一版就因此制造了
  1.47 GB + 1.22 GB 两个 java 进程，用例假失败。要测「端口占用检测」，
  正确做法是传**当前引擎正在用的端口**：它会命中 `fail(EXIT_ENV, "端口已被占用")`
  并直接返回，不产生任何副作用。引擎没起时该用例明确登记为跳过，而不是硬跑。

用法：
    python tools/audit/audit_cli_contract.py

退出码：0 全部通过 / 4 有用例未通过
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # skill 根目录
CLI = ROOT / "scripts" / "cyctl.py"
PY = sys.executable
OUT = ROOT / "evidence" / "cli-entry-audit.json"


def free_port():
    """向系统要一个当前空闲的端口（用完立即释放）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# (标签, argv, 期望退出码集合)
CASES = [
    ("env",                  ["env"],                                           {0}),
    ("describe",             ["describe"],                                      {0}),
    ("doctor",               ["doctor"],                                        {0, 3}),
    ("discover",             ["discover"],                                      {0, 3}),
    ("status",               ["status"],                                        {0, 3}),
    ("wait",                 ["wait", "--timeout", "60"],                       {0, 3}),
    ("cmd 正常",             ["cmd", "network list"],                           {0, 3}),
    ("cmd GET",              ["cmd", "layout get preferred", "--get"],          {0, 3}),
    ("cmd 空串",             ["cmd", ""],                                       {2}),
    ("cmd 全空白",           ["cmd", "   "],                                    {2}),
    ("rest 正常",            ["rest", "GET", "networks"],                       {0, 3}),
    ("rest text/plain",      ["rest", "GET", "commands", "--accept", "text/plain"], {0, 3}),
    ("rest 坏 JSON body",    ["rest", "POST", "networks", "--body", "{oops"],   {2}),
    ("rest 坏 --param",      ["rest", "GET", "networks", "--param", "noequals"], {2}),
    ("rest 空 operation",    ["rest", "GET", ""],                               {0, 3}),
    ("commands 列命名空间",   ["commands"],                                      {0, 3}),
    ("commands 列一ns",      ["commands", "network"],                           {0, 3}),
    ("commands 查一cmd",     ["commands", "network", "export"],                  {0, 3}),
    ("commands 危险命令",     ["commands", "command", "quit"],                   {2}),
    ("commands 执行需参数",   ["commands", "network", "export", "--allow-execute"], {0, 3}),
    ("mcp",                  ["mcp"],                                           {0}),
    ("bridge --probe",       ["bridge", "--probe"],                             {0, 3}),
    ("prune dry-run",        ["prune"],                                         {0}),
    ("run 工作流",            ["run", "workflows/demo.workflow.json"],           {0, 3, 4}),
    ("provision 只下载态",    ["provision", "--component", "jre"],               {0, 5}),
    ("--version",            ["--version"],                                     {0}),
    ("--help",               ["--help"],                                        {0, 2}),
    ("无参数",                [],                                               {2}),
    ("未知子命令",            ["frobnicate"],                                    {2}),
    ("stop 空端口(幂等)",      ["stop", "--port", "{free}"],                      {0}),
    ("start 端口占用检测",     ["start", "--port", "{busy}"],                     {0, 3}),
]


def run(argv):
    t0 = time.time()
    try:
        p = subprocess.run([PY, str(CLI)] + argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"rc": None, "json_obj": False, "extra": "", "note": "TIMEOUT",
                "secs": round(time.time() - t0, 2)}
    secs = round(time.time() - t0, 2)
    out = p.stdout
    rec = {"rc": p.returncode, "json_obj": False, "extra": "", "secs": secs, "note": ""}
    try:
        d = json.loads(out)
        rec["json_obj"] = isinstance(d, dict)
        rec["ok"] = d.get("ok")
        rec["keys"] = len(d)
    except Exception as exc:  # noqa: BLE001
        rec["note"] = "stdout 非单一 JSON: %s" % exc
        rec["extra"] = out[:120]
    if p.returncode == 1:
        rec["note"] = (rec["note"] + " | 退出码=1（违反契约）").strip(" |")
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="CLI 输出契约审计")
    ap.add_argument("--engine-port", type=int, default=1234,
                    help="当前引擎端口（用于端口占用用例，默认 %(default)s）")
    ap.add_argument("--out", default=str(OUT), help="证据输出路径")
    args = ap.parse_args(argv)

    # 必须转成 str：subprocess 的 argv 元素只能是 str/bytes/PathLike，
    # 直接塞 int 会在 list2cmdline 里抛 TypeError（本脚本第一版就踩了这个）。
    fp = str(free_port())
    engine_up = port_open(args.engine_port)
    # 引擎没起时，占用用例会变成「真的启一个新引擎」—— 明确跳过，不硬跑。
    busy = str(args.engine_port) if engine_up else None

    print("[audit] 空闲端口=%s，引擎端口=%d（%s）" % (
        fp, args.engine_port, "在跑" if engine_up else "未起"))
    print()
    print("%-22s %-4s %-6s %-6s %-6s %s" % ("用例", "rc", "JSON?", "ok", "秒", "备注"))
    print("-" * 96)

    fails, skipped = [], []
    for label, argv_tpl, want in CASES:
        if "{busy}" in argv_tpl:
            if busy is None:
                skipped.append({"label": label,
                                "reason": "引擎未运行；为避免真的启动第二个实例，跳过"})
                print("%-22s %-4s %-6s %-6s %-6s %s" % (
                    label, "-", "-", "-", "-", "跳过：引擎未运行"))
                continue
            argv_real = [busy if a == "{busy}" else a for a in argv_tpl]
        else:
            argv_real = [fp if a == "{free}" else a for a in argv_tpl]

        rec = run(argv_real)
        ok_rc = rec["rc"] in want
        good = ok_rc and rec["json_obj"] and rec["rc"] != 1
        if not good:
            fails.append((label, argv_real, rec, want))
        print("%-22s %-4s %-6s %-6s %-6s %s" % (
            label, rec["rc"], rec["json_obj"], rec.get("ok"), rec["secs"],
            "" if good else ("期望 rc∈%s " % sorted(want)) + rec["note"]))

    print("-" * 96)
    print("[audit] 用例 %d，未通过 %d，跳过 %d" % (
        len(CASES), len(fails), len(skipped)))
    for label, argv_real, rec, want in fails:
        print("  FAIL %-20s argv=%s rc=%s want=%s %s" % (
            label, argv_real, rec["rc"], sorted(want), rec["note"]))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "contract": {"exit_codes": [0, 2, 3, 4, 5],
                         "stdout": "恰好一个 JSON 对象"},
            "engine_port": args.engine_port, "engine_was_up": engine_up,
            "cases": [{"label": l, "argv": a, "want": sorted(w)} for l, a, w in CASES],
            "skipped": skipped,
            "failures": [{"label": l, "argv": a, "rc": r["rc"], "note": r["note"],
                          "want": sorted(w)} for l, a, r, w in fails],
        }, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("[audit] 已写入: %s (%d 字节)" % (out, os.path.getsize(out)))
    return 4 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
