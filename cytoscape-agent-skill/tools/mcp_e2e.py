#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端验证 MCP 直连通道（Streamable HTTP）。

为什么需要这个脚本
------------------
cyctl 有三条通道：CLI（CyREST）、stdio 桥、MCP 直连。前两条用 `cyctl run` /
`test_stability.py` 覆盖了；MCP 直连此前只验证到「/mcp/manifest 返回 200」，
那只能证明 App **装上了**，不能证明**协议对话能用**。

这个脚本做真正的协议往返，逐条打印证据：
  1. POST initialize          -> 必须拿到 Mcp-Session-Id 与 protocolVersion
  2. POST notifications/initialized -> 必须 202 且无 body
  3. POST tools/list          -> 必须含命令网关三件套（实测返回 25 个工具；
                                 注意 /mcp/manifest 只文档化了 4 个，
                                 工具清单的事实源是 tools/list）
  4. POST tools/call          -> 调只读工具 command_gateway_get 取命令 schema

只用**只读**工具：command_gateway_get / command_gateway_search 官方标注为
"read-only and does not modify desktop state"。绝不调 command_gateway_invoke
（它是 state-mutating，且能执行任意桌面命令 —— 包括关掉引擎）。

输出：stdout 一个 JSON 对象（与 cyctl 的输出契约一致）。
退出码：0 全通过 / 4 远端错误 / 3 环境未就绪。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mcpbridge  # noqa: E402


def _call(bridge, rid, method, params=None):
    """发一条请求，返回 (status, messages)。"""
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    status, headers, body = bridge._post(msg)
    if status == 202:
        return status, []
    msgs = mcpbridge._extract_messages(headers.get("Content-Type"), body)
    for m in msgs:
        # initialize 的返回里带 protocolVersion，后续请求必须回填这个头
        bridge._maybe_record_version(m)
    return status, msgs


def _notify(bridge, method, params=None):
    """发一条通知（无 id）。服务端应回 202 且无 body。"""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    status, _headers, _body = bridge._post(msg)
    return status


def main():
    ap = argparse.ArgumentParser(description="端到端验证 MCP 直连通道")
    ap.add_argument("--endpoint", default=os.environ.get(
        "CYCTL_MCP_ENDPOINT", "http://localhost:1234/mcp"),
        help="MCP 端点（端口根 + /mcp），默认 %(default)s")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()
    endpoint = args.endpoint

    steps = []
    result = {"endpoint": endpoint, "steps": steps}

    bridge = mcpbridge.Bridge(endpoint, timeout=args.timeout)

    # ---- 1. initialize -------------------------------------------------
    try:
        status, msgs = _call(bridge, 1, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cyctl-mcp-e2e", "version": "1.0.0"},
        })
    except mcpbridge.BridgeError as exc:
        result.update({"ok": False, "kind": "not_ready", "error": str(exc),
                       "hint": "MCP 端点在 http://<host>:<port>/mcp（端口根），"
                               "不是 /v1/mcp。先确认 cyctl status 通。"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    init_result = None
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("result"), dict):
            init_result = m["result"]
    server_info = (init_result or {}).get("serverInfo", {})
    steps.append({
        "step": "initialize",
        "http_status": status,
        "session_id_present": bool(bridge.session_id),
        "protocol_version": (init_result or {}).get("protocolVersion"),
        "server_info": server_info,
        "ok": bool(bridge.session_id) and bool(init_result),
    })
    if not bridge.session_id:
        result.update({"ok": False, "kind": "remote",
                       "error": "initialize 未返回 Mcp-Session-Id —— "
                                "后续请求会全部失联（工具列表为空）"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 4

    # ---- 2. notifications/initialized ----------------------------------
    nstatus = _notify(bridge, "notifications/initialized")
    steps.append({"step": "notifications/initialized", "http_status": nstatus,
                  "ok": nstatus == 202})

    # ---- 3. tools/list -------------------------------------------------
    status, msgs = _call(bridge, 2, "tools/list")
    tools = []
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("result"), dict):
            tools = m["result"].get("tools", []) or []
    names = sorted(t.get("name") for t in tools)
    expected = {"load_cytoscape_network_view", "command_gateway_search",
                "command_gateway_get", "command_gateway_invoke"}
    steps.append({
        "step": "tools/list",
        "http_status": status,
        "count": len(tools),
        "names": names,
        "missing_vs_expected": sorted(expected - set(names)),
        "ok": bool(tools) and not (expected - set(names)),
    })

    # ---- 4. tools/call（只读工具）--------------------------------------
    status, msgs = _call(bridge, 3, "tools/call", {
        "name": "command_gateway_get",
        "arguments": {"commandKeys": ["network list"]},
    })
    call_payload = None
    is_error = None
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("result"), dict):
            r = m["result"]
            is_error = r.get("isError")
            # MCP 工具结果统一放在 content[].text（JSON 字符串）
            for c in (r.get("content") or []):
                if c.get("type") == "text":
                    try:
                        call_payload = json.loads(c["text"])
                    except ValueError:
                        call_payload = c["text"]
                    break
    got_schema = bool(isinstance(call_payload, dict)
                      and call_payload.get("success"))
    steps.append({
        "step": "tools/call command_gateway_get",
        "http_status": status,
        "is_error": is_error,
        "success": (call_payload or {}).get("success")
        if isinstance(call_payload, dict) else None,
        "command_key_returned": [
            x.get("commandKey") for x in
            ((call_payload or {}).get("results") or [])
        ] if isinstance(call_payload, dict) else None,
        "ok": got_schema,
    })

    result["ok"] = all(s.get("ok") for s in steps)
    if not result["ok"]:
        result["kind"] = "remote"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    sys.exit(main())
