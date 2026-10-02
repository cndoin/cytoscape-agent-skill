#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳定性 / 性能 / 契约回归测试（打包前验证）。

分层，每一层都对应一个**实际发生过的**失败模式，不是凭想象写的：

  A. TestProxyPolicy      —— 下载网络路径决策。根因：urllib.getproxies() 会读
                             Windows 注册表的 WinINET 设置，把用户从未配置过的代理
                             套上来。实测本机因此从 10.5 MB/s 掉到约 180 KB/s（57 倍）。
  B. TestConcurrency      —— cyrest 默认 opener 是进程内**共享**的 OpenerDirector。
                             若并发下串包，Agent 并发下命令就会拿到别人的结果 ——
                             在生信分析里等于结论错误，而且完全静默。
  C. TestIdempotency      —— 重复执行必须产生同样结果、且不重复产生副作用。
  D. TestResourceStability—— 长循环下句柄 / 内存不得持续增长。
  E. TestHostileInputs    —— 任何输入都不得让进程崩溃或吐出非 JSON。
                             stdout 恒为一个 JSON、退出码不出现 1（内部错误），
                             这是所有 Agent 解析本工具的硬前提。
  F. TestLiveEngine       —— 真实引擎端到端（有引擎才跑，没有自动跳过）。
                             含「同一命令两次结果指纹必须相同」—— 直接对应
                             「结果与原生项目一模一样」这条要求。

运行：  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cyctl     # noqa: E402
import cyrest    # noqa: E402

PY = sys.executable
CYCTL = str(ROOT / "scripts" / "cyctl.py")
#: 一个几乎不可能有服务在听的端口，用于把「引擎未就绪」路径固定下来。
DEAD_PORT = "59999"


# ===========================================================================
# A. 下载网络路径决策
# ===========================================================================

class TestProxyPolicy(unittest.TestCase):
    """代理决策。

    背景：旧实现直接 `ProxyHandler(urllib.request.getproxies())`。而 getproxies()
    除了环境变量还会读 Windows 注册表 / macOS 系统网络设置 —— 于是「没配过代理」
    的用户会被悄悄套上系统级代理。实测本机 http_proxy 全空、注册表里有个
    127.0.0.1:7890，下载吞吐因此只有直连的 1/57。
    """

    def test_none_mode_is_direct(self):
        self.assertEqual(cyctl._resolve_proxies("none"), {})

    def test_auto_mode_is_a_sentinel_not_a_value(self):
        """auto 必须返回 None（待探测），绝不能在这里就把系统代理塞进去。"""
        self.assertIsNone(cyctl._resolve_proxies("auto"))

    def test_env_mode_reads_environment_only(self):
        with mock.patch("urllib.request.getproxies_environment",
                        return_value={"http": "http://from-env:8080"}), \
             mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://from-registry:7890"}):
            self.assertEqual(cyctl._resolve_proxies("env"),
                             {"http": "http://from-env:8080"})

    def test_system_mode_reads_system_settings(self):
        with mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://from-registry:7890"}):
            self.assertEqual(cyctl._resolve_proxies("system"),
                             {"http": "http://from-registry:7890"})

    def test_explicit_url_overrides_mode(self):
        self.assertEqual(cyctl._resolve_proxies("none", explicit="http://p:1"),
                         {"http": "http://p:1", "https": "http://p:1"})

    # ---- auto 的行为 -------------------------------------------------------

    def test_auto_prefers_direct_even_when_registry_proxy_exists(self):
        """★ 核心回归：直连可达时必须直连。

        而且——直连可用时**连问都不该去问**系统代理（否则等于仍在依赖它）。
        """
        with mock.patch.object(cyctl, "_probe_url", return_value=(True, "HTTP 206")), \
             mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://from-registry:7890"}) as gp:
            proxies, info = cyctl.choose_proxies("http://example.invalid/artifact")
        self.assertEqual(proxies, {}, "直连可用却仍然使用了代理")
        self.assertEqual(info["decided"], "direct")
        gp.assert_not_called()

    def test_auto_falls_back_to_system_proxy_when_direct_fails(self):
        def probe(url, proxies, timeout=10):
            return (False, "ConnectionRefused") if not proxies else (True, "HTTP 206")

        with mock.patch.object(cyctl, "_probe_url", side_effect=probe), \
             mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://from-registry:7890"}):
            proxies, info = cyctl.choose_proxies("http://example.invalid/artifact")
        self.assertEqual(proxies, {"http": "http://from-registry:7890"})
        self.assertEqual(info["decided"], "system-proxy")
        self.assertIn("direct_error", info)

    def test_auto_without_any_proxy_reports_direct_unverified(self):
        with mock.patch.object(cyctl, "_probe_url", return_value=(False, "boom")), \
             mock.patch("urllib.request.getproxies", return_value={}):
            proxies, info = cyctl.choose_proxies("http://example.invalid/artifact")
        self.assertEqual(proxies, {})
        self.assertEqual(info["decided"], "direct-unverified")
        self.assertIn("--proxy", info["note"], "应给出可执行的下一步提示")

    def test_probe_uses_range_not_head(self):
        """部分 CDN 对 HEAD 返回 405 但对 Range GET 正常；用 HEAD 会误判不可达。"""
        seen = {}

        class FakeResp:
            status = 206

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                seen["range"] = req.get_header("Range")
                seen["ua"] = req.get_header("User-agent") or req.get_header("User-agent")
                return FakeResp()

        with mock.patch("urllib.request.build_opener", return_value=FakeOpener()):
            ok, _ = cyctl._probe_url("http://x/y", {})
        self.assertTrue(ok)
        self.assertEqual(seen["range"], "bytes=0-0")

    def test_download_installs_exactly_the_given_proxies(self):
        """_download 必须把调用方给的 proxies 装进 ProxyHandler，不得自作主张。"""
        seen = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.URLError("stop here")

        def fake_build(*handlers):
            for h in handlers:
                if isinstance(h, urllib.request.ProxyHandler):
                    seen.update(h.proxies)
            return FakeOpener()

        with tempfile.TemporaryDirectory() as td, \
             mock.patch("urllib.request.build_opener", side_effect=fake_build):
            with self.assertRaises(cyctl.DownloadError):
                cyctl._download("http://x/y", Path(td) / "f", retries=1,
                                proxies={"https": "http://p:1"})
        self.assertEqual(seen, {"https": "http://p:1"})

    def test_download_default_is_direct_not_system(self):
        """不传 proxies 时必须是直连 —— 这是「不再静默走注册表代理」的保证。"""
        seen = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.URLError("stop here")

        def fake_build(*handlers):
            for h in handlers:
                if isinstance(h, urllib.request.ProxyHandler):
                    seen.update(h.proxies)
            return FakeOpener()

        with tempfile.TemporaryDirectory() as td, \
             mock.patch("urllib.request.build_opener", side_effect=fake_build), \
             mock.patch("urllib.request.getproxies",
                        return_value={"http": "http://from-registry:7890"}):
            with self.assertRaises(cyctl.DownloadError):
                cyctl._download("http://x/y", Path(td) / "f", retries=1)
        self.assertEqual(seen, {}, "默认情况下不该使用任何代理")


# ===========================================================================
# B. 并发正确性
# ===========================================================================

class _EchoHandler(BaseHTTPRequestHandler):
    """把请求体里的 marker 原样回显，用于检测并发串包。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return self._send({"errors": ["bad json"]})
        if self.path.endswith("/slow"):
            time.sleep(0.05)
        self._send({"ok": True, "echo": body.get("marker"), "path": self.path})

    def do_GET(self):
        if self.path.endswith("/fail"):
            return self._send({"errors": ["引擎业务错误"]})
        if self.path.endswith("/version"):
            return self._send({"apiVersion": "v1", "cytoscapeVersion": "3.10.5"})
        self._send({"ok": True, "path": self.path, "version": "3.10.5"})


class TestConcurrentRequests(unittest.TestCase):
    """共享 OpenerDirector 在并发下的正确性。

    这是个真问题：cyrest._DEFAULT_OPENER 是模块级单例，被所有请求共用。
    urllib 的 OpenerDirector 并非文档化的线程安全组件，一旦在并发下互相污染，
    Agent 同时下多条命令时就会拿到**别人的响应**——在医学/生信场景里，
    这比报错可怕得多：它静默地给出错误结论。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.srv.server_address[1]
        cls.srv.daemon_threads = True
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_shared_opener_does_not_mix_up_responses(self):
        workers, rounds = 12, 15
        errors, seen = [], []

        def worker(i):
            for j in range(rounds):
                marker = "w%d-j%d" % (i, j)
                try:
                    url, body = cyrest.command_to_post(
                        'test echo marker="%s"' % marker, self.base)
                    res, _, _ = cyrest.cyrest_request("POST", url, body=body)
                    if res.get("echo") != marker:
                        errors.append((marker, res.get("echo")))
                    seen.append(marker)
                except Exception as exc:            # noqa: BLE001
                    errors.append((marker, "EXC:%s" % exc))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(90)

        self.assertEqual(errors, [], "并发下出现响应串包或异常")
        self.assertEqual(len(seen), workers * rounds, "有请求未完成（可能被阻塞）")

    def test_concurrent_get_and_post_with_shared_server(self):
        """GET 与 POST 混合并发，验证 opener 在不同方法间也不互相干扰。"""
        errors = []

        def worker(i):
            for j in range(10):
                try:
                    if j % 2:
                        cyrest.cyrest_request("GET", self.base + "/networks")
                    else:
                        url, body = cyrest.command_to_post(
                            'test echo marker="g%d-%d"' % (i, j), self.base)
                        res, _, _ = cyrest.cyrest_request("POST", url, body=body)
                        if res.get("echo") != "g%d-%d" % (i, j):
                            errors.append(res)
                except Exception as exc:            # noqa: BLE001
                    errors.append("EXC:%s" % exc)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(errors, [])

    def test_business_error_is_detected_even_with_http_200(self):
        """HTTP 200 但响应体含 errors —— 必须判为失败，不能当成功。"""
        with self.assertRaises(cyrest.CyRestError) as cm:
            cyrest.cyrest_request("GET", self.base + "/fail")
        self.assertIn("业务错误", str(cm.exception))


# ---------------------------------------------------------------------------
# B2. 引擎版本读取与一致性判定
# ---------------------------------------------------------------------------

class TestEngineVersionProbe(unittest.TestCase):
    """版本读取。

    根端点 GET /v1/ **不含** cytoscapeVersion —— 版本在 GET /v1/version。
    这个区分是实测得到的：不查清楚就会写出「拿不到版本」的溯源清单，
    而版本是判断结果能否复现的第一要素。
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.srv.server_address[1]
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_engine_version_reads_cytoscape_version(self):
        ver, raw = cyrest.engine_version(self.base)
        self.assertEqual(ver, "3.10.5")
        self.assertIn("apiVersion", raw)

    def test_engine_version_returns_none_when_field_absent(self):
        """端点在但字段缺失 -> 返回 None 而不是抛异常，由调用方决定怎么办。"""
        class OnlyRoot(_EchoHandler):
            def do_GET(self):
                self._send({"allAppsStarted": True})
        srv = HTTPServer(("127.0.0.1", 0), OnlyRoot)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ver, raw = cyrest.engine_version("http://127.0.0.1:%d/v1" % srv.server_address[1])
            self.assertIsNone(ver)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_version_info_flags_mismatch_loudly(self):
        """版本不一致必须给出可读警告 —— 静默通过等于埋雷。"""
        with mock.patch.object(cyrest, "engine_version",
                               return_value=("3.9.0", {"cytoscapeVersion": "3.9.0"})):
            info = cyctl._engine_version_info(self.base)
        self.assertTrue(info["available"])
        self.assertFalse(info["match"])
        self.assertIn("不一致", info["note"])
        self.assertEqual(info["engine"], "3.9.0")

    def test_version_info_reports_unreachable_engine(self):
        bad = "http://127.0.0.1:1/v1"
        info = cyctl._engine_version_info(bad, timeout=2)
        self.assertFalse(info["available"])
        self.assertIn("pinned", info)


# ===========================================================================
# C. 幂等性
# ===========================================================================

class TestIdempotency(unittest.TestCase):

    def _run(self, *args, timeout=60):
        return subprocess.run(
            [PY, CYCTL] + list(args), capture_output=True, text=True,
            encoding="utf-8", timeout=timeout, cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_mcp_matrix_is_byte_identical_across_runs(self):
        a = self._run("mcp", "--project", str(ROOT))
        b = self._run("mcp", "--project", str(ROOT))
        self.assertEqual(a.returncode, 0)
        self.assertEqual(a.stdout, b.stdout,
                         "同一输入两次输出不一致 —— Agent 无法依赖本工具的输出")

    def test_describe_is_byte_identical_across_runs(self):
        a = self._run("describe")
        b = self._run("describe")
        self.assertEqual(a.stdout, b.stdout)

    def test_write_config_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            args = ["mcp", "--project", str(proj), "--write-config",
                    "--scope", "project", "--only-dedicated"]
            r1 = self._run(*args)
            self.assertEqual(r1.returncode, 0)
            snapshot = (proj / ".mcp.json").read_bytes()
            r2 = self._run(*args)
            self.assertEqual(r2.returncode, 0)
            self.assertEqual(snapshot, (proj / ".mcp.json").read_bytes(),
                             "第二次写入改动了文件 —— 不幂等")
            summary = json.loads(r2.stdout)["written_summary"]
            self.assertGreaterEqual(summary.get("unchanged", 0), 1,
                                    "第二次应为 unchanged，而不是重复合并")

    def test_provision_is_idempotent_when_already_installed(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "runtime"
            (rd / "cytoscape").mkdir(parents=True)      # 伪造已安装
            r = subprocess.run(
                [PY, CYCTL, "provision", "--component", "cytoscape",
                 "--runtime", str(rd)],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
                cwd=str(ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(r.returncode, 0)
            payload = json.loads(r.stdout)
            self.assertTrue(payload["already_installed"])

    def test_repeated_status_on_dead_port_is_stable(self):
        outs = []
        for _ in range(3):
            r = self._run("status", "--port", DEAD_PORT, "--timeout", "3")
            self.assertEqual(r.returncode, 4)
            outs.append(json.loads(r.stdout))
        self.assertEqual({o["kind"] for o in outs}, {"not_ready"},
                         "引擎未就绪必须稳定归类为 not_ready")


# ===========================================================================
# D. 资源占用
# ===========================================================================

def _handle_count():
    """当前进程的打开句柄数（Windows 用 GetProcessHandleCount，POSIX 数 fd）。"""
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            n = ctypes.c_ulong(0)
            if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
                return n.value
        except Exception:                        # noqa: BLE001
            return None
        return None
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


class TestResourceStability(unittest.TestCase):
    """长循环下不得泄漏：真实 Agent 会连续下几百条命令。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.srv.server_address[1]
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _one_request(self, i):
        url, body = cyrest.command_to_post('test echo marker="n%d"' % i, self.base)
        res, _, _ = cyrest.cyrest_request("POST", url, body=body)
        return res.get("echo")

    def test_no_handle_leak_over_many_requests(self):
        before = _handle_count()
        if before is None:
            self.skipTest("本平台无法读取句柄数")
        for i in range(20):                      # 预热，排除首次初始化
            self._one_request(i)
        warm = _handle_count()
        for i in range(300):
            self.assertEqual(self._one_request(i), "n%d" % i)
        after = _handle_count()
        growth = after - warm
        self.assertLess(growth, 64,
                        "300 次请求后句柄增长 %d（%d -> %d），疑似泄漏"
                        % (growth, warm, after))

    def test_no_unbounded_memory_growth(self):
        """用 tracemalloc 观测 Python 侧分配，300 次请求后不得持续线性增长。"""
        import tracemalloc
        for i in range(20):
            self._one_request(i)
        tracemalloc.start()
        for i in range(150):
            self._one_request(i)
        snap1 = tracemalloc.take_snapshot()
        for i in range(150):
            self._one_request(i)
        snap2 = tracemalloc.take_snapshot()
        tracemalloc.stop()
        diff = sum(s.size_diff for s in snap2.compare_to(snap1, "filename"))
        self.assertLess(diff, 4 * 1024 * 1024,
                        "150 次请求间净增 %.1f KB，疑似无界增长" % (diff / 1024.0))


# ===========================================================================
# E. 敌意 / 边界输入
# ===========================================================================

class TestHostileInputs(unittest.TestCase):
    """任何输入都不得让 CLI 崩溃或吐出非 JSON。

    硬不变量（Agent 依赖它）：
      * stdout 恒为一个合法 JSON 对象（空输出 = 调用方崩）
      * 退出码绝不出现 1（内部未捕获异常）
    """

    #: Windows CreateProcess 的命令行总长上限约 32K。超过时 subprocess 会在
    #: **启动进程之前**就抛 WinError 206 —— 那测的是操作系统，不是 cyctl。
    #: 把「超长」限制在平台确实能把参数传进去的量级，仍远超任何合理用户输入。
    #: （真正的超长字符串处理由 test_command_splitter_rejects_only_explicitly
    #:   在进程内直接覆盖，不经过命令行。）
    LONG_ARG = 5000

    HOSTILE = [
        ["cmd", ""],
        ["cmd", "   "],
        ["cmd", "x" * LONG_ARG],
        ["cmd", "network get attribute network="],
        ["cmd", "network get attribute network=\"\""],
        ["cmd", "a b c d e f g h i j"],
        ["cmd", "命名空间 中文命令 参数=值"],
        ["cmd", "network\timport\tfile"],
        ["cmd", "network get attribute network=\"a=b=c\""],
        ["cmd", 'network get attribute network="\\"quoted\\""'],
        ["cmd", "network get attribute network=😀"],
        # 注：原先这里有 ["cmd", "net\u0000work list"]，已移除 —— Windows
        # 命令行**无法携带 NUL 字符**，subprocess 会在启动进程之前就抛
        # ValueError: embedded null character。那测的是操作系统而非 cyctl。
        # NUL 的正确覆盖方式是在进程内直接喂给 split_command，见
        # test_command_splitter_rejects_only_explicitly。
        ["rest", "GET", ""],
        ["rest", "GET", "networks", "--param", "novalue"],
        ["rest", "GET", "networks", "--param", "=v"],
        ["rest", "GET", "networks", "--body", "["],
        ["rest", "GET", "networks", "--body", "null"],
        ["rest", "WAT", "networks"],
        ["rest", "GET", "a" * LONG_ARG],
        ["run", "no/such/file.json"],
        ["mcp", "--host", "x" * 5000],
        ["discover", "--max-depth", "0"],
        ["provision", "--component", "cytoscape", "--variant", "z" * 5000],
        ["doctor"],
    ]

    def test_cli_never_returns_non_json_or_internal_error(self):
        problems = []
        for args in self.HOSTILE:
            # 强制指向一个必然没有服务的端口，避免打到真实引擎
            argv = [PY, CYCTL] + args
            if args[0] in ("cmd", "rest", "run", "status"):
                argv += ["--port", DEAD_PORT, "--timeout", "3"]
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=90,
                                   cwd=str(ROOT),
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            except subprocess.TimeoutExpired:
                problems.append((args, "TIMEOUT"))
                continue

            label = " ".join(a[:24] for a in args)
            if r.returncode not in (0, 2, 3, 4, 5):
                problems.append((label, "退出码 %s（1 = 内部未捕获异常）" % r.returncode))
                continue
            if not r.stdout.strip():
                problems.append((label, "stdout 为空，违反「一个 JSON 信封」契约"))
                continue
            try:
                payload = json.loads(r.stdout)
            except ValueError as exc:
                problems.append((label, "stdout 不是合法 JSON: %s" % exc))
                continue
            if not isinstance(payload, dict):
                problems.append((label, "stdout 顶层不是 JSON 对象"))

        self.assertEqual(problems, [], "敌意输入导致契约破坏：\n" +
                         "\n".join("  - %s: %s" % p for p in problems))

    def test_empty_command_is_usage_error_not_internal(self):
        """空命令必须以 2（用法错误）结束，绝不能是 1（内部未捕获异常）。

        这是**实际发生过的**缺陷：`cyctl cmd ""` 抛出的 ValueError 没被 cmd
        子命令捕获，一路落到 main 的兜底分支，返回退出码 1 —— 违反输出契约
        （只允许 0/2/3/4/5），调用方会把 1 解读成未知灾难。
        """
        for bad in ("", "   ", "\t", "\n", "  \t \n "):
            with self.subTest(cmd=bad):
                r = subprocess.run(
                    [PY, CYCTL, "cmd", bad, "--port", DEAD_PORT, "--timeout", "3"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=60, cwd=str(ROOT),
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self.assertEqual(r.returncode, 2,
                                 "空命令应报用法错误(2)，实际 %s\n%s"
                                 % (r.returncode, r.stdout + r.stderr))
                payload = json.loads(r.stdout)
                self.assertFalse(payload["ok"])

    def test_command_splitter_rejects_only_explicitly(self):
        """split_command 允许抛 ValueError，但不得抛别的异常。"""
        for s in ["", "   ", None, 123, [], {}, "x" * 100000, "\n", "\t",
                  "net\u0000work list", "\u0000", "a\u0000b"]:
            try:
                cyrest.split_command(s)
            except ValueError:
                pass
            except Exception as exc:             # noqa: BLE001
                self.fail("输入 %r 抛了非 ValueError 的 %s: %s"
                          % (s, type(exc).__name__, exc))

    def test_command_to_post_handles_pathological_parameters(self):
        cases = [
            'net list a=""',
            'net list a="b c d"',
            'net list a="x" a="y"',                 # 重复参数
            'net list =v',                          # 空参数名
            'net list a=',
        ]
        for cmd in cases:
            try:
                url, body = cyrest.command_to_post(cmd)
            except ValueError:
                continue
            self.assertIsInstance(url, str)
            self.assertIsInstance(body, dict)
            self.assertTrue(url.startswith("http"))


# ===========================================================================
# F. 真实引擎端到端（没有引擎则跳过）
# ===========================================================================

ENGINE_BASE = (os.environ.get("CYTOSCAPE_BASE_URL") or "http://localhost:1234/v1")


def _engine_ready():
    try:
        cyrest.cyrest_request("GET", ENGINE_BASE, timeout=3)
        return True
    except Exception:                            # noqa: BLE001
        return False


ENGINE_UP = _engine_ready()


@unittest.skipUnless(ENGINE_UP, "未检测到运行中的 Cytoscape（设置 CYTOSCAPE_BASE_URL 或先 cyctl start）")
class TestLiveEngine(unittest.TestCase):
    """接真实引擎跑。这里的断言直接对应「结果与原生项目一致」这条硬要求。"""

    @classmethod
    def setUpClass(cls):
        cls.base = ENGINE_BASE

    def _cmd(self, s):
        url, body = cyrest.command_to_post(s, self.base)
        return cyrest.cyrest_request("POST", url, body=body, timeout=60)[0]

    def test_engine_reports_version(self):
        """版本在 /v1/version，不在根端点 /v1/。"""
        ver, raw = cyrest.engine_version(self.base)
        self.assertIsNotNone(ver, "拿不到引擎版本：%r" % (raw,))
        self.assertRegex(str(ver), r"^\d+\.\d+")

    def test_root_endpoint_has_no_version_field(self):
        """把「根端点不含版本」这件事锁死，防止将来又从这里取版本。"""
        info, _, _ = cyrest.cyrest_request("GET", self.base, timeout=10)
        self.assertIsInstance(info, dict)
        self.assertNotIn("cytoscapeVersion", info)
        self.assertIn("allAppsStarted", info)

    def test_network_list_returns_networks_array(self):
        """network list 的真实返回是 {"data":{"networks":[...]},"errors":[]}。

        不是裸数组 —— 官方命令的返回值统一包在 data 里，而 CyREST 函数
        （/v1/networks）才直接返回数组。两者结构不同，断言必须照实写。

        两个容易写错的点：
          * networks 里装的是**网络的 SUID（整数）**，不是网络对象；
          * **空会话时引擎返回 {"data": {}}** —— 连 networks 键都没有。
            这是干净引擎的真实行为（刚启动时一个网络都没有），不是异常。
            原断言假设会话非空，在任何一台刚起引擎的机器上都会误报。
        """
        res = self._cmd("network list")
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("errors"), [])
        data = res.get("data")
        self.assertIsInstance(data, dict)
        nets = data.get("networks")
        if nets is None:
            self.assertEqual(data, {}, "空会话下 data 只应为空对象：%r" % (data,))
        else:
            self.assertIsInstance(nets, list)
            self.assertTrue(all(isinstance(x, int) for x in nets),
                            "networks 里应是 SUID 整数：%r" % (nets,))

    def test_engine_version_matches_lock(self):
        """★ 引擎实际版本必须等于 versions.lock.json 的 pinned。

        这是「结果可复现」的前提：引擎版本一变，布局与统计结果就可能变。
        """
        ver, _ = cyrest.engine_version(self.base)
        lock = json.loads((ROOT / "assets" / "versions.lock.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(ver, lock["pinned"]["cytoscape"],
                         "实际引擎版本 %s != 锁定版本 %s"
                         % (ver, lock["pinned"]["cytoscape"]))

    def test_same_command_twice_gives_identical_fingerprint(self):
        """★ 结果确定性：同一条命令两次调用的结果指纹必须完全相同。

        这就是「结果和原始项目一模一样」在工具层面的可验证形式 ——
        如果连同一进程内两次调用都无法复现，跨机复现就无从谈起。
        """
        def fp(cmd):
            res = self._cmd(cmd)
            return hashlib.sha256(
                json.dumps(res, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()

        a = fp("network list")
        b = fp("network list")
        self.assertEqual(a, b)

    def test_concurrent_commands_against_live_engine(self):
        """真实引擎上的并发：每条命令的响应必须与自己的请求对应。"""
        errors = []

        def worker(i):
            for j in range(5):
                try:
                    res = self._cmd("network list")
                    data = (res or {}).get("data")
                    # 空会话下 networks 键不存在，这是合法的；见
                    # test_network_list_returns_networks_array 的说明。
                    if not isinstance(data, dict):
                        errors.append((i, j, "结构异常: %r" % (res,)))
                    elif "networks" in data and not isinstance(data["networks"], list):
                        errors.append((i, j, "networks 不是列表: %r" % (res,)))
                except Exception as exc:         # noqa: BLE001
                    errors.append((i, j, "EXC:%s" % exc))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(120)
        self.assertEqual(errors, [], "真实引擎并发下出现错误")

    def test_rest_function_endpoint(self):
        res, _, _ = cyrest.cyrest_request("GET", self.base + "/networks", timeout=30)
        self.assertIsInstance(res, list)

    def test_unknown_command_is_reported_not_silently_ignored(self):
        """未知命令必须报错。若引擎返回 200 且为空，说明命令被静默忽略了。"""
        try:
            res = self._cmd("this is not a real cytoscape command at all")
        except cyrest.CyRestError:
            return                                # 正确：明确失败
        self.fail("未知命令未报错，返回了 %r —— 静默忽略是致命的" % (res,))

    def test_mcp_endpoint_probe_is_classified(self):
        """MCP 端点：可用则校验工具列表；未安装 MCP App 则必须给出 404 提示。"""
        import mcpbridge
        port = int(self.base.split(":")[2].split("/")[0])
        url = "http://localhost:%d/mcp" % port
        probe = mcpbridge.probe_manifest(url, timeout=10)
        if probe.get("ok"):
            self.assertIn("tool", probe["manifest"].lower())
        else:
            self.assertIn(probe.get("status"), (404, 500, None),
                          "非预期状态：%s" % probe)
            self.assertTrue(probe.get("hint"), "失败时必须给出可执行的提示")

    def _sample_sif(self):
        """定位官方示例网络 galFiltered.sif；都没有则返回 None。

        三个来源，按「越接近上游越好」排序：

        1. `runtime/cytoscape/<ver>/sampleData/galFiltered.sif` —— 上游安装包里
           原样带出来的文件，最权威。
        2. 同目录下按名字扫（版本号变化时不至于直接失效）。
        3. `data/galFiltered.sif` —— 仓库自带的基线。

        第 3 条是**必须**的：`runtime/` 是 580 MB 的 provision 下载，故意不进分发包。
        没有它，下面三条最关键的确定性断言（330/359、重复导入一致、PNG 字节精确）
        在「解压后的包」里会全部静默跳过 —— 那等于打包校验其实没验到核心。
        已实测 `data/galFiltered.sif` 与上游 sampleData 里的那份**逐字节相同**
        （6822 B，sha256 `18aedc36…`，LF × 359），所以拿它当基线不会引入偏差。
        """
        p = (ROOT / "runtime" / "cytoscape" / "cytoscape-windows-3.10.5"
             / "sampleData" / "galFiltered.sif")
        if p.exists():
            return p
        base = ROOT / "runtime" / "cytoscape"
        if base.is_dir():
            for d in sorted(base.iterdir()):
                q = d / "sampleData" / "galFiltered.sif"
                if q.exists():
                    return q
        baseline = ROOT / "data" / "galFiltered.sif"
        return baseline if baseline.exists() else None

    def test_import_sample_network_gives_expected_counts(self):
        """★ 导入官方示例网络，节点/边数必须是 330 / 359。

        这是「结果与原生项目一模一样」最直接的证据：galFiltered.sif 是 Cytoscape
        官方示例网络，其标准规模就是 330 个节点 / 359 条边。数字一旦对不上，
        说明引擎版本、SIF 解析或导入路径的行为发生了漂移 —— 那正是最需要
        在打包前拦住的东西。
        """
        sif = self._sample_sif()
        if sif is None:
            self.skipTest("未找到示例网络（需先 cyctl provision 下载自带引擎）")
        self._cmd('network import file file="%s"' % sif)
        nets = self._cmd("network list")["data"]["networks"]
        self.assertTrue(nets, "导入后 network list 仍为空")
        suid = nets[-1]                      # 刚导入的那个
        nodes, _, _ = cyrest.cyrest_request(
            "GET", "%s/networks/%d/nodes" % (self.base, suid), timeout=60)
        edges, _, _ = cyrest.cyrest_request(
            "GET", "%s/networks/%d/edges" % (self.base, suid), timeout=60)
        self.assertEqual(len(nodes), 330, "节点数与官方示例不符")
        self.assertEqual(len(edges), 359, "边数与官方示例不符")

    def test_reimport_of_same_file_gives_identical_counts(self):
        """★ 同一文件重复导入，规模必须完全一致（确定性，与网络 SUID 无关）。"""
        sif = self._sample_sif()
        if sif is None:
            self.skipTest("未找到示例网络（需先 cyctl provision 下载自带引擎）")
        sizes = []
        for _ in range(2):
            self._cmd('network import file file="%s"' % sif)
            suid = self._cmd("network list")["data"]["networks"][-1]
            nodes, _, _ = cyrest.cyrest_request(
                "GET", "%s/networks/%d/nodes" % (self.base, suid), timeout=60)
            edges, _, _ = cyrest.cyrest_request(
                "GET", "%s/networks/%d/edges" % (self.base, suid), timeout=60)
            sizes.append((len(nodes), len(edges)))
        self.assertEqual(sizes[0], sizes[1],
                         "同一文件两次导入规模不同：%r" % (sizes,))
        self.assertEqual(sizes[0], (330, 359))

    def test_binary_view_export_preserves_png_bytes(self):
        """★ 视图导出的 PNG 必须完整保留二进制字节。

        两层意义：
          1. 这是本轮审计发现的静默失败 —— 默认通道把响应体按 UTF-8 解码
             （errors="replace"），二进制会被替换成 U+FFFD 而响应仍是 ok:true。
             这里断言落盘首字节是 PNG 魔数，把「成功信封里装垃圾」钉死。
          2. 重复导出都必须是完整 PNG，长度与 SHA-256 必须对应实际落盘字节。

        不断言两张图的 SHA-256 相同：引擎使用中的活动视图可能随布局、渲染
        与其他 UI 状态变化；图像字节一致应在固定布局、App 集合和视图状态的
        专用实验中核验（见 docs/07），不属于无条件的导出契约。
        """
        sif = self._sample_sif()
        if sif is None:
            self.skipTest("未找到示例网络（需先 cyctl provision 下载自带引擎）")
        self._cmd('network import file file="%s"' % sif)
        suid = self._cmd("network list")["data"]["networks"][-1]
        views, _, _ = cyrest.cyrest_request(
            "GET", "%s/networks/%d/views" % (self.base, suid), timeout=60)
        if not views:
            self.skipTest("导入的网络没有视图，无法导出图片")
        vid = views[0]

        with tempfile.TemporaryDirectory() as td:
            for i in range(2):
                dest = os.path.join(td, "v%d.png" % i)
                info = cyrest.cyrest_download(
                    "networks/%s/views/%s.png" % (suid, vid), self.base, dest,
                    timeout=180)
                raw = open(dest, "rb").read()
                self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"),
                                "落盘内容不是 PNG：%r" % raw[:16])
                self.assertEqual(info["size"], len(raw))
                self.assertEqual(info["sha256"], hashlib.sha256(raw).hexdigest())
                # 反证：这层字节经默认 JSON 通道会被替换字符毁掉
                self.assertIn("\ufffd", raw.decode("utf-8", errors="replace"))

    # ---- MCP 直连（Streamable HTTP）协议级往返 ---------------------------
    # 只验证「/mcp/manifest 返回 200」是不够的：那只证明 App **装上了**，
    # 不证明协议对话**能用**。下面做真正的 initialize / tools/list / tools/call。

    def _mcp_bridge(self):
        """建立到 MCP 端点的桥；App 未生效则跳过（而不是失败）。"""
        import mcpbridge
        port = int(self.base.split(":")[2].split("/")[0])
        root = "http://localhost:%d" % port
        probe = mcpbridge.probe_manifest(root, timeout=10)
        if not probe.get("ok"):
            self.skipTest("MCP App 未安装/未生效：%s" % probe.get("error"))
        return mcpbridge.Bridge(root + "/mcp", timeout=30)

    def _mcp_call(self, bridge, rid, method, params=None):
        import mcpbridge
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        status, headers, body = bridge._post(msg)
        if status == 202:                     # 通知类：Accepted 且无 body
            return status, []
        msgs = mcpbridge._extract_messages(headers.get("Content-Type"), body)
        for m in msgs:
            bridge._maybe_record_version(m)
        return status, msgs

    def _mcp_initialize(self, bridge):
        status, msgs = self._mcp_call(bridge, 1, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cyctl-tests", "version": "1.0.0"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(bridge.session_id,
                        "initialize 未返回 Mcp-Session-Id —— 后续请求会全部失联")
        for m in msgs:
            if isinstance(m, dict) and isinstance(m.get("result"), dict):
                return m["result"]
        self.fail("initialize 没有返回 result")

    def test_mcp_initialize_returns_session_and_version(self):
        """★ 会话握手：必须拿到 Mcp-Session-Id 与 protocolVersion。

        丢掉 session id 的症状是「initialize 成功、tools/list 却返回空」，
        极难定位；这里把它钉死。
        """
        res = self._mcp_initialize(self._mcp_bridge())
        self.assertTrue(res.get("protocolVersion"))
        self.assertIn("serverInfo", res)

    def test_mcp_tools_list_is_nonempty(self):
        """tools/list 必须返回工具，且含命令网关三件套。

        ★ 不要拿 /mcp/manifest 当工具清单的事实源：实测 manifest 只文档化了
        4 个工具，而 tools/list 返回 25 个（App 升级还会继续加）。
        因此只断言「包含」，不断言「完全等于」。
        """
        bridge = self._mcp_bridge()
        self._mcp_initialize(bridge)
        status, msgs = self._mcp_call(bridge, 2, "tools/list")
        self.assertEqual(status, 200)
        tools = []
        for m in msgs:
            if isinstance(m, dict) and isinstance(m.get("result"), dict):
                tools = m["result"].get("tools") or []
        names = {t.get("name") for t in tools}
        self.assertTrue(names, "tools/list 返回为空")
        for required in ("command_gateway_search", "command_gateway_get",
                         "command_gateway_invoke"):
            self.assertIn(required, names)

    def test_mcp_tools_call_readonly_schema_fetch(self):
        """tools/call 调**只读**工具，必须真的拿到命令 schema。

        只调 command_gateway_get（官方标注 read-only，不改桌面状态）。
        绝不调 command_gateway_invoke —— 它能执行任意桌面命令，包括关掉引擎。
        """
        bridge = self._mcp_bridge()
        self._mcp_initialize(bridge)
        status, msgs = self._mcp_call(bridge, 3, "tools/call", {
            "name": "command_gateway_get",
            "arguments": {"commandKeys": ["network list"]},
        })
        self.assertEqual(status, 200)
        payload = None
        for m in msgs:
            if isinstance(m, dict) and isinstance(m.get("result"), dict):
                r = m["result"]
                self.assertFalse(r.get("isError"), "工具返回 isError=true")
                for c in (r.get("content") or []):
                    if c.get("type") == "text":
                        payload = json.loads(c["text"])
                        break
        self.assertIsInstance(payload, dict, "没拿到工具结果")
        self.assertTrue(payload.get("success"), "工具 success 不为真：%r" % (payload,))
        keys = [x.get("commandKey") for x in (payload.get("results") or [])]
        self.assertIn("network list", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
