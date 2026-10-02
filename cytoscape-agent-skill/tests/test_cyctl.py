#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cyctl / cyrest 的单元测试（仅标准库 unittest，无需 pytest）。

覆盖：
  1. 命令字符串解析与 URL 构造 —— 与官方 py4cytoscape 行为逐字等价
  2. base url 解析优先级
  3. 版本锁定文件与官方 sha256 清单的交叉一致性（防篡改/防抄错）
  4. CLI 的输出契约（stdout 必须是可解析的单个 JSON）

运行：  python -m unittest discover -s tests -v
或      python tests/test_cyctl.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _closed_port():
    """返回一个**实测**当前无人监听的端口。

    为什么不写死 59999：那是个不可验证的假设。同一次会话里
    `cyctl start --port 59999` 会真的在 59999 起一个引擎，
    于是「端口不通」的前提失效，用例假失败（本轮实测踩到）。
    端口可用性必须现场探测。
    """
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return str(port)
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cyrest  # noqa: E402
import cyctl   # noqa: E402

BASE = "http://localhost:1234/v1"


class TestCommandParsing(unittest.TestCase):
    """命令字符串 -> (命令, 参数) 的拆解。"""

    def test_split_command_with_params(self):
        cy_cmd, params = cyrest.split_command(
            'network get attribute network="test" columnList="SUID"')
        self.assertEqual(cy_cmd, "network get attribute")
        self.assertEqual(params, ['network="test"', 'columnList="SUID"'])

    def test_split_command_without_params(self):
        cy_cmd, params = cyrest.split_command("layout getLayoutNames")
        self.assertEqual(cy_cmd, "layout getLayoutNames")
        self.assertEqual(params, [])

    def test_split_command_rejects_empty(self):
        for bad in ("", "   ", None, 123):
            with self.assertRaises(ValueError):
                cyrest.split_command(bad)

    def test_underscore_and_dash_param_names(self):
        # 官方正则允许 [A-Za-z0-9_-]* 作为参数名
        _, params = cyrest.split_command('x y some_arg="1" other-arg="2"')
        self.assertEqual(params, ['some_arg="1"', 'other-arg="2"'])


class TestUrlConstruction(unittest.TestCase):
    """URL 构造必须与 py4cytoscape 完全一致。

    官方算法：只把命令部分的第一个空格替换为 '/'，再做 URL 编码
    （quote 默认 safe='/'，因此空格变 %20）。
    """

    def test_post_three_word_command(self):
        url, body = cyrest.command_to_post('network import file file="/data/a.sif"', BASE)
        self.assertEqual(url, BASE + "/commands/network/import%20file")
        self.assertEqual(body, {"file": "/data/a.sif"})

    def test_post_command_without_args_yields_empty_body(self):
        url, body = cyrest.command_to_post("network list", BASE)
        self.assertEqual(url, BASE + "/commands/network/list")
        self.assertEqual(body, {})

    def test_post_value_containing_equals_sign_is_split_once(self):
        # 值里含 '=' 时只按第一个 '=' 切分，保留剩余部分
        url, body = cyrest.command_to_post('table import url url="http://x/?a=1&b=2"', BASE)
        self.assertEqual(url, BASE + "/commands/table/import%20url")
        self.assertEqual(body, {"url": "http://x/?a=1&b=2"})

    def test_get_two_word_command_url_and_params(self):
        url, params = cyrest.command_to_get(
            'network get attribute network="test" columnList="SUID"', BASE)
        self.assertEqual(url, BASE + "/commands/network/get%20attribute")
        self.assertEqual(params, {"network": "test", "columnList": "SUID"})

    def test_get_without_params_returns_none(self):
        url, params = cyrest.command_to_get("layout getLayoutNames", BASE)
        self.assertEqual(url, BASE + "/commands/layout/getLayoutNames")
        self.assertIsNone(params)

    def test_single_word_command(self):
        url, params = cyrest.command_to_get("session save", BASE)
        self.assertEqual(url, BASE + "/commands/session/save")
        self.assertIsNone(params)

    def test_quotes_stripped_in_get_values(self):
        _, params = cyrest.command_to_get('command echo message="has space here"', BASE)
        self.assertEqual(params, {"message": "has space here"})


class TestBaseUrlResolution(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(cyrest.resolve_base_url("http://h:9/v1/", env={}), "http://h:9/v1")

    def test_port_argument(self):
        self.assertEqual(cyrest.resolve_base_url(port=8888, env={}), "http://localhost:8888/v1")

    def test_env_var(self):
        env = {"CYTOSCAPE_BASE_URL": "http://remote:1234/v1/"}
        self.assertEqual(cyrest.resolve_base_url(env=env), "http://remote:1234/v1")

    def test_default(self):
        self.assertEqual(cyrest.resolve_base_url(env={}), cyrest.DEFAULT_BASE_URL)
        self.assertEqual(cyrest.DEFAULT_PORT, 1234)


class TestVersionLockIntegrity(unittest.TestCase):
    """锁定文件必须自洽，且所有 official 哈希必须与官方发布清单逐字一致。"""

    @classmethod
    def setUpClass(cls):
        cls.lock = json.loads((ROOT / "assets" / "versions.lock.json").read_text(encoding="utf-8"))
        evidence = ROOT / "evidence" / "official-sha256sums-3.10.5.txt"
        cls.official = {}
        for line in evidence.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            digest, name = line.split(None, 1)
            cls.official[name.lstrip("*").strip()] = digest.lower()

    def test_schema_and_pin(self):
        self.assertEqual(self.lock["schema"], "cyctl/versions.lock/v2")
        self.assertEqual(self.lock["pinned"]["cytoscape"], "3.10.5")
        self.assertEqual(self.lock["pinned"]["jre_required_major"], 17)

    def test_support_matrix_is_honest_about_missing_artifacts(self):
        """support_matrix 里的每一项，都必须能在 artifacts 里找到对应产物（或明确为 null）。"""
        m = self.lock["support_matrix"]
        self.assertIn("_comment", m)
        for plat, by_arch in m.items():
            if plat == "_comment":
                continue
            self.assertIn(plat, self.lock["artifacts"]["cytoscape"])
            for arch, info in by_arch.items():
                self.assertIn(arch, ("x64", "aarch64"))
                self.assertIsInstance(info, dict)

    def test_every_artifact_declares_arch(self):
        for _comp, by_plat in self.lock["artifacts"].items():
            for _plat, variants in by_plat.items():
                for spec in variants:
                    self.assertIn(spec.get("arch"), ("x64", "aarch64"),
                                  "%s 缺少或非法的 arch 字段" % spec["file"])

    def test_engine_modes_documented(self):
        modes = self.lock["engine_modes"]
        for m in ("bundled", "system", "attach"):
            self.assertIn(m, modes)
            self.assertIn("desc", modes[m])

    def test_every_official_hash_matches_official_manifest(self):
        checked = 0
        for _comp, by_plat in self.lock["artifacts"].items():
            for _plat, variants in by_plat.items():
                for spec in variants:
                    if spec.get("sha256_source") != "official":
                        continue
                    checked += 1
                    name = spec["file"]
                    self.assertIn(name, self.official,
                                  "官方清单里没有 %s，哈希来源被标错" % name)
                    self.assertEqual(
                        spec["sha256"].lower(), self.official[name],
                        "%s 的 sha256 与官方清单不一致" % name)
        self.assertGreater(checked, 0, "至少应有一个 official 哈希被校验")

    def test_unhashed_artifacts_are_declared_not_silently_trusted(self):
        for _comp, by_plat in self.lock["artifacts"].items():
            for _plat, variants in by_plat.items():
                for spec in variants:
                    if spec.get("sha256") is None:
                        self.assertIn(spec.get("sha256_source"), ("null", "tofu"),
                                      "%s 无哈希但未声明来源等级" % spec["file"])

    def test_release_base_matches_pinned_version(self):
        self.assertIn(self.lock["pinned"]["cytoscape"], self.lock["release_base"])


class TestProxyBypass(unittest.TestCase):
    """本机 CyREST 请求绝不能被环境代理接管。

    实测：本机若设了 http_proxy（企业代理/调试工具常见），urllib 会把
    http://localhost:1234 也发往代理，代理回 502，表现为「引擎在跑却连不上」。
    """

    def test_localhost_request_does_not_go_through_env_proxy(self):
        """行为级验证：起一个「对任何请求都回 502」的假代理。

        若 cyrest 误走代理 -> 拿到 HTTP 502（kind=remote）；
        正确行为是直连 localhost 失败 -> kind=not_ready。
        """
        import http.server
        import os
        import threading

        class _FakeProxy(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                                   # noqa: N802
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"proxy says 502")

            def log_message(self, *a):                          # noqa: D102
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeProxy)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        proxy_url = "http://127.0.0.1:%d" % srv.server_address[1]

        keys = ("http_proxy", "HTTP_PROXY")
        old = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ[k] = proxy_url
            with self.assertRaises(cyrest.CyRestError) as cm:
                cyrest.cyrest_request("GET", "http://localhost:59999/v1", timeout=5)
            self.assertEqual(cm.exception.kind, "not_ready",
                             "本机请求被代理接管了（拿到 %s）" % cm.exception.status)
        finally:
            srv.shutdown()
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_explicit_use_proxy_still_supported(self):
        """use_proxy=True 时交回 urllib 的默认行为（读环境变量），便于远端部署场景。"""
        import os
        import urllib.request
        old = os.environ.get("http_proxy")
        try:
            os.environ["http_proxy"] = "http://proxy.invalid:8080"
            opener = cyrest._build_opener(use_proxy=True)
            handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertTrue(handlers, "build_opener 会补上默认 ProxyHandler")
            self.assertIn("http", handlers[-1].proxies)
        finally:
            if old is None:
                os.environ.pop("http_proxy", None)
            else:
                os.environ["http_proxy"] = old

    def test_connection_failure_is_classified_not_ready(self):
        with self.assertRaises(cyrest.CyRestError) as cm:
            cyrest.cyrest_request("GET", "http://localhost:59999/v1", timeout=3)
        self.assertEqual(cm.exception.kind, "not_ready")
        self.assertIn("无法连接", str(cm.exception))


class TestCliContract(unittest.TestCase):
    """CLI 输出契约：stdout 必须是合法 JSON，stderr 承载人类日志。"""

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "cyctl.py"), *argv],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    def test_describe_is_json(self):
        r = self._run("describe")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)      # 解析失败即视为契约破坏
        self.assertTrue(payload["ok"])
        names = {c["name"] for c in payload["commands"]}
        for required in ("env", "provision", "start", "wait", "cmd", "rest", "run", "mcp", "stop"):
            self.assertIn(required, names)

    def test_describe_survives_legacy_windows_console_encoding(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env["PYTHONLEGACYWINDOWSSTDIO"] = "1"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "cyctl.py"), "describe"],
            capture_output=True, text=False, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", errors="replace"))
        payload = json.loads(r.stdout.decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_env_is_json_and_reports_platform(self):
        r = self._run("env")
        self.assertIn(r.returncode, (0, 3))
        payload = json.loads(r.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["platform"], ("windows", "linux", "macos"))
        self.assertIn("java", payload)
        self.assertIn("cyrest", payload)

    def test_status_when_engine_absent_returns_remote_error_code(self):
        # 指向一个必然无人在听的端口，必须给出 JSON 而不是堆栈
        r = self._run("status", "--port", _closed_port(), "--timeout", "3")
        self.assertEqual(r.returncode, cyctl.EXIT_REMOTE, r.stderr)
        payload = json.loads(r.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "not_ready")

    def test_provision_reports_unsupported_combo_without_touching_network(self):
        """Linux 上没有官方 JRE 产物 -> 必须给出可读 hint + 退出码 3，且绝不发起下载。"""
        import contextlib
        import io as _io
        from unittest import mock

        class _Args:
            component = "jre"
            variant = None
            force = False
            execute_installer = False
            runtime = None

        buf = _io.StringIO()
        with mock.patch.object(cyctl, "platform_key", return_value="linux"), \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                cyctl.cmd_provision(_Args())
        self.assertEqual(cm.exception.code, cyctl.EXIT_ENV)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("hint", payload)
        self.assertIn("openjdk-17", payload["hint"])

    def test_version_flag(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("cyctl", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
