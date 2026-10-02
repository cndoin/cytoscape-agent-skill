#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hosts_bridge.py —— 覆盖「全都要」新增的三块能力：

  1. 宿主配置矩阵（hostmatrix）：每个宿主的字段名必须与官方文档一致。
     这是最容易静默失败的地方 —— 字段名写错，配置写进去了、进程也不报错，
     只是宿主永远连不上。所以逐宿主断言**确切的字段名**。
  2. stdio↔HTTP 桥（mcpbridge）：会话头、协议版本头、SSE 解析、通知 202。
  3. 跨平台引擎发现（enginediscovery）。

全部使用标准库，且不依赖真实 Cytoscape 引擎。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cyctl          # noqa: E402
import enginediscovery as edis   # noqa: E402
import hostmatrix as hmat        # noqa: E402
import mcpbridge                 # noqa: E402

PY = sys.executable
CYCTL = str(ROOT / "scripts" / "cyctl.py")


# ---------------------------------------------------------------------------
# 1. 宿主配置矩阵
# ---------------------------------------------------------------------------

class TestHostEntryShapes(unittest.TestCase):
    """逐宿主锁定确切的字段名 —— 抄错字段名 = 宿主永远连不上。"""

    URL = "http://localhost:1234/mcp"

    def _entry(self, host_id):
        style = hmat.entry_style_for(host_id)
        return hmat.render_server_entry(style, url=self.URL, port=1234,
                                        python_exe="/py/python", cyctl_path="/s/cyctl.py")

    def test_claude_code_and_vscode_need_type_http(self):
        # 官方明确：只有 url 而没有 type 的条目会被 Claude Code 当成 stdio 服务器而报错
        for hid in ("claude-code", "vscode", "copilot-cli"):
            e = self._entry(hid)
            self.assertEqual(e, {"type": "http", "url": self.URL}, "%s 条目形状错误" % hid)

    def test_cursor_uses_bare_url(self):
        self.assertEqual(self._entry("cursor"), {"url": self.URL})

    def test_gemini_uses_httpurl_not_url(self):
        # Gemini 里 url=SSE、httpUrl=StreamableHTTP，写错会连到错误的传输
        e = self._entry("gemini-cli")
        self.assertEqual(e, {"httpUrl": self.URL})
        self.assertNotIn("url", e)

    def test_windsurf_uses_serverurl(self):
        self.assertEqual(self._entry("windsurf"), {"serverUrl": self.URL})

    def test_claude_desktop_must_use_stdio_bridge(self):
        # Claude Desktop 只说 stdio，必须给 command/args，不能给 url
        e = self._entry("claude-desktop")
        self.assertIn("command", e)
        self.assertIn("args", e)
        self.assertNotIn("url", e)
        self.assertIn("bridge", e["args"])
        self.assertIn("--port", e["args"])

    def test_vscode_top_level_key_is_servers_not_mcpservers(self):
        cfg, text = hmat.render_file("vscode", url=self.URL, port=1234,
                                     python_exe="/py/python", cyctl_path="/s/cyctl.py",
                                     project_dir="/proj")
        self.assertEqual(cfg["key"], "servers", "VS Code 原生格式的顶层键必须是 servers")
        body = json.loads(text)
        self.assertIn("servers", body)
        self.assertNotIn("mcpServers", body)

    def test_other_hosts_use_mcpservers(self):
        for hid in ("claude-code", "cursor", "gemini-cli", "generic-mcp"):
            cfg, text = hmat.render_file(hid, url=self.URL, port=1234,
                                         python_exe="/py/python", cyctl_path="/s/cyctl.py",
                                         project_dir="/proj")
            self.assertEqual(cfg["key"], "mcpServers", hid)
            self.assertIn("mcpServers", json.loads(text))

    def test_codex_is_toml_in_mcp_servers_section(self):
        cfg, text = hmat.render_file("codex", url=self.URL, port=1234,
                                     python_exe="/py/python", cyctl_path="/s/cyctl.py")
        self.assertEqual(cfg["format"], "toml")
        self.assertEqual(cfg["key"], "mcp_servers")
        # TOML 点号会被解析成嵌套表，所以 server id 用下划线
        self.assertIn("[mcp_servers.cytoscape_mcp]", text)
        self.assertIn('url = "%s"' % self.URL, text)

    def test_cli_commands_contain_url(self):
        for hid in ("claude-code", "vscode", "copilot-cli", "codex", "gemini-cli"):
            cmd = hmat.cli_command_for(hid, url=self.URL)
            self.assertIsNotNone(cmd, hid)
            self.assertIn(self.URL, cmd, hid)

    def test_unknown_host_raises(self):
        with self.assertRaises(KeyError):
            hmat.entry_style_for("no-such-host")

    def test_all_hosts_have_verified_doc_source(self):
        for h in hmat.HOSTS:
            self.assertTrue(h.get("doc"), "%s 缺少文档来源，等于没依据" % h["id"])
            self.assertIn(h["transport"], ("http", "stdio"))


class TestDedicatedConfig(unittest.TestCase):
    """混合配置文件绝不能被自动改写。"""

    def test_mixed_files_detected(self):
        for p in ("/h/.claude.json", "/h/.codex/config.toml", "/h/.gemini/settings.json"):
            self.assertFalse(hmat.is_dedicated_config(p), p)

    def test_dedicated_files_detected(self):
        for p in ("/p/.mcp.json", "/h/.cursor/mcp.json", "/h/.vscode/mcp.json",
                  "/h/.copilot/mcp-config.json", "/h/.codeium/windsurf/mcp_config.json"):
            self.assertTrue(hmat.is_dedicated_config(p), p)


class TestConfigWriteSafety(unittest.TestCase):
    """写入用户配置必须保守：备份、合并、幂等、拒绝破坏。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cyctl-host-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.entry = {"type": "http", "url": "http://localhost:1234/mcp"}

    def test_creates_new_file(self):
        p = self.tmp / ".mcp.json"
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp",
                              entry=self.entry)
        self.assertEqual(r["action"], "created")
        body = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(body["mcpServers"]["cytoscape-mcp"], self.entry)

    def test_merge_preserves_existing_servers(self):
        p = self.tmp / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {"other": {"url": "http://x"}}}),
                     encoding="utf-8")
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp",
                              entry=self.entry)
        self.assertEqual(r["action"], "merged")
        body = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("other", body["mcpServers"], "合并不得丢掉已有条目")
        self.assertIn("cytoscape-mcp", body["mcpServers"])

    def test_backup_is_created(self):
        p = self.tmp / ".mcp.json"
        p.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp",
                              entry=self.entry)
        self.assertTrue(r.get("backup"), "改写已有文件必须先备份")
        self.assertTrue(Path(r["backup"]).exists())

    def test_idempotent(self):
        p = self.tmp / ".mcp.json"
        hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp", entry=self.entry)
        before = p.read_text(encoding="utf-8")
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp", entry=self.entry)
        self.assertEqual(r["action"], "unchanged")
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_refuses_mixed_config(self):
        p = self.tmp / ".claude.json"
        p.write_text(json.dumps({"projects": {}, "history": [1, 2, 3]}), encoding="utf-8")
        before = p.read_text(encoding="utf-8")
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp", entry=self.entry)
        self.assertEqual(r["action"], "refused")
        self.assertEqual(p.read_text(encoding="utf-8"), before, "被拒绝时不得改动文件")

    def test_refuses_invalid_json(self):
        p = self.tmp / ".mcp.json"
        p.write_text("{ this is not json", encoding="utf-8")
        before = p.read_text(encoding="utf-8")
        r = hmat.write_config(str(p), key="mcpServers", name="cytoscape-mcp", entry=self.entry)
        self.assertEqual(r["action"], "refused")
        self.assertEqual(p.read_text(encoding="utf-8"), before)

    def test_toml_appends_section_and_is_idempotent(self):
        p = self.tmp / "config.toml"
        p.write_text('model = "gpt-5"\n', encoding="utf-8")
        r = hmat.write_config(str(p), key="mcp_servers", name="cytoscape-mcp",
                              entry={"url": "http://localhost:1234/mcp"},
                              fmt="toml", allow_mixed=True)
        self.assertEqual(r["action"], "merged")
        text = p.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5"', text, "不得丢掉原有配置")
        self.assertIn("[mcp_servers.cytoscape_mcp]", text)
        r2 = hmat.write_config(str(p), key="mcp_servers", name="cytoscape-mcp",
                               entry={"url": "http://localhost:1234/mcp"},
                               fmt="toml", allow_mixed=True)
        self.assertEqual(r2["action"], "unchanged")
        self.assertEqual(p.read_text(encoding="utf-8").count("[mcp_servers.cytoscape_mcp]"), 1,
                         "重复运行不得产生重复的 TOML 节（那会是非法的 TOML）")


# ---------------------------------------------------------------------------
# 2. MCP 桥
# ---------------------------------------------------------------------------

class TestSseParsing(unittest.TestCase):

    def test_single_event(self):
        self.assertEqual(mcpbridge.parse_sse('data: {"a":1}\n\n'), ['{"a":1}'])

    def test_multiline_data_joined(self):
        self.assertEqual(mcpbridge.parse_sse("data: line1\ndata: line2\n\n"),
                         ["line1\nline2"])

    def test_comments_ignored(self):
        self.assertEqual(mcpbridge.parse_sse(": keep-alive\n\ndata: x\n\n"), ["x"])

    def test_event_without_trailing_blank_line(self):
        self.assertEqual(mcpbridge.parse_sse("data: y"), ["y"])

    def test_extract_messages_from_json(self):
        msgs = mcpbridge._extract_messages("application/json", '{"jsonrpc":"2.0","id":1}')
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["id"], 1)

    def test_extract_messages_from_sse(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":2}\n\n'
        msgs = mcpbridge._extract_messages("text/event-stream", body)
        self.assertEqual(msgs[0]["id"], 2)

    def test_non_json_sse_ignored_not_crashing(self):
        body = "data: not-json\n\ndata: {\"id\":3}\n\n"
        msgs = mcpbridge._extract_messages("text/event-stream", body)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["id"], 3)


class _FakeMcpHandler(BaseHTTPRequestHandler):
    """极简 MCP Streamable HTTP 服务端，用来验证桥的行为。"""

    seen = []

    def log_message(self, *a):     # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).seen.append({
            "method": msg.get("method"),
            "id": msg.get("id"),
            "session": self.headers.get("Mcp-Session-Id"),
            "protocol": self.headers.get("MCP-Protocol-Version"),
            "accept": self.headers.get("Accept"),
        })
        method = msg.get("method")
        if method == "initialize":
            body = json.dumps({
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "serverInfo": {"name": "fake", "version": "0"}},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "SESSION-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif method == "tools/list":
            payload = json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"tools": [{"name": "t1"}]}})
            sse = ("event: message\ndata: %s\n\n" % payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse)))
            self.end_headers()
            self.wfile.write(sse)
        else:
            self.send_response(202)          # 通知：Accepted，无 body
            self.end_headers()


class TestBridgeRoundTrip(unittest.TestCase):

    def setUp(self):
        _FakeMcpHandler.seen = []
        self.srv = HTTPServer(("127.0.0.1", 0), _FakeMcpHandler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

        def _down():
            self.srv.shutdown()
            self.srv.server_close()
        self.addCleanup(_down)

    def _run(self, messages):
        stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
        stdout = io.StringIO()
        rc = mcpbridge.run_bridge("http://127.0.0.1:%d/mcp" % self.port,
                                  stdin=stdin, stdout=stdout)
        lines = [l for l in stdout.getvalue().strip().split("\n") if l]
        return rc, [json.loads(l) for l in lines]

    def test_round_trip_and_session_propagation(self):
        rc, out = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(rc, 0)
        self.assertEqual([m.get("id") for m in out], [1, 2],
                         "通知不得产生响应，两个请求各得一个响应")
        self.assertEqual(out[1]["result"]["tools"][0]["name"], "t1",
                         "SSE 响应必须被解析并转成协议消息")

        seen = {s["method"]: s for s in _FakeMcpHandler.seen}
        self.assertIsNone(seen["initialize"]["session"], "首次请求不应带会话头")
        self.assertEqual(seen["tools/list"]["session"], "SESSION-1",
                         "后续请求必须带上 initialize 返回的会话头，否则会话丢失")
        self.assertEqual(seen["tools/list"]["protocol"], "2025-06-18",
                         "后续请求必须带 MCP-Protocol-Version")
        self.assertIn("text/event-stream", seen["tools/list"]["accept"],
                      "必须声明接受 SSE，否则服务端可能不回流式响应")

    def test_notification_gets_202_and_no_output(self):
        _, out = self._run([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(out, [], "202 通知不应产生任何 stdout 输出")

    def test_stdout_is_pure_protocol(self):
        _, out = self._run([{"jsonrpc": "2.0", "id": 7, "method": "initialize"}])
        for m in out:
            self.assertIn("jsonrpc", m, "stdout 上只能有 JSON-RPC 消息")
            self.assertEqual(m["jsonrpc"], "2.0")

    def test_unreachable_endpoint_yields_jsonrpc_error(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n")
        stdout = io.StringIO()
        mcpbridge.run_bridge("http://127.0.0.1:1/mcp", stdin=stdin, stdout=stdout)
        lines = [l for l in stdout.getvalue().strip().split("\n") if l]
        self.assertEqual(len(lines), 1)
        err = json.loads(lines[0])
        self.assertEqual(err["id"], 9, "连接失败必须回一个同 id 的 JSON-RPC error")
        self.assertIn("error", err)


# ---------------------------------------------------------------------------
# 3. 引擎发现
# ---------------------------------------------------------------------------

class TestEngineDiscovery(unittest.TestCase):

    def test_probe_java_missing_returns_none(self):
        self.assertIsNone(edis.probe_java("/definitely/not/here/java"))

    def test_platform_key_is_known(self):
        self.assertIn(edis.platform_key(), ("windows", "linux", "darwin"))

    def test_walk_limited_finds_targets(self):
        with tempfile.TemporaryDirectory() as d:
            deep = Path(d) / "a" / "b" / "c"
            deep.mkdir(parents=True)
            (deep / "cytoscape.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            hits = edis._walk_limited(d, {"cytoscape.sh"}, max_depth=4)
            self.assertEqual(len(hits), 1)

    def test_walk_limited_respects_depth(self):
        with tempfile.TemporaryDirectory() as d:
            deep = Path(d) / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "cytoscape.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(edis._walk_limited(d, {"cytoscape.sh"}, max_depth=2), [])

    def test_finds_bundled_installation(self):
        with tempfile.TemporaryDirectory() as d:
            rt = Path(d) / "runtime"
            name = "cytoscape.bat" if os.name == "nt" else "cytoscape.sh"
            (rt / "cytoscape" / "bin").mkdir(parents=True)
            (rt / "cytoscape" / "bin" / name).write_text("x", encoding="utf-8")
            found = edis.find_cytoscape_installations(runtime_dir=str(rt))
            mine = [f for f in found if f["source"] == "cyctl-bundled"]
            self.assertEqual(len(mine), 1)
            self.assertTrue(mine[0]["launcher"].endswith(name))

    def test_version_hint_extracted_from_path(self):
        self.assertEqual(edis._version_hint("/opt/Cytoscape_v3.10.5/cytoscape.sh"), "3.10.5")
        self.assertIsNone(edis._version_hint("/opt/cytoscape/cytoscape.sh"))

    def test_xvfb_status_shape(self):
        st = edis.xvfb_status()
        self.assertIn("available", st)
        if not st["available"]:
            self.assertIn("hint", st, "不可用时必须给出安装指引")

    def test_disk_free_returns_number_or_none(self):
        v = edis.disk_free_bytes(".")
        self.assertTrue(v is None or isinstance(v, int))


# ---------------------------------------------------------------------------
# 4. 新子命令的 CLI 契约
# ---------------------------------------------------------------------------

class TestNewCliContract(unittest.TestCase):
    """新子命令同样必须遵守「stdout 恒为一个 JSON 对象」的契约。"""

    def _run(self, *args):
        return subprocess.run([PY, CYCTL, *args], capture_output=True, text=True,
                              encoding="utf-8", timeout=180)

    def _json(self, r):
        self.assertTrue(r.stdout.strip(), "stdout 不得为空（命令: %s）" % r.args)
        return json.loads(r.stdout)

    def test_discover(self):
        r = self._run("discover", "--no-path")
        self.assertEqual(r.returncode, 0)
        d = self._json(r)
        self.assertTrue(d["ok"])
        self.assertIn("java", d)
        self.assertIn("cytoscape", d)
        self.assertIn("best_java", d)

    def test_doctor_always_emits_json(self):
        r = self._run("doctor")
        d = self._json(r)
        # 本机没装引擎 -> 应当以「环境未就绪」(3) 退出，且指出阻断项
        self.assertIn(r.returncode, (0, 3))
        self.assertIn("checks", d)
        if r.returncode == 3:
            self.assertTrue(d["blocking_ids"])

    def test_bridge_probe(self):
        r = self._run("bridge", "--probe", "--port", "59998", "--timeout", "3")
        d = self._json(r)
        self.assertIn("manifest_probe", d)
        self.assertFalse(d["ok"])

    def test_mcp_full_matrix(self):
        r = self._run("mcp")
        self.assertEqual(r.returncode, 0)
        d = self._json(r)
        self.assertEqual(len(d["hosts"]), len(hmat.HOSTS))
        for h in d["hosts"]:
            self.assertTrue(h["configs"], "%s 没有可落盘位置" % h["id"])
            for c in h["configs"]:
                if c.get("error"):
                    continue
                if c["format"] == "toml":
                    self.assertIn("[mcp_servers.", c["content"])
                else:
                    json.loads(c["content"])      # 必须是合法 JSON

    def test_mcp_unknown_host_is_usage_error(self):
        r = self._run("mcp", "--host", "nope")
        self.assertEqual(r.returncode, 2)
        d = self._json(r)
        self.assertFalse(d["ok"])
        self.assertIn("available", d)

    def test_mcp_single_host_shortcut(self):
        r = self._run("mcp", "--host", "cursor")
        d = self._json(r)
        self.assertEqual(len(d["hosts"]), 1)
        self.assertIn("host", d)

    def test_mcp_write_config_only_prints_without_flag(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run("mcp", "--project", d, "--scope", "project")
            self.assertEqual(r.returncode, 0)
            payload = self._json(r)
            self.assertNotIn("written", payload, "未加 --write-config 时不得写盘")
            self.assertEqual(list(Path(d).iterdir()), [], "默认不得产生任何文件")

    def test_mcp_write_config_creates_and_refuses_mixed(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run("mcp", "--project", d, "--scope", "project", "--write-config")
            self.assertEqual(r.returncode, 0)
            payload = self._json(r)
            actions = {x["host"]: x["action"] for x in payload.get("written", [])}
            self.assertEqual(actions.get("claude-code"), "created")
            self.assertEqual(actions.get("cursor"), "created")
            # gemini 的项目级配置是 settings.json（混合文件），必须被拒
            self.assertEqual(actions.get("gemini-cli"), "refused")
            self.assertTrue((Path(d) / ".mcp.json").exists())
            self.assertTrue((Path(d) / ".cursor" / "mcp.json").exists())
            self.assertFalse((Path(d) / ".gemini" / "settings.json").exists(),
                             "混合配置文件不得被创建或改写")

    def test_start_attach_requires_running_engine(self):
        r = self._run("start", "--engine", "attach", "--port", "59997", "--timeout", "3")
        d = self._json(r)
        self.assertFalse(d["ok"])
        self.assertIn(r.returncode, (3, 4))
        self.assertEqual(d.get("mode"), "attach")

    def test_start_system_engine_gives_actionable_hint(self):
        r = self._run("start", "--engine", "system")
        d = self._json(r)
        if d["ok"]:
            self.skipTest("本机发现了系统 Cytoscape，跳过失败路径断言")
        self.assertFalse(d["ok"])
        self.assertIn("hint", d, "找不到引擎时必须给出可执行的下一步")


if __name__ == "__main__":
    unittest.main(verbosity=2)
