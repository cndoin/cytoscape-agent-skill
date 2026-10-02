#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcpbridge.py —— stdio ↔ Streamable HTTP 的 MCP 桥。

为什么需要它：
    Cytoscape 官方 MCP 服务只讲 **Streamable HTTP**（挂在 CyREST 端口上的 /mcp）。
    但相当一部分宿主只会讲 **stdio** —— 最典型的是 Claude Desktop：官方为此专门
    发了一个 .mcpb 扩展，里面就是一个小桥。本模块用零依赖的方式提供同样的桥，
    于是「任何 MCP 客户端」都能接上，不必先下载官方扩展。

协议要点（MCP stdio transport）：
    * stdin  逐行读取 JSON-RPC 2.0 消息（换行分隔，不得内嵌裸换行）
    * stdout 逐行写出 JSON-RPC 响应；**不得输出任何非协议内容**
    * stderr 放人类日志

协议要点（MCP Streamable HTTP）：
    * POST JSON-RPC 到端点，`Accept: application/json, text/event-stream`
    * 响应可能是 application/json（单个响应），也可能是 text/event-stream（SSE 流）
    * 通知/响应类消息返回 202 Accepted，无 body
    * 服务端可在 initialize 响应里回 `Mcp-Session-Id` 头，后续请求必须带上；
      否则会话丢失 —— 这是最常见的「初始化成功但工具列表为空」的成因
    * initialize 之后，后续请求应带 `MCP-Protocol-Version` 头

并发模型：
    initialize 同步处理（必须先拿到会话 id）；其余请求各起一个线程并发转发，
    避免客户端流水线请求时互相阻塞。写 stdout 时加锁，保证消息不交错。

仅使用标准库。
"""

from __future__ import annotations

import json
import ssl
import sys
import threading
import urllib.error
import urllib.request

__all__ = [
    "BridgeError",
    "parse_sse",
    "probe_manifest",
    "run_bridge",
    "build_opener",
]

#: 仅需 JSON 响应时的 Accept。MCP 服务端可能两种都返回，所以两个都声明。
ACCEPT = "application/json, text/event-stream"


class BridgeError(RuntimeError):
    """桥自身的错误（不是远端协议错误）。"""


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------

def build_opener(insecure=False):
    """构造 opener。

    与 cyrest.py 同样的理由：MCP 端点在 localhost，必须**绕过环境代理**。
    本机若设了 http_proxy，urllib 会把 127.0.0.1 的请求也发给代理，
    代理回 502/407，表现成「引擎在跑但连不上」，极难定位。
    """
    handlers = [urllib.request.ProxyHandler({})]
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------

def parse_sse(text):
    """从 SSE 文本里抽出所有 data 负载（按事件聚合，多行 data 以换行拼接）。

    SSE 规范：以空行分隔事件；同一事件内多行 `data:` 用换行连接。
    """
    events, buf = [], []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            if buf:
                events.append("\n".join(buf))
                buf = []
            continue
        if line.startswith(":"):        # 注释/心跳
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            buf.append(value)
    if buf:
        events.append("\n".join(buf))
    return events


def _extract_messages(content_type, body):
    """从一次 HTTP 响应里抽出所有 JSON-RPC 消息（dict 列表）。"""
    msgs = []
    if "text/event-stream" in (content_type or ""):
        for payload in parse_sse(body):
            payload = payload.strip()
            if not payload:
                continue
            try:
                msgs.append(json.loads(payload))
            except ValueError:
                # 非 JSON 的 SSE 数据（如 keep-alive）忽略
                continue
    else:
        text = body.strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise BridgeError("服务端返回的不是 JSON: %s" % exc)
            if isinstance(parsed, list):
                msgs.extend(parsed)
            else:
                msgs.append(parsed)
    return msgs


# ---------------------------------------------------------------------------
# 探测（供 doctor 用）
# ---------------------------------------------------------------------------

def probe_manifest(base_url, timeout=10, insecure=False):
    """访问 /mcp/manifest 获取人类可读的工具目录。

    官方文档说明该端点返回 Markdown 格式的完整能力清单 —— 这是判断
    「MCP App 是否已安装并生效」最直接的证据（比只看端口通不通强得多）。
    """
    opener = build_opener(insecure)
    url = base_url.rstrip("/") + "/mcp/manifest"
    req = urllib.request.Request(url, headers={"Accept": "text/markdown, text/plain, */*"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return {"ok": True, "url": url, "status": resp.status,
                    "content_type": resp.headers.get("Content-Type"),
                    "manifest": resp.read().decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "status": exc.code,
                "error": "HTTP %d" % exc.code,
                "hint": "404 通常意味着 Cytoscape 里没有安装/启用 MCP App。"}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc),
                "hint": "连不上。确认 Cytoscape 正在运行，且 CyREST 端口正确。"}


# ---------------------------------------------------------------------------
# 桥主体
# ---------------------------------------------------------------------------

class _Writer:
    """串行化 stdout 写入，保证每行一条完整消息、互不交错。"""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, obj):
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


def _log(msg):
    sys.stderr.write("[cyctl-bridge] %s\n" % msg)
    sys.stderr.flush()


class Bridge:
    """一次桥接会话。"""

    def __init__(self, endpoint, *, timeout=120, insecure=False, writer=None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.opener = build_opener(insecure)
        self.session_id = None
        self.protocol_version = None
        self.writer = writer or _Writer(sys.stdout)
        self._threads = []

    # -- HTTP 转发 ---------------------------------------------------------
    def _post(self, message):
        """转发一条消息，返回 (status, headers, body_text)。"""
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": ACCEPT}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        req = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                return resp.status, resp.headers, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, exc.headers, body
        except Exception as exc:                 # noqa: BLE001
            raise BridgeError("无法连接 MCP 端点 %s：%s" % (self.endpoint, exc))

    # -- 单条消息处理 -------------------------------------------------------
    def handle(self, message):
        msg_id = message.get("id")
        is_request = msg_id is not None

        # initialize 同步做：必须先拿到 Mcp-Session-Id，否则后续请求全部失联。
        if message.get("method") == "initialize":
            self._exchange(message)
            return

        if is_request:
            t = threading.Thread(target=self._exchange, args=(message,), daemon=True)
            t.start()
            self._threads.append(t)
        else:
            # 通知（如 notifications/initialized）：转发了事，无响应。
            t = threading.Thread(target=self._notify, args=(message,), daemon=True)
            t.start()
            self._threads.append(t)

    def _exchange(self, message):
        msg_id = message.get("id")
        try:
            status, headers, body = self._post(message)
        except BridgeError as exc:
            self._reply_error(msg_id, -32000, str(exc))
            return

        if status == 202:
            return                                # Accepted，无 body
        if status >= 400:
            # 远端 HTTP 错误 → 回一个 JSON-RPC error，让客户端能显示原因
            self._reply_error(msg_id, -32001,
                              "MCP 端点返回 HTTP %d" % status,
                              data={"body": body[:2000]})
            return
        try:
            msgs = _extract_messages(headers.get("Content-Type"), body)
        except BridgeError as exc:
            self._reply_error(msg_id, -32700, str(exc))
            return
        for m in msgs:
            self._maybe_record_version(m)
            self.writer.send(m)

    def _notify(self, message):
        try:
            self._post(message)
        except BridgeError as exc:
            _log("通知转发失败: %s" % exc)

    def _maybe_record_version(self, msg):
        """从 initialize 结果里记住协议版本，后续请求要带这个头。"""
        if isinstance(msg, dict) and isinstance(msg.get("result"), dict):
            v = msg["result"].get("protocolVersion")
            if v:
                self.protocol_version = v

    def _reply_error(self, msg_id, code, message, data=None):
        if msg_id is None:
            return
        err = {"jsonrpc": "2.0", "id": msg_id,
               "error": {"code": code, "message": message}}
        if data is not None:
            err["error"]["data"] = data
        self.writer.send(err)

    def join(self):
        for t in self._threads:
            t.join(timeout=self.timeout + 5)


def run_bridge(endpoint, *, timeout=120, insecure=False,
               stdin=None, stdout=None):
    """跑桥主循环，直到 stdin EOF。返回退出码。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    writer = _Writer(stdout)
    bridge = Bridge(endpoint, timeout=timeout, insecure=insecure, writer=writer)
    _log("桥接启动：stdio → %s" % endpoint)

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _log("跳过无法解析的行: %s" % exc)
            continue
        if not isinstance(message, dict):
            _log("跳过非对象消息")
            continue
        try:
            bridge.handle(message)
        except Exception as exc:                 # noqa: BLE001
            _log("处理消息失败: %s" % exc)
            bridge._reply_error(message.get("id"), -32603, "桥内部错误: %s" % exc)

    bridge.join()
    _log("stdin 结束，桥退出")
    return 0
