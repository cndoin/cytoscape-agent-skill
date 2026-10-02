#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全与稳定性回归测试（2026-10-01 全面检查后补入）。

这些用例全部对应「实际发生过的失败模式」，不是凭想象写的：

  1. TestExtractSafety      —— Zip Slip / Tar Slip：恶意归档写过 dest 之外
  2. TestDownloadStability  —— 下载中断留下半截文件被当成有效缓存
  3. TestCliJsonContract    —— 退出码 2 时 stdout 为空，违反输出契约
  4. TestProvisionSemantics —— --force 语义谎报；无哈希产物被静默安装

运行：  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io as _io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cyctl  # noqa: E402
import cyrest  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 解压安全
# ---------------------------------------------------------------------------

class TestExtractSafety(unittest.TestCase):
    """路径穿越防护。归档条目名由归档自己控制，不能信。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "dest"
        self.dest.mkdir()
        self.outside = self.root / "PWNED.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _evil_tar(self, member_name):
        p = self.root / "evil.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            data = b"pwned"
            info = tarfile.TarInfo(name=member_name)
            info.size = len(data)
            tf.addfile(info, _io.BytesIO(data))
        return p

    def _evil_zip(self, member_name):
        p = self.root / "evil.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr(member_name, "pwned")
        return p

    def test_is_within_rejects_sibling_prefix(self):
        # 经典陷阱：/a/bc 的字符串前缀是 /a/b，但不是它的子路径
        self.assertFalse(cyctl._is_within("/a/b", "/a/bc"))
        self.assertTrue(cyctl._is_within("/a/b", "/a/b/c"))
        self.assertTrue(cyctl._is_within("/a/b", "/a/b"))

    def test_tar_slip_is_rejected(self):
        evil = self._evil_tar("../PWNED.txt")
        with self.assertRaises(ValueError) as cm:
            cyctl._extract(evil, self.dest)
        self.assertIn("越出目标目录", str(cm.exception))
        self.assertFalse(self.outside.exists(), "文件被写到 dest 之外了！")

    def test_tar_absolute_path_is_rejected(self):
        evil = self._evil_tar("/tmp/PWNED_ABS.txt")
        with self.assertRaises(ValueError):
            cyctl._extract(evil, self.dest)

    def test_zip_slip_is_rejected(self):
        evil = self._evil_zip("../PWNED.txt")
        with self.assertRaises(ValueError) as cm:
            cyctl._extract(evil, self.dest)
        self.assertIn("越出目标目录", str(cm.exception))
        self.assertFalse(self.outside.exists(), "文件被写到 dest 之外了！")

    def test_tar_symlink_escape_is_rejected(self):
        """符号链接本身在 dest 内，但指向外部 —— 必须拒绝。"""
        p = self.root / "link.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            info = tarfile.TarInfo(name="escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../etc"
            tf.addfile(info)
        with self.assertRaises(ValueError) as cm:
            cyctl._extract(p, self.dest)
        self.assertIn("符号链接", str(cm.exception))

    def test_normal_archive_still_extracts(self):
        """防护不能误伤正常归档（含子目录与可执行位）。"""
        p = self.root / "ok.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            data = b"#!/bin/sh\necho hi\n"
            info = tarfile.TarInfo(name="cytoscape.sh")
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, _io.BytesIO(data))
            sub = tarfile.TarInfo(name="lib/a.txt")
            sub.size = 3
            tf.addfile(sub, _io.BytesIO(b"abc"))
        cyctl._extract(p, self.dest)
        self.assertTrue((self.dest / "cytoscape.sh").exists())
        self.assertTrue((self.dest / "lib" / "a.txt").exists())
        if os.name != "nt":
            self.assertTrue((self.dest / "cytoscape.sh").stat().st_mode & 0o111,
                            "可执行位必须保留")

    def test_zip_normal_extracts(self):
        p = self.root / "ok.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("cytoscape.bat", "echo hi")
        cyctl._extract(p, self.dest)
        self.assertTrue((self.dest / "cytoscape.bat").exists())

    def test_unsupported_type_raises(self):
        p = self.root / "x.rar"
        p.write_bytes(b"x")
        with self.assertRaises(ValueError):
            cyctl._extract(p, self.dest)


# ---------------------------------------------------------------------------
# 2. 下载稳定性
# ---------------------------------------------------------------------------

class TestDownloadStability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_leaves_no_partial_archive(self):
        """下载失败后不能留下 dest 或 .part —— 否则会被下次调用当成有效缓存。"""
        dest = self.root / "_downloads" / "x.tar.gz"
        with self.assertRaises(cyctl.DownloadError):
            cyctl._download("http://127.0.0.1:1/nope.tar.gz", dest,
                            retries=1, proxies={})
        self.assertFalse(dest.exists(), "失败却留下了目标文件")
        self.assertFalse(dest.with_name(dest.name + ".part").exists(),
                         "失败却留下了 .part 临时文件")

    def test_empty_response_is_rejected(self):
        dest = self.root / "empty.bin"
        with mock.patch.object(cyctl, "urllib") as fake:
            resp = mock.MagicMock()
            resp.headers = {"Content-Length": "0"}
            resp.read.return_value = b""
            fake.request.build_opener.return_value.open.return_value.__enter__.return_value = resp
            with self.assertRaises(cyctl.DownloadError) as cm:
                cyctl._download("http://x/y", dest, retries=1, proxies={})
        self.assertIn("空文件", str(cm.exception))
        self.assertFalse(dest.exists())


# ---------------------------------------------------------------------------
# 3. CLI 输出契约
# ---------------------------------------------------------------------------

class TestCliJsonContract(unittest.TestCase):
    """无论退出码是多少，stdout 都必须是一个可解析的 JSON 对象。

    调用方（Agent）按契约解析 stdout；一旦某条路径 stdout 为空，
    它无法区分「内部错误」与「没有输出」。
    """

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "cyctl.py"), *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    def _json(self, r):
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.fail("stdout 不是合法 JSON:\n%r\n--- stderr ---\n%s"
                      % (r.stdout[:500], r.stderr[:800]))

    def test_unknown_subcommand(self):
        r = self._run("bogus-subcommand")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        p = self._json(r)
        self.assertFalse(p["ok"])

    def test_missing_required_positional(self):
        r = self._run("cmd")           # cmd 缺 command_string
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        self.assertFalse(self._json(r)["ok"])

    def test_unknown_option(self):
        r = self._run("env", "--nope")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        self.assertFalse(self._json(r)["ok"])

    def test_version_is_json(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        p = self._json(r)
        self.assertTrue(p["ok"])
        self.assertEqual(p["name"], "cyctl")
        self.assertTrue(p["version"])

    def test_bad_param_without_equals(self):
        r = self._run("rest", "GET", "networks", "--param", "foo")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        p = self._json(r)
        self.assertFalse(p["ok"])
        self.assertIn("k=v", p["error"])

    def test_bad_body_json(self):
        r = self._run("rest", "POST", "networks", "--body", "{not json")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        self.assertFalse(self._json(r)["ok"])

    def test_missing_workflow_file(self):
        r = self._run("run", "no/such/workflow.json")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        self.assertFalse(self._json(r)["ok"])

    def test_malformed_workflow_json(self):
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d) / "bad.json"
            wf.write_text("{ this is not json", encoding="utf-8")
            r = self._run("run", str(wf))
            self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
            p = self._json(r)
            self.assertFalse(p["ok"])
            self.assertIn("JSON", p["error"])

    def test_unknown_variant(self):
        r = self._run("provision", "--component", "cytoscape",
                      "--variant", "not-a-variant")
        self.assertEqual(r.returncode, cyctl.EXIT_USAGE)
        p = self._json(r)
        self.assertFalse(p["ok"])
        self.assertIn("可选", p["error"])

    def test_describe_is_json_and_lists_new_commands(self):
        p = self._json(self._run("describe"))
        names = {c["name"] for c in p["commands"]}
        self.assertIn("prune", names)
        self.assertEqual(p["output_contract"]["exit_codes"]["5"], "完整性校验失败")


# ---------------------------------------------------------------------------
# 4. provision 语义
# ---------------------------------------------------------------------------

def _spec(sha256=None, source="tofu", observed=None):
    s = {"variant": "temurin17", "kind": "archive",
         "file": "windows-amd64-17.0.5.tar.gz", "size_mb": 41.7,
         "sha256": sha256, "sha256_source": source}
    if observed:
        s["tofu_observed"] = observed
    return s


def _lock(spec):
    return {"pinned": {"cytoscape": "3.10.5", "jre": "17.0.5"},
            "release_base": "https://example.invalid/releases/3.10.5/",
            "jre_base": "https://example.invalid/jres/",
            "artifacts": {"jre": {"windows": [spec]}}, "notes": {}}


class _Args:
    component = "jre"
    variant = None
    force = False
    execute_installer = False
    allow_unverified = False
    no_proxy = True
    runtime = None
    port = None


class TestProvisionSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.args = _Args()
        self.args.runtime = self.tmp.name
        self.rd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, spec, digest="deadbeef", size=100, args=None):
        buf = _io.StringIO()
        a = args or self.args

        def fake_download(url, dest, expected_sha256=None, **kwargs):
            # 忠实复现 _download 的契约：只要传了期望哈希就必须返回 bool 结论。
            # 一律返回 None 会让「校验被跳过」这条路径永远测不到。
            verified = None if expected_sha256 is None else (
                str(digest).lower() == str(expected_sha256).lower())
            return digest, size, verified

        with mock.patch.object(cyctl, "platform_key", return_value="windows"), \
                mock.patch.object(cyctl, "load_lock", return_value=_lock(spec)), \
                mock.patch.object(cyctl, "_download", side_effect=fake_download) as dl, \
                mock.patch.object(cyctl, "_extract") as ex, \
                contextlib.redirect_stdout(buf):
            code = None
            try:
                code = cyctl.cmd_provision(a)
            except SystemExit as e:
                code = e.code
        return code, json.loads(buf.getvalue()), dl, ex

    def test_unverified_artifact_is_blocked_by_default(self):
        """无官方哈希且无 TOFU 基准 -> fail-closed，退出码 5，且不安装。"""
        code, payload, dl, ex = self._call(_spec(sha256=None, source="tofu"))
        self.assertEqual(code, cyctl.EXIT_INTEGRITY)
        self.assertTrue(payload["blocked"])
        self.assertFalse(payload["installed"])
        self.assertEqual(payload["integrity"], "unverified")
        self.assertIn("sha256", payload["hint"])
        dl.assert_called_once()
        ex.assert_not_called()

    def test_allow_unverified_opts_in(self):
        a = _Args()
        a.runtime = self.tmp.name
        a.allow_unverified = True
        code, payload, dl, ex = self._call(_spec(sha256=None), args=a)
        self.assertEqual(code, cyctl.EXIT_OK)
        self.assertTrue(payload["installed"])
        self.assertEqual(payload["integrity"], "unverified-accepted")
        ex.assert_called_once()

    def test_tofu_observed_match_proceeds(self):
        """实测值与首次观测一致 -> 允许继续，并标注来源为 tofu-match。"""
        code, payload, dl, ex = self._call(
            _spec(sha256=None, observed="deadbeef"))
        self.assertEqual(code, cyctl.EXIT_OK)
        self.assertEqual(payload["integrity"], "tofu-match")
        self.assertTrue(payload["installed"])

    def test_official_hash_mismatch_fails_hard(self):
        code, payload, dl, ex = self._call(
            _spec(sha256="a" * 64, source="official"), digest="b" * 64)
        self.assertEqual(code, cyctl.EXIT_INTEGRITY)
        self.assertFalse(payload["ok"])
        ex.assert_not_called()

    def test_official_hash_match_installs(self):
        code, payload, dl, ex = self._call(
            _spec(sha256="c" * 64, source="official"), digest="c" * 64)
        self.assertEqual(code, cyctl.EXIT_OK)
        self.assertTrue(payload["verified"])
        ex.assert_called_once()

    def test_expected_hash_but_no_verdict_is_rejected(self):
        """有期望哈希却拿到 None 结论 —— 等于校验被静默跳过，必须报错而非放行。"""
        buf = _io.StringIO()
        with mock.patch.object(cyctl, "platform_key", return_value="windows"), \
                mock.patch.object(cyctl, "load_lock", return_value=_lock(
                    _spec(sha256="d" * 64, source="official"))), \
                mock.patch.object(cyctl, "_download",
                                  return_value=("e" * 64, 100, None)), \
                mock.patch.object(cyctl, "_extract") as ex, \
                contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                cyctl.cmd_provision(self.args)
        self.assertEqual(cm.exception.code, cyctl.EXIT_INTEGRITY)
        ex.assert_not_called()

    def test_already_installed_is_idempotent(self):
        """目标已存在且未加 --force -> 直接返回，绝不重复下载或覆盖。

        旧实现只打一行「--force 可强制重装」的日志却继续解压覆盖，
        日志与行为不符。
        """
        (self.rd / "jre").mkdir()
        code, payload, dl, ex = self._call(_spec(sha256="c" * 64, source="official"))
        self.assertEqual(code, cyctl.EXIT_OK)
        self.assertTrue(payload["already_installed"])
        dl.assert_not_called()
        ex.assert_not_called()

    def test_force_reinstalls_even_if_present(self):
        (self.rd / "jre").mkdir()
        a = _Args()
        a.runtime = self.tmp.name
        a.force = True
        code, payload, dl, ex = self._call(_spec(sha256="c" * 64, source="official"),
                                          digest="c" * 64, args=a)
        self.assertEqual(code, cyctl.EXIT_OK)
        dl.assert_called_once()
        ex.assert_called_once()


# ---------------------------------------------------------------------------
# 5. prune
# ---------------------------------------------------------------------------

class TestPrune(unittest.TestCase):
    def test_prune_dry_run_then_delete(self):
        with tempfile.TemporaryDirectory() as d:
            dl = Path(d) / "_downloads"
            dl.mkdir()
            (dl / "a.tar.gz").write_bytes(b"x" * 2048)
            (dl / "b.zip").write_bytes(b"y" * 1024)

            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "cyctl.py"), "prune", "--runtime", d],
                capture_output=True, text=True, encoding="utf-8")
            p = json.loads(r.stdout)
            self.assertTrue(p["dry_run"])
            self.assertEqual(p["removed_count"], 2)
            self.assertTrue((dl / "a.tar.gz").exists(), "dry-run 不应删除文件")

            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "cyctl.py"), "prune", "--runtime", d, "--yes"],
                capture_output=True, text=True, encoding="utf-8")
            p = json.loads(r.stdout)
            self.assertFalse(p["dry_run"])
            self.assertFalse((dl / "a.tar.gz").exists())
            self.assertFalse((dl / "b.zip").exists())


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


# ---------------------------------------------------------------------------
# 5. commands 子命令的安全门禁
# ---------------------------------------------------------------------------

class TestCommandsGuard(unittest.TestCase):
    """`cyctl commands` 默认不得触发「查询即执行」。

    背景：CyREST 的 `GET /commands/{ns}/{cmd}` 对**不需要参数**的命令就是执行它。
    实测 `cyctl commands command quit` 真的把 Cytoscape 关掉了；更麻烦的是 GUI
    模式下 JVM 卡在退出确认对话框上不退出，留下一个占 1.3 GB 的僵尸进程，而端口
    已经关闭 —— 表现成「没有进程在监听，却还有进程吃内存」。
    因此：危险动词永久拒绝；其余命令默认只读地列命名空间清单。
    """

    def _run(self, *argv):
        #: 现场探测一个空闲端口，证明**拦截发生在发请求之前**：
        #: 若实现真的发了请求，会得到连接错误而不是干净的拒绝。
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cyctl.py"), "commands",
             "--port", _closed_port(), *argv],
            capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_dangerous_verbs_are_blocked(self):
        for verb in ("quit", "exit", "shutdown", "halt"):
            with self.subTest(verb=verb):
                p = self._run("command", verb)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                payload = json.loads(p.stdout)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["blocked"], "command %s" % verb)

    def test_allow_execute_does_not_unlock_dangerous_verbs(self):
        """--allow-execute 是「允许走执行端点」，不是「允许关引擎」。"""
        p = self._run("command", "quit", "--allow-execute")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["blocked"], "command quit")

    def test_default_path_never_reports_executed(self):
        """非危险命令默认走只读清单；端口不通时应是远端错误（4），
        且输出里绝不能出现 executed=true。
        """
        p = self._run("network", "export")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        payload = json.loads(p.stdout)
        self.assertFalse(payload["ok"])
        self.assertNotIn("executed", payload)

    # ---- executed 标志是否如实 ------------------------------------------
    # /commands/{ns}/{cmd} 有两种回应：不需要参数 -> 真执行；需要参数 -> 只回参数清单。
    # 无条件报 executed=true 会让人以为副作用已经发生 —— 医学/生信流程里这是危险的误报。

    def _main_capture(self, argv):
        captured = {}
        with mock.patch.object(cyctl, "emit",
                               side_effect=lambda o: captured.update(o)):
            cyctl.main(argv)
        return captured

    def test_help_mode_is_not_reported_as_executed(self):
        help_body = ("Available arguments for 'network export':\n"
                     "  network\n  outputFile\n")
        with mock.patch.object(cyctl.cyrest, "cyrest_request",
                               return_value=(help_body, help_body, 200)):
            payload = self._main_capture(
                ["commands", "network", "export", "--allow-execute"])
        self.assertTrue(payload["ok"])
        self.assertIs(payload["executed"], False,
                      "只拿到参数清单却报 executed=true")
        self.assertEqual(payload["mode"], "help")
        self.assertIn("executed=false", payload["caution"])

    def test_real_run_is_reported_as_executed(self):
        real_body = "file\n"
        with mock.patch.object(cyctl.cyrest, "cyrest_request",
                               return_value=(real_body, real_body, 200)):
            payload = self._main_capture(
                ["commands", "network", "list", "--allow-execute"])
        self.assertTrue(payload["ok"])
        self.assertIs(payload["executed"], True)
        self.assertEqual(payload["mode"], "executed")

    def test_allow_execute_is_declared_in_describe(self):
        """describe 必须把 --allow-execute 暴露出来，否则 Agent 无从得知。"""
        p = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cyctl.py"), "describe"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        payload = json.loads(p.stdout)
        entry = [c for c in payload["commands"] if c["name"] == "commands"][0]
        self.assertIn("--allow-execute", entry["args"])


# ---------------------------------------------------------------------------
# 6. run / rest 通道的契约
# ---------------------------------------------------------------------------

class TestRunAndRestContract(unittest.TestCase):
    """两条契约，都是本轮实测踩出来的问题（不是凭想象写的）。

    1. `rest` 必须能声明 Accept。CyREST 里 `commands` / `commands/{ns}` 这类 help
       端点**只接受 text/plain**，发默认的 application/json 会得到 HTTP 406 ——
       而 workflows/demo.workflow.json 的第一步恰恰就是 `rest GET commands`。
    2. 工作流里的相对路径必须相对**工作流文件所在目录**解析，命令字符串要支持
       `{input}` 占位符。执行期真正读文件的是 Cytoscape 引擎，它的 cwd 是自己
       的安装目录，直接写相对路径会解析到不相干的位置，且报错信息里看不出是
       路径问题；写死绝对路径又不可移植。
    """

    def _describe(self):
        p = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "cyctl.py"), "describe"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        return json.loads(p.stdout)

    def test_rest_exposes_accept(self):
        cmds = {c["name"]: c for c in self._describe()["commands"]}
        self.assertIn("--accept", cmds["rest"]["args"])

    def test_run_documents_placeholders(self):
        cmds = {c["name"]: c for c in self._describe()["commands"]}
        self.assertIn("{input}", cmds["run"]["note"])

    def test_demo_workflow_is_valid_and_uses_placeholder(self):
        """示例工作流必须是合法 JSON、用 {input} 而非写死路径，且第一步声明 text/plain。"""
        wf = json.loads((ROOT / "workflows" / "demo.workflow.json")
                        .read_text(encoding="utf-8"))
        text = json.dumps(wf, ensure_ascii=False)
        self.assertIn("{input}", text, "示例工作流应使用 {input} 占位符")
        first = wf["steps"][0]["rest"]
        self.assertEqual(first.get("accept"), "text/plain",
                         "help 端点必须声明 Accept: text/plain，否则 406")

    def test_hash_inputs_marks_missing_without_raising(self):
        """缺失的输入必须被标成 missing，而不是抛异常。"""
        res = cyctl._hash_inputs([ROOT / "no" / "such" / "file.bin"])
        self.assertEqual(res[0]["kind"], "missing")


# ---------------------------------------------------------------------------
# 危险命令的统一闸门
# ---------------------------------------------------------------------------


class TestDangerCommandGuard(unittest.TestCase):
    """CyREST 的 ``GET /commands/{ns}/{cmd}`` 对**无参命令**就是执行它。

    quit / exit 类命令会让引擎退出（GUI 模式下还会留下占 GB 的僵尸进程）。
    `cyctl commands` 早就拦了，但 `cmd` / `rest` / 工作流是另外的入口 ——
    拦不住等于没拦：一条 `cyctl rest GET commands/command/quit` 就能关掉引擎。
    这一组把「所有入口过同一道闸」钉死。
    """

    def test_recognises_dangerous_command_strings(self):
        for c in ("command quit", "command exit", "command shutdown", "command halt"):
            self.assertEqual(cyrest.dangerous_command(c), c)

    def test_does_not_flag_normal_commands(self):
        for c in ("network list", "layout get preferred",
                  'network set attribute name="halt"',     # halt 在参数值里，不是命令名
                  "table export outputFile=x"):
            self.assertIsNone(cyrest.dangerous_command(c), c)

    def test_recognises_dangerous_operations(self):
        for op, want in (("commands/command/quit", "command quit"),
                         ("/commands/command/exit", "command exit"),
                         ("commands/command/quit?x=1", "command quit")):
            self.assertEqual(cyrest.dangerous_operation(op), want, op)

    def test_normal_operations_pass(self):
        for op in ("networks/196/nodes", "commands/network",
                   "commands/layout/get preferred", "commands/network/export",
                   "networks/196/views/2292.png"):
            self.assertIsNone(cyrest.dangerous_operation(op), op)

    def test_run_command_refuses(self):
        with self.assertRaises(cyrest.CyRestError) as cm:
            cyrest.run_command("command quit", "http://localhost:1/v1")
        self.assertEqual(cm.exception.kind, "danger")

    def test_call_operation_refuses(self):
        with self.assertRaises(cyrest.CyRestError) as cm:
            cyrest.call_operation("commands/command/quit", "http://localhost:1/v1")
        self.assertEqual(cm.exception.kind, "danger")

    def test_download_refuses(self):
        with self.assertRaises(cyrest.CyRestError) as cm:
            cyrest.cyrest_download("commands/command/quit", "http://localhost:1/v1",
                                   os.path.join(tempfile.gettempdir(), "_never.bin"))
        self.assertEqual(cm.exception.kind, "danger")

    def test_workflow_step_refuses(self):
        """工作流文件同样可能是外部给的，不能假定它安全。"""
        wf = {
            "name": "danger-probe",
            "steps": [{"command": "command quit"}],
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wf.json"
            p.write_text(json.dumps(wf), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "cyctl.py"), "run", str(p),
                 "--base-url", "http://localhost:59999/v1", "--timeout", "3"],
                capture_output=True, text=True, encoding="utf-8", timeout=90)
            payload = json.loads(r.stdout)
            # 引擎不可达时会在跑步骤前就退出（3）；可达时步骤必须被拒。
            self.assertFalse(payload["ok"])
            self.assertIn(r.returncode, (cyctl.EXIT_ENV, cyctl.EXIT_REMOTE), r.stdout)

    def test_cli_uses_usage_exit_code(self):
        for argv in (["cmd", "command quit"],
                     ["rest", "GET", "commands/command/quit"],
                     ["rest", "GET", "commands/command/quit", "--out",
                      os.path.join(tempfile.gettempdir(), "_never2.bin")]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "cyctl.py"), *argv],
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=90)
            self.assertEqual(r.returncode, cyctl.EXIT_USAGE, (argv, r.stdout, r.stderr))
            payload = json.loads(r.stdout)
            self.assertFalse(payload["ok"], argv)
            self.assertEqual(payload["kind"], "danger", argv)


    def test_danger_verb_tables_do_not_drift(self):
        """cyctl 与 cyrest 各有一份危险动词表，必须完全相同。

        分头维护是隐患：漏掉一个动词，对应的命令就能从某个入口溜过去。
        这里把「两份表一致」做成断言，而不是靠人记得同步改。
        """
        self.assertEqual(set(cyctl._DANGER_VERBS), set(cyrest.DANGER_VERBS))

    def test_stop_quit_request_uses_json_accept(self):
        """stop 的优雅退出走 POST，Accept 必须是 application/json。

        实测：POST /v1/commands/{ns}/{cmd} + Accept: text/plain -> HTTP 406，
        只有 GET 的 help 端点才要 text/plain。原来这里写成 text/plain，
        于是优雅退出每次都被 406 挡下，又被「连接中断不代表失败」那句注释盖住 ——
        表现为 stop 永远 graceful:false，一直靠杀进程收场。这条断言防止它退回去。
        """
        src = (SCRIPTS / "cyctl.py").read_text(encoding="utf-8")
        i = src.find("def cmd_stop")
        self.assertGreater(i, 0)
        body = src[i:src.find("\ndef ", i + 10)]
        j = body.find("/commands/command/quit")
        self.assertGreater(j, 0, "cmd_stop 里找不到退出请求端点")
        snippet = body[max(0, j - 400):j + 400]
        self.assertNotIn('accept="text/plain"', snippet,
                         "优雅退出是 POST，Accept 只能是 application/json")
        self.assertIn('accept="application/json"', snippet)

    def test_stop_treats_4xx_as_real_failure_not_disconnect(self):
        """4xx 不能解释成「进程退出导致连接中断」，否则真实故障被静默吞掉。"""
        src = (SCRIPTS / "cyctl.py").read_text(encoding="utf-8")
        i = src.find("def cmd_stop")
        body = src[i:src.find("\ndef ", i + 10)]
        self.assertIn("400 <= exc.status < 500", body)


# ---------------------------------------------------------------------------
# operation 路径编码
# ---------------------------------------------------------------------------


class TestOperationEncoding(unittest.TestCase):
    """operation 里的空格会让 urllib 抛 InvalidURL。未捕获时退出码是 1 ——
    违反「退出码只能是 0/2/3/4/5」的契约（实测 `cyctl rest GET "commands/network/get preferred"`）。"""

    def test_space_is_percent_encoded(self):
        self.assertEqual(cyrest._encode_operation("commands/layout/get preferred"),
                         "commands/layout/get%20preferred")

    def test_existing_escape_is_not_double_encoded(self):
        self.assertEqual(cyrest._encode_operation("commands/layout/get%20preferred"),
                         "commands/layout/get%20preferred")

    def test_query_string_is_left_alone(self):
        self.assertEqual(cyrest._encode_operation("networks/1/tables/defaultnode/rows?limit=2"),
                         "networks/1/tables/defaultnode/rows?limit=2")

    def test_leading_slash_stripped(self):
        self.assertEqual(cyrest._encode_operation("/networks/196"), "networks/196")

    def test_cli_does_not_return_internal_error_code(self):
        """真实走一次 CLI：带空格的 operation 不得以退出码 1 结束。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "cyctl.py"), "rest", "GET",
             "commands/layout/get preferred", "--accept", "text/plain",
             "--base-url", "http://localhost:59999/v1", "--timeout", "3"],
            capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertNotEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(r.returncode, (0, 2, 3, 4, 5))
        json.loads(r.stdout)


# ---------------------------------------------------------------------------
# --help 的输出契约
# ---------------------------------------------------------------------------


class TestHelpContract(unittest.TestCase):
    """stdout 恒为一个 JSON 对象（唯一例外是 cyctl bridge）。

    argparse 默认把 help 当纯文本打到 stdout；`error()` 与 `--version` 早被
    JSON 化，help 是漏掉的一处 —— 按契约解析的 Agent 执行 `cyctl --help` 会崩。
    """

    def _run(self, *argv):
        return subprocess.run([sys.executable, str(SCRIPTS / "cyctl.py"), *argv],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_top_level_help_is_json(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("usage", payload)
        self.assertIn("commands", payload["help"])

    def test_short_help_is_json(self):
        payload = json.loads(self._run("-h").stdout)
        self.assertTrue(payload["ok"])

    def test_subcommand_help_is_json(self):
        r = self._run("rest", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("--out", payload["help"])


# ---------------------------------------------------------------------------
# 二进制落盘
# ---------------------------------------------------------------------------


class _TinyServer:
    """一次性 HTTP 服务，用来测字节通道的边界（长度不符 / JSON 错误信封）。"""

    def __init__(self, body, content_type, declared_length=None):
        import http.server
        import threading

        payload = body
        ctype = content_type
        declared = len(body) if declared_length is None else declared_length

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):                        # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(declared))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):               # 静音
                pass

        self._srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._srv.server_port

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class TestBinaryDownloadContract(unittest.TestCase):
    """默认通道把响应体按 UTF-8 解码（errors="replace"），二进制会被替换字符
    **不可逆**地破坏，而响应仍是 ok:true —— 成功信封里装垃圾。
    实测一张 72 KB 的 PNG 变成含 28,798 个替换字符的字符串。"""

    def test_rest_exposes_out(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "cyctl.py"), "describe"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        cmds = {c["name"]: c for c in json.loads(r.stdout)["commands"]}
        self.assertIn("--out", cmds["rest"]["args"])

    def test_bytes_are_written_verbatim(self):
        payload = bytes(range(256)) * 8          # 含 0x00 与所有非法 UTF-8 序列
        srv = _TinyServer(payload, "application/octet-stream")
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "blob.bin"
                info = cyrest.cyrest_download("blob", srv.url, str(dest), timeout=15)
                self.assertEqual(dest.read_bytes(), payload, "落盘字节与响应体不一致")
                self.assertEqual(info["size"], len(payload))
                self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
                # 反证：这就是修复前的行为 —— 同一份字节经默认通道会被毁掉。
                self.assertIn("\ufffd", payload.decode("utf-8", errors="replace"))
        finally:
            srv.close()

    def test_content_length_mismatch_is_rejected(self):
        """声明 100 字节、实收 3 字节：必须失败，不得把截断当成功产物。"""
        srv = _TinyServer(b"abc", "application/octet-stream", declared_length=100)
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "x.bin"
                with self.assertRaises(cyrest.CyRestError):
                    cyrest.cyrest_download("blob", srv.url, str(dest), timeout=15)
                self.assertFalse(dest.exists(), "校验失败却留下了产物")
                self.assertFalse(Path(str(dest) + ".part").exists(), "临时文件没清掉")
        finally:
            srv.close()

    def test_json_error_envelope_is_not_saved_as_product(self):
        """HTTP 200 + JSON 错误信封：不能把这张错误页当成功产物留下来。"""
        body = json.dumps({"data": {}, "errors": [{"status": 500, "message": "boom"}]}
                          ).encode("utf-8")
        srv = _TinyServer(body, "application/json")
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "x.png"
                with self.assertRaises(cyrest.CyRestError):
                    cyrest.cyrest_download("views/1.png", srv.url, str(dest), timeout=15)
                self.assertFalse(dest.exists())
                self.assertFalse(Path(str(dest) + ".part").exists())
        finally:
            srv.close()

    def test_http_error_is_reported_with_status(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):                        # noqa: N802
                self.send_error(404, "nope")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "x.bin"
                with self.assertRaises(cyrest.CyRestError) as cm:
                    cyrest.cyrest_download("blob", "http://127.0.0.1:%d" % srv.server_port,
                                           str(dest), timeout=15)
                self.assertEqual(cm.exception.status, 404)
                self.assertFalse(dest.exists())
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
