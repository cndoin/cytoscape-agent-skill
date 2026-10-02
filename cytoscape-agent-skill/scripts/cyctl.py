#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cyctl —— Cytoscape 引擎的确定性驱动层 / Agent Skill 执行器。

设计契约（与 gitpilot 保持一致，便于任何 Agent 解析）：
  * stdout  只放机器可读 JSON，一次调用仅输出一个 JSON 对象
  * stderr  放人类可读日志、进度、警告
  * 退出码  0=成功 2=用法错误 3=环境未就绪 4=远端(Cytoscape)错误 5=完整性校验失败

零第三方依赖（仅标准库），Python 3.8+。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import traceback
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import cyrest  # noqa: E402  (同目录模块)
import enginediscovery as edis  # noqa: E402  (同目录模块)
import hostmatrix as hmat  # noqa: E402  (同目录模块)
import mcpbridge  # noqa: E402  (同目录模块)

CYCTL_VERSION = "0.4.0"
EXIT_OK, EXIT_USAGE, EXIT_ENV, EXIT_REMOTE, EXIT_INTEGRITY = 0, 2, 3, 4, 5

# Keep both machine output and human diagnostics portable on Windows legacy
# consoles. Python's JSON payload is UTF-8 by contract; stderr messages may
# contain the same Unicode text and are captured as UTF-8 by callers/tests.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

SKILL_ROOT = _HERE.parent
LOCK_PATH = SKILL_ROOT / "assets" / "versions.lock.json"


# ---------------------------------------------------------------------------
# 输出契约
# ---------------------------------------------------------------------------

def emit(obj):
    """把结果作为唯一一个 JSON 对象写到 stdout。"""
    # Windows terminals may expose a legacy code page (for example GBK) that
    # cannot encode valid Unicode JSON content. Write UTF-8 bytes directly so
    # every code path preserves the machine-readable stdout contract.
    payload = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(payload.encode("utf-8"))
        stream.flush()
    else:  # StringIO and other text-only streams used by embedders/tests.
        sys.stdout.write(payload)
        sys.stdout.flush()


def log(msg):
    """人类可读信息 -> stderr。"""
    sys.stderr.write("[cyctl] %s\n" % msg)
    sys.stderr.flush()


def _exit_for(exc):
    """把 CyRestError 的 kind 映射成契约里的退出码。

    ``usage``（请求本身不合法）与 ``danger``（被安全策略拒绝）都属于**用法错误**，
    是调用方该改参数的情形；其余算远端错误。曾经一律返回 4，
    而非法 URL 还会穿透到兜底变成 1 —— 两者都违反「退出码只能是 0/2/3/4/5」。
    """
    return EXIT_USAGE if getattr(exc, "kind", None) in ("usage", "danger") else EXIT_REMOTE


def fail(code, message, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    emit(payload)
    sys.exit(code)


class ArgParser(argparse.ArgumentParser):
    """把 argparse 自身的报错也纳入 JSON 输出契约。

    默认 argparse 在用法错误时会向 stderr 打印 usage 然后 sys.exit(2)，
    结果是 stdout 完全为空、退出码 2 —— 违反「stdout 恒为一个 JSON 对象」的约定，
    按契约解析 stdout 的 Agent 会直接崩溃。这里覆盖 error() 把它 JSON 化。
    """

    def print_help(self, file=None):
        """把 --help 也纳入 JSON 输出契约。

        argparse 默认把 help 当纯文本打到 stdout。而本项目的契约是
        「stdout 恒为一个 JSON 对象（唯一例外是 cyctl bridge）」——
        一个按契约解析 stdout 的 Agent 执行 `cyctl --help` 会直接崩。
        `error()` 与 `--version` 早就为同一个原因被 JSON 化了，
        help 是漏掉的一处（审计实测：rc=0 但 stdout 是纯文本）。
        """
        emit({"ok": True, "usage": self.format_usage().strip(),
              "help": self.format_help().rstrip()})

    def error(self, message):
        emit({"ok": False, "error": "用法错误: %s" % message,
              "usage": self.format_usage().strip()})
        sys.exit(EXIT_USAGE)


class VersionAction(argparse.Action):
    """--version 同样输出 JSON（默认 action 会向 stdout 打印纯文本）。"""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        emit({"ok": True, "name": "cyctl", "version": CYCTL_VERSION})
        parser.exit(0)


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------

def detect_java():
    """探测可用的 Java 17+。返回 dict。"""
    info = {"found": False, "version": None, "major": None, "path": None,
            "source": None, "java_home": os.environ.get("JAVA_HOME")}

    candidates = []
    if info["java_home"]:
        exe = "java.exe" if os.name == "nt" else "java"
        candidates.append(str(Path(info["java_home"]) / "bin" / exe))
    candidates.append(shutil.which("java") or "")

    # cyctl 自己供给的 JRE 优先（保证版本可控）
    bundled = _bundled_jre_java()
    if bundled:
        candidates.insert(0, bundled)

    for cand in candidates:
        if not cand or not Path(cand).exists():
            continue
        try:
            out = subprocess.run([cand, "-version"], capture_output=True, text=True, timeout=30)
            text = (out.stderr or "") + (out.stdout or "")
            m = re.search(r'version "?(\d+)(?:\.(\d+))?', text)
            if not m:
                continue
            major = int(m.group(1))
            if major == 1 and m.group(2):      # 旧式 "1.8.0_503"
                major = int(m.group(2))
            info.update(found=True, path=cand, version=m.group(0).split('"')[-1] or text.split()[2],
                        major=major,
                        source="bundled" if cand == bundled else ("JAVA_HOME" if info["java_home"] and cand.startswith(info["java_home"]) else "PATH"))
            info["ok_for_cytoscape"] = major >= 17
            return info
        except Exception as exc:               # noqa: BLE001
            log("探测 java 失败 (%s): %s" % (cand, exc))

    info["ok_for_cytoscape"] = False
    return info


def _runtime_dir(args_dir=None):
    if args_dir:
        return Path(args_dir).expanduser().resolve()
    env = os.environ.get("CYCTL_RUNTIME_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return SKILL_ROOT / "runtime"


def _bundled_jre_java():
    jre = _runtime_dir() / "jre"
    exe = "java.exe" if os.name == "nt" else "bin/java"
    p = jre / "bin" / exe
    if p.exists():
        return str(p)
    # 官方 JRE 压缩包解压后可能多一层目录
    for sub in sorted(jre.glob("*/bin/" + exe)):
        return str(sub)
    return None


def find_launcher(runtime_dir):
    """在 runtime 目录里找 cytoscape 启动脚本。"""
    runtime_dir = Path(runtime_dir)
    if os.name == "nt":
        patterns = ["cytoscape.bat", "*/cytoscape.bat", "*/*/cytoscape.bat"]
    else:
        patterns = ["cytoscape.sh", "*/cytoscape.sh", "*/*/cytoscape.sh"]
    for pat in patterns:
        hits = sorted(runtime_dir.glob(pat))
        if hits:
            return hits[0]
    return None


def platform_key():
    s = _platform.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


# ---------------------------------------------------------------------------
# versions.lock.json
# ---------------------------------------------------------------------------

def load_lock():
    if not LOCK_PATH.exists():
        fail(EXIT_ENV, "缺少版本锁定文件: %s" % LOCK_PATH)
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 子命令：env / describe
# ---------------------------------------------------------------------------

def cmd_env(args):
    plat = platform_key()
    java = detect_java()
    rd = _runtime_dir(args.runtime)
    launcher = find_launcher(rd)

    reachable, status_info = False, None
    base = cyrest.resolve_base_url(args.base_url, args.port)
    try:
        result, _, _ = cyrest.cyrest_request("GET", base, timeout=5)
        reachable, status_info = True, result
    except cyrest.CyRestError as exc:
        status_info = exc.to_dict()
    except Exception as exc:                    # noqa: BLE001
        status_info = {"error": str(exc)}

    emit({
        "ok": True,
        "cyctl_version": CYCTL_VERSION,
        "platform": plat,
        "python": sys.version.split()[0],
        "runtime_dir": str(rd),
        "java": java,
        "cytoscape": {
            "launcher": str(launcher) if launcher else None,
            "installed": bool(launcher),
            "pinned_version": load_lock()["pinned"]["cytoscape"],
        },
        "cyrest": {"base_url": base, "reachable": reachable, "detail": status_info},
        "ready": bool(launcher and java.get("ok_for_cytoscape") and reachable),
    })
    return EXIT_OK


def cmd_describe(args):
    """自描述：供 Agent 自动发现可用能力（机器可读）。"""
    emit({
        "ok": True,
        "name": "cyctl",
        "version": CYCTL_VERSION,
        "summary": "驱动原版 Cytoscape 引擎（CyREST / Commands API），结果与 Cytoscape 原生一致。",
        "determinism": "AI 仅负责选择调用哪条官方命令，不参与任何计算；所有数值由 Cytoscape Java 引擎产生。",
        "output_contract": {"stdout": "单个 JSON 对象", "stderr": "人类日志",
                            "exit_codes": {"0": "成功", "2": "用法错误", "3": "环境未就绪",
                                           "4": "远端错误", "5": "完整性校验失败"}},
        "engines": {
            "bundled": "cyctl provision 下载的引擎，版本锁定，结果可复现（推荐）",
            "system": "系统上已安装的 Cytoscape，用 cyctl discover 探测",
            "attach": "连接已在运行的实例，不启动新进程",
        },
        "platforms": ["windows", "linux", "macos"],
        "display_modes": {
            "desktop": "有图形界面",
            "headless": "Linux 无 DISPLAY 时自动用 xvfb-run 包装（官方无 -N 无头开关）",
        },
        "hosts": hmat.host_ids(),
        "commands": [
            {"name": "env", "desc": "探测 Java / Cytoscape / CyREST 可用性"},
            {"name": "doctor", "desc": "全面体检，逐项给出「缺什么 + 怎么补」"},
            {"name": "discover", "desc": "跨平台发现已有的 Cytoscape 与 Java 17"},
            {"name": "provision", "desc": "按 versions.lock.json 下载并校验 Cytoscape / JRE",
             "args": ["--component cytoscape|jre", "--variant", "--force", "--allow-unverified"]},
            {"name": "start", "desc": "启动 Cytoscape",
             "args": ["--engine auto|bundled|system|attach", "--headless", "--launcher"]},
            {"name": "wait", "desc": "轮询 /v1/ 直到引擎就绪"},
            {"name": "status", "desc": "读取 /v1/ 引擎信息"},
            {"name": "commands", "desc": "查询引擎**真实**命令清单，避免猜名字",
             "args": ["[namespace]", "[command]", "--allow-execute"],
             "note": "CyREST 的 help 端点只接受 Accept: text/plain，传 json 会 406。"
                     "给出 [command] 时默认只读地到命名空间清单里查存在性；"
                     "要拿参数清单须加 --allow-execute（/commands/{ns}/{cmd} 对无参"
                     "命令等于执行）。quit/exit 类命令永久拒绝，关引擎请用 cyctl stop"},
            {"name": "cmd", "desc": "执行一条 Cytoscape 命令", "args": ["command_string", "--get|--post"]},
            {"name": "rest", "desc": "调用 CyREST 函数",
             "args": ["method", "operation", "--param", "--body", "--accept", "--out"],
             "note": "help 类端点（commands / commands/{ns}）只接受 "
                     "Accept: text/plain，发默认的 application/json 会 406。"
                     "导出二进制产物（views/{id}.png|pdf、networks/{id}.cx）必须用 --out："
                     "默认通道会把响应体按 UTF-8 解码，二进制会被替换字符不可逆地破坏，"
                     "而响应仍是 ok:true"},
            {"name": "run", "desc": "按工作流文件确定性执行并产出溯源清单",
             "args": ["workflow.json", "--out"],
             "note": "inputs 的相对路径相对**工作流文件所在目录**解析；命令字符串支持 "
                     "{workflow_dir} / {input_dir} / {input} / {input0} 占位符"},
            {"name": "mcp", "desc": "输出全宿主 MCP 接入矩阵（可 --write-config 安全落盘）",
             "args": ["--host", "--project", "--write-config"]},
            {"name": "bridge", "desc": "stdio ↔ Streamable HTTP 桥（供仅支持 stdio 的宿主）",
             "note": "该子命令 stdout 是 MCP 协议本身，不是 cyctl 的 JSON 信封"},
            {"name": "prune", "desc": "清理下载缓存（默认 dry-run，--yes 才删除）"},
            {"name": "stop", "desc": "停止由 cyctl 启动的 Cytoscape"},
        ],
    })
    return EXIT_OK


# ---------------------------------------------------------------------------
# 子命令：provision
# ---------------------------------------------------------------------------

def _sha256_of(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


class DownloadError(Exception):
    """下载失败（网络层或完整性），由调用方转为结构化输出。"""


#: 下载用的 User-Agent。部分 CDN 对 urllib 的默认 UA 不友好；
#: 显式声明一个能标识来源的 UA，既礼貌也避免被当作爬虫拒绝。
USER_AGENT = "cyctl/%s (+cytoscape-agent-skill)" % CYCTL_VERSION


def _resolve_proxies(mode, explicit=None):
    """把代理策略解析成 urllib 的 proxies 字典（{} 表示直连）。

    mode 取值：
      none   直连
      env    只读环境变量（与 curl 的行为一致）
      system 读系统设置（Windows 注册表 / macOS 网络偏好）
      auto   返回 None，表示需要运行时探测 —— 见 choose_proxies
    """
    if explicit:
        return {"http": explicit, "https": explicit}
    if mode == "none":
        return {}
    if mode == "env":
        return dict(urllib.request.getproxies_environment())
    if mode == "system":
        return dict(urllib.request.getproxies())
    return None


def _probe_url(url, proxies, timeout=10):
    """轻量探测 URL 是否可达（只取第 1 个字节）。

    返回 (ok, detail)。用 Range 请求而非 HEAD：部分 CDN 对 HEAD 返回 405，
    但对 Range GET 正常响应，用 HEAD 会误判为不可达。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(dict(proxies or {})))
    req = urllib.request.Request(
        url, headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return True, "HTTP %s" % resp.status
    except urllib.error.HTTPError as exc:
        # 4xx 也可能是「可达但拒绝」；只有 2xx/3xx 才算可达。
        return (200 <= exc.code < 400), "HTTP %s" % exc.code
    except Exception as exc:                      # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def choose_proxies(url, mode="auto", explicit=None, timeout=10):
    """决定下载走哪条网络路径，并留下可审计的决策记录。

    返回 (proxies_dict, info)。

    为什么要 auto 而不是直接用 urllib 的默认行为：
        urllib.request.getproxies() 除了环境变量，还会读 **Windows 注册表的 WinINET
        设置**和 **macOS 的系统网络设置**。这比「环境变量代理」宽得多 —— 后果是
        用户从没配过 http_proxy，却被悄悄塞进一个系统级代理。实测本机
        （Windows，系统里有个 127.0.0.1:7890 的代理）此时代理只有约 180 KB/s，
        而直连是 10.5 MB/s，相差约 57 倍；而且失败信息里只有「慢」，没有「走了代理」，
        用户根本无从定位。

    因此 auto 的策略是：**直连优先，不可达才回落到系统代理**，
    并把最终选择写进 info，输出到 stdout 让调用方看得见。
    """
    if explicit:
        p = _resolve_proxies("none", explicit)
        return p, {"mode": "explicit", "proxies": p, "decided": "explicit",
                   "note": "按显式 URL 使用代理。"}

    if mode != "auto":
        p = _resolve_proxies(mode)
        return p, {"mode": mode, "proxies": p, "decided": mode,
                   "note": "按 --proxy %s 指定。" % mode}

    ok, detail = _probe_url(url, {}, timeout=timeout)
    if ok:
        return {}, {"mode": "auto", "proxies": {}, "decided": "direct",
                    "probe": detail, "note": "直连可达，未使用代理。"}

    sysp = dict(urllib.request.getproxies())
    if sysp:
        ok2, detail2 = _probe_url(url, sysp, timeout=timeout)
        return sysp, {
            "mode": "auto", "proxies": sysp,
            "decided": "system-proxy" if ok2 else "system-proxy-unverified",
            "probe": detail2, "direct_error": detail,
            "note": ("直连不可达，回落到系统/环境代理。"
                     if ok2 else "直连与代理探测均未成功，仍按系统代理尝试下载。"),
        }
    return {}, {"mode": "auto", "proxies": {}, "decided": "direct-unverified",
                "probe": detail,
                "note": "直连探测未成功，且系统未配置任何代理。若下载失败，"
                        "请用 --proxy <url> 显式指定代理。"}


def _download(url, dest, expected_sha256=None, expected_size=None,
              retries=3, proxies=None):
    """下载到 dest.part，边下边算 sha256，完成后原子改名。

    返回 (sha256, size, verified)。

    稳定性要点（每一项都对应一个曾真实发生过的失败模式）：
      * 写临时文件 + os.replace 原子落地 —— 中断只会留下 .part，
        不会出现「半截归档被下次调用当成有效缓存而复用」；
      * 校验 Content-Length 与实际字节数 —— 提前识别截断；
      * 有限次重试 + 退避 —— 慢链路上的瞬时中断不至于让 200MB 白下。

    代理由调用方通过 proxies 显式决定（见 choose_proxies）：None / {} 表示直连。
    这里默认「直连」而非「用系统代理」是刻意的 —— urllib 的 getproxies() 在
    Windows 上会读注册表、在 macOS 上读系统网络设置，等于把一个用户从未配置过的
    代理悄悄套上（实测因此慢约 57 倍）。详见 choose_proxies 的说明。

    与 cyrest 的差异：CyREST 永远在 localhost，必须绕过代理；
    下载访问的是外网，是否走代理取决于用户环境，所以交给参数决定。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(dict(proxies or {})))

    last_exc = None
    for attempt in range(1, retries + 1):
        h = hashlib.sha256()
        size = 0
        try:
            log("下载 %s（第 %d/%d 次）" % (url, attempt, retries))
            with opener.open(url, timeout=120) as resp, open(tmp, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                if expected_size and total and abs(total - int(expected_size)) > (1 << 20):
                    log("警告：Content-Length=%d 与锁定 size_bytes=%s 不一致"
                        % (total, expected_size))
                last = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                    if total and size - last > (32 << 20):
                        last = size
                        log("  %.1f%% (%d/%d MB)" % (100.0 * size / total,
                                                     size >> 20, total >> 20))
            if size == 0:
                raise DownloadError("下载得到空文件（0 字节）")
            if total and size != total:
                raise DownloadError("下载不完整：收到 %d 字节，Content-Length=%d"
                                    % (size, total))
            digest = h.hexdigest()
            os.replace(tmp, dest)               # 原子落地
            if expected_sha256 is None:
                return digest, size, None       # TOFU：无官方清单，交由调用方处置
            return digest, size, (digest.lower() == expected_sha256.lower())
        except Exception as exc:                # noqa: BLE001
            last_exc = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            log("下载失败（第 %d/%d 次）：%s" % (attempt, retries, exc))
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise DownloadError("下载 %s 连续 %d 次失败：%s" % (url, retries, last_exc))


def _tar_extract_member(tf, member, dest):
    """逐条解压，并显式声明不启用 tarfile 自带的 filter。

    安全性已由 _extract 的 guard() 逐条校验（路径越界、链接逃逸一律拒绝），
    所以这里显式写 fully_trusted：避免 Python 3.12+ 的默认 filter 在将来版本
    切换为 'data' 之后二次干预 —— 那会拒绝符号链接、改写元数据，导致官方归档
    的解压结果随解释器版本漂移（确定性问题，医学场景不接受）。
    filter 关键字是 Python 3.12 才有的，旧版本按 TypeError 回退。
    """
    try:
        tf.extract(member, dest, filter="fully_trusted")
    except TypeError:                      # Python < 3.12
        tf.extract(member, dest)


def _is_within(base, target):
    """判断 target 解析后是否落在 base 之内（防 Zip Slip / Tar Slip）。"""
    try:
        base_r = Path(base).resolve()
        target_r = Path(target).resolve()
    except OSError:
        return False
    return target_r == base_r or base_r in target_r.parents


def _extract(archive, dest):
    """安全解压。

    归档若含 ../ 逃逸或指向目录外的符号链接，会写到 dest 之外 —— 这就是
    Zip Slip / Tar Slip。provision 下载的归档中存在 sha256 为 null 的产物
    （官方未提供清单），一旦下载链路被劫持，未防护的解压等于任意文件写入。
    因此这里逐条校验，路径越界一律拒绝。
    """
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest_r = str(Path(dest).resolve())

    def guard(name, link_target=None):
        if not _is_within(dest, Path(dest) / name):
            raise ValueError("归档条目越出目标目录（已拒绝）: %s" % name)
        if link_target:
            # 符号链接本身在 dest 内还不够，链接指向也必须在 dest 内，
            # 否则可通过「链接指向 /etc」在后续写入时逃逸。
            base = Path(dest) / Path(name).parent
            if not _is_within(dest, base / link_target):
                raise ValueError("符号链接指向目标目录之外（已拒绝）: %s -> %s"
                                 % (name, link_target))

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                mode = info.external_attr >> 16
                link_target = None
                if stat.S_ISLNK(mode):
                    with zf.open(info) as fh:
                        link_target = fh.read().decode("utf-8", "replace")
                guard(info.filename, link_target)
                zf.extract(info, dest)
    elif archive.name.endswith(".tar.gz") or archive.suffix == ".tgz":
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                link = member.linkname if (member.issym() or member.islnk()) else None
                guard(member.name, link)
                _tar_extract_member(tf, member, dest)
    else:
        raise ValueError("不支持的归档类型: %s" % archive.name)
    log("已解压到 %s" % dest_r)


def cmd_provision(args):
    lock = load_lock()
    plat = platform_key()
    comp = args.component
    variants = lock["artifacts"].get(comp, {}).get(plat) or []
    if not variants:
        fail(EXIT_ENV, "锁定文件中没有 %s 在 %s 平台的产物定义" % (comp, plat),
             hint=lock.get("notes", {}).get("%s_jre" % plat)
                  or "请参考 docs/02-coverage-and-determinism.md，或用系统包管理器自行提供。")

    if args.variant is not None:
        spec = next((v for v in variants if v["variant"] == args.variant), None)
        if spec is None:
            fail(EXIT_USAGE, "无效 --variant %s，可选: %s"
                 % (args.variant, ", ".join(v["variant"] for v in variants)))
    else:
        spec = variants[0]        # 列表中靠前者优先（安装包优先于无哈希归档）
    log("选用产物: %s (%s)" % (spec["file"], spec["variant"]))

    base = lock["jre_base"] if comp == "jre" else lock["release_base"]
    url = base + spec["file"]
    rd = _runtime_dir(args.runtime)
    dl_dir = rd / "_downloads"
    archive = dl_dir / spec["file"]
    target = rd / comp

    # 幂等：目标已存在且未要求 --force，直接返回。
    # 旧实现这里只打了一行「--force 可强制重装」的日志却继续解压覆盖，
    # 日志与行为不符 —— 调用方会误判为「已跳过」，实际重装了。
    if target.exists() and not args.force:
        emit({"ok": True, "component": comp, "platform": plat,
              "file": spec["file"], "variant": spec["variant"],
              "installed": True, "install_path": str(target),
              "already_installed": True,
              "note": "%s 已安装；如需重装请加 --force" % comp})
        return EXIT_OK

    expected = spec.get("sha256")
    observed = spec.get("tofu_observed")
    source = spec.get("sha256_source")

    proxy_info = None
    if not archive.exists() or args.force:
        pmode = "none" if getattr(args, "no_proxy", False) \
            else (getattr(args, "proxy", None) or "auto")
        proxies, proxy_info = choose_proxies(url, mode=pmode, timeout=10)
        log("下载代理: %s -> %s" % (proxy_info.get("decided"),
                                   proxy_info.get("proxies") or "直连"))
        digest, size, verified = _download(
            url, archive, expected,
            expected_size=int((spec.get("size_mb") or 0) * 1048576) or None,
            proxies=proxies)
    else:
        digest = _sha256_of(archive)
        size = archive.stat().st_size
        verified = None if expected is None else (digest.lower() == expected.lower())
        log("复用已下载归档 %s" % archive)

    result = {
        "ok": True, "component": comp, "platform": plat,
        "file": spec["file"], "variant": spec["variant"], "url": url,
        "sha256": digest, "sha256_expected": expected,
        "sha256_source": source,
        "size_bytes": size, "verified": verified,
        "download_proxy": proxy_info,
    }

    # ① 官方给了哈希且不匹配 —— 硬失败（完整性优先于可用性）。
    if expected and verified is False:
        archive.unlink(missing_ok=True)
        fail(EXIT_INTEGRITY,
             "%s 校验失败，已删除下载文件。期望 sha256=%s，实际=%s"
             % (comp, expected, digest), detail=result)

    # ①' 有期望哈希却拿不到校验结论 —— 说明校验被静默跳过了。
    # 这是内部一致性断言：宁可报错，也绝不放行一个「本该校验但没校验」的产物。
    if expected and verified is None:
        archive.unlink(missing_ok=True)
        fail(EXIT_INTEGRITY,
             "%s 完整性校验未执行（内部错误），已删除下载文件" % comp,
             detail=result)

    # ② 官方没给哈希 —— 默认 fail-closed。
    # 官方 sha256sums 只覆盖 4 个安装包，tar.gz / zip / 自建 JRE 全都没有哈希。
    # 旧实现在这种情形下 verified=None 却照样解压安装，等于把未校验的二进制
    # 直接放进运行目录。医学/生信场景不接受这种「静默信任」，改为默认阻断，
    # 必须显式放行；同时把实测 sha256 交回给调用方，便于核对后回填锁定文件。
    if expected is None:
        if observed and str(digest).lower() == str(observed).lower():
            result["integrity"] = "tofu-match"
            result["verified"] = True
            log("已与锁定文件中的 tofu_observed 基准一致")
        elif not args.allow_unverified:
            result.update(
                installed=False, integrity="unverified", blocked=True,
                hint=("官方未提供该归档的 sha256（来源：%s）。核对上面的 sha256 后，"
                      "写入 assets/versions.lock.json 的 sha256 字段，"
                      "或加 --allow-unverified 显式接受本次下载。" % source))
            emit(result)
            return EXIT_INTEGRITY
        else:
            result["integrity"] = "unverified-accepted"
            log("警告：按 --allow-unverified 接受未校验归档（来源 %s）" % source)

    # 安装包（.exe/.sh）走静默安装；归档走解压
    if spec["kind"] == "installer":
        if not args.execute_installer:
            result.update(installed=False,
                          note="已下载并校验安装包。安装需加 --execute-installer（静默安装到 runtime/%s）。" % comp)
            emit(result)
            return EXIT_OK
        if plat == "windows":
            cmd = [str(archive), "-q", "-dir", str(target), "-overwrite"]
        else:
            os.chmod(archive, 0o755)
            cmd = ["bash", str(archive), "-q", "-dir", str(target), "-overwrite"]
        log("静默安装: %s" % " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        result["installer_exit_code"] = proc.returncode
        if proc.returncode != 0:
            fail(EXIT_ENV, "安装包执行失败", detail=(proc.stderr or proc.stdout)[-2000:])
    else:
        try:
            _extract(archive, target)
        except (ValueError, tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            fail(EXIT_INTEGRITY, "%s 解压失败，已中止" % comp,
                 detail="%s: %s" % (type(exc).__name__, exc),
                 hint="归档可能损坏或含越界条目（Zip/Tar Slip 防护已生效）。")

    result["installed"] = True
    result["install_path"] = str(target)
    if comp == "jre":
        result["java"] = _bundled_jre_java()
    if comp == "cytoscape":
        launcher = find_launcher(rd)
        result["launcher"] = str(launcher) if launcher else None
    emit(result)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 子命令：start / wait / status / stop
# ---------------------------------------------------------------------------

def _pid_file(rd):
    return Path(rd) / "cytoscape.pid"


def _port_open(port, host="127.0.0.1", timeout=1.0):
    """端口是否有服务在监听。"""
    import socket as _socket
    s = _socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def _wait_port_closed(port, timeout=45.0, interval=1.0):
    """等端口关闭，返回等待秒数；超时仍开着则返回 None。

    这是判断「引擎是否真的停了」的**唯一可靠判据**。
    不能靠 HTTP 返回码：`command quit` 会让进程在响应写回之前就退出，
    客户端只会看到连接被重置 —— 看起来像失败，实际成功。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _port_open(port):
            return round(time.time() - t0, 1)
        time.sleep(interval)
    return None


def _port_owner_pid(port):
    """按端口找出监听进程的 pid（尽力而为，取不到返回 None）。

    为什么要有这个而不是直接用 pid 文件：启动器是 .bat / .sh 包装，
    pid 文件里记的可能是外层壳（甚至已被测试脚本改写），与真正的 java 进程不同。
    实测：pid 文件写 49612，tasklist 里真正的 java 是 46776，杀错了对象。
    """
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=25).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[1].endswith(":%d" % port)
                        and parts[3].upper() == "LISTENING"):
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=25).stdout
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    return int(parts[1])
    except Exception:                             # noqa: BLE001
        return None
    return None


def _kill_pid(pid):
    """结束进程及其子进程。返回 (ok, detail)。"""
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=30)
            return (r.returncode == 0), (r.stdout or r.stderr or "").strip()[:300]
        os.kill(int(pid), 15)
        return True, "SIGTERM"
    except Exception as exc:                      # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def _resolve_launcher(args, rd):
    """按 --launcher / --engine 决定用哪个启动脚本。返回 (Path|None, source)。"""
    if args.launcher:
        p = Path(args.launcher)
        return (p if p.exists() else None), "explicit"
    mode = getattr(args, "engine", "auto")
    if mode in ("auto", "bundled"):
        found = find_launcher(rd)
        if found:
            return found, "cyctl-bundled"
    if mode in ("auto", "system"):
        best = edis.best_cytoscape(runtime_dir=rd, prefer_bundled=False)
        if best:
            return Path(best["launcher"]), best["source"]
    return None, None


def _apply_headless(cmd, args, plat):
    """按平台决定是否套虚拟显示。返回 (cmd, info)。

    Cytoscape Desktop 是 GUI 程序：Linux 无 DISPLAY 时不套 Xvfb 必然起不来。
    官方**没有**无头开关（网上流传的 -N 不存在，见 versions.lock.json 的 notes），
    所以只能靠虚拟显示。
    """
    if plat == "windows":
        return cmd, {
            "headless": False,
            "reason": "Windows 上不需要虚拟显示；GUI 进程在交互式会话中运行。",
            "note": ("若在无桌面的 Windows 服务会话里运行，Cytoscape 仍需要交互式桌面。"
                     if args.headless else None),
        }
    if plat == "darwin":
        return cmd, {"headless": False, "reason": "macOS 始终有 WindowServer，无需虚拟显示。"}

    has_display = edis.display_available()
    want = bool(args.headless) or not has_display
    if not want:
        return cmd, {"headless": False, "reason": "检测到 DISPLAY，直接启动。"}

    xv = edis.xvfb_status()
    if not xv.get("available"):
        return cmd, {
            "headless": True, "virtual_display": False,
            "reason": "无可用 DISPLAY，且未找到 Xvfb/Xvfb-run。",
            "hint": xv.get("hint"),
            "risk": "Cytoscape 极可能启动失败（GUI 初始化会直接退出）。",
        }
    if xv.get("method") == "xvfb-run":
        new = [xv["xvfb_run"], "-a", "-s", "-screen 0 1600x1200x24"] + cmd
        return new, {"headless": True, "virtual_display": True, "method": "xvfb-run",
                     "note": "已用 xvfb-run -a 包装（自动挑选空闲显示号）。"}
    return cmd, {
        "headless": True, "virtual_display": False, "method": "Xvfb",
        "hint": "只找到 Xvfb 而没有 xvfb-run。请手动启动 Xvfb 并设置 DISPLAY 后重试。",
    }


def cmd_start(args):
    rd = _runtime_dir(args.runtime)
    rd.mkdir(parents=True, exist_ok=True)
    plat = platform_key()
    mode = getattr(args, "engine", "auto")

    # attach：不启动，只确认能连上已运行的实例
    if mode == "attach":
        base = cyrest.resolve_base_url(args.base_url, args.port)
        try:
            info, _, _ = cyrest.cyrest_request("GET", base, timeout=5)
            emit({"ok": True, "mode": "attach", "base_url": base,
                  "reachable": True, "engine": info,
                  "note": "已连上现有实例，未启动新进程。"})
            return EXIT_OK
        except cyrest.CyRestError as exc:
            emit({"ok": False, "mode": "attach", "base_url": base,
                  "reachable": False, **exc.to_dict(),
                  "hint": "attach 模式要求 Cytoscape 已经在运行。若需要启动，去掉 --engine attach。"})
            return EXIT_ENV

    # 端口占用检查：不检查的话会制造「影子实例」——
    # 旧实例还占着 1234，新实例被启动但端口抢不到，而 cyctl wait 立刻返回
    # ready（它连上的是旧实例），调用方完全看不出自己启动的引擎没生效。
    # 实测踩过：stop 未杀干净后 start + wait 得到 waited=0.0s 的假成功。
    port = args.port or cyrest.DEFAULT_PORT
    if _port_open(port):
        fail(EXIT_ENV, "端口 %d 已被占用，未启动新实例" % port,
             hint="可能已有一个 Cytoscape 在运行：\n"
                  "  * 想接管它 -> cyctl start --engine attach\n"
                  "  * 想换新实例 -> 先 cyctl stop\n"
                  "  * 想并行运行 -> cyctl start --port <其它端口>",
             port=port, base_url=cyrest.resolve_base_url(None, port))

    launcher, source = _resolve_launcher(args, rd)
    if launcher is None:
        hints = {
            "bundled": "先运行 cyctl provision --component cytoscape（约 350 MB）。",
            "system": "未在系统上发现已有安装。请先安装 Cytoscape Desktop，"
                      "或用 cyctl discover 查看探测范围；也可 --launcher 显式指定。",
            "auto": "既没有自带引擎，也没发现系统安装。二选一："
                    "cyctl provision --component cytoscape，或安装 Cytoscape Desktop。",
        }
        fail(EXIT_ENV, "未找到 Cytoscape 启动脚本（--engine %s）" % mode,
             hint=hints.get(mode, hints["auto"]),
             searched=[str(rd)] + ([str(Path.home())] if mode == "system" else []))

    # Java：优先自带（版本可控 ⇒ 结果可复现），否则用系统上满足 17+ 的
    java = edis.best_java(runtime_dir=rd)
    if java is None:
        legacy = detect_java()
        java = legacy if legacy.get("found") else None

    env = os.environ.copy()
    if java:
        jp = Path(java["path"])
        env["JAVA_HOME"] = str(jp.parent.parent)
        if args.display:
            env["DISPLAY"] = args.display
    if args.display and "DISPLAY" not in env:
        env["DISPLAY"] = args.display

    if not java or not java.get("ok_for_cytoscape"):
        log("警告：未探测到 Java 17+，Cytoscape 3.10+ 需要 Java 17。"
            "当前 major=%s" % (java or {}).get("major"))

    cmd = [str(launcher), "-R", str(args.port)]
    if args.command_file:
        cmd += ["-c", str(Path(args.command_file).resolve())]
    cmd += list(args.extra or [])

    if os.name != "nt" and str(launcher).endswith(".sh"):
        cmd = ["bash", str(launcher)] + cmd[1:]

    cmd, headless_info = _apply_headless(cmd, args, plat)

    logf = open(rd / "cytoscape.out.log", "ab")
    kwargs = dict(cwd=str(Path(launcher).parent), env=env, stdout=logf,
                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    log("启动: %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, **kwargs)
    _pid_file(rd).write_text(str(proc.pid), encoding="utf-8")

    payload = {
        "ok": True, "pid": proc.pid, "port": args.port,
        "engine_mode": mode, "launcher": str(launcher), "launcher_source": source,
        "java": java,
        "log": str(rd / "cytoscape.out.log"),
        "base_url": cyrest.resolve_base_url(None, args.port),
        "headless": headless_info,
        "next": "运行 cyctl wait 等待引擎就绪，然后 cyctl status 验证。",
    }
    if headless_info.get("risk"):
        payload["warning"] = headless_info["risk"]
    emit(payload)
    return EXIT_OK


def cmd_wait(args):
    base = cyrest.resolve_base_url(args.base_url, args.port)
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        try:
            result, _, _ = cyrest.cyrest_request("GET", base, timeout=5)
            emit({"ok": True, "ready": True, "base_url": base, "detail": result,
                  "waited_seconds": round(args.timeout - (deadline - time.time()), 1)})
            return EXIT_OK
        except Exception as exc:                 # noqa: BLE001
            last = str(exc)
            time.sleep(min(2.0, max(0.3, args.timeout / 60.0)))
    emit({"ok": False, "ready": False, "base_url": base, "error": "等待超时", "last_error": last})
    return EXIT_ENV


def cmd_status(args):
    base = cyrest.resolve_base_url(args.base_url, args.port)
    try:
        result, raw, status = cyrest.cyrest_request("GET", base, timeout=args.timeout)
    except cyrest.CyRestError as exc:
        emit({"ok": False, "base_url": base, **exc.to_dict()})
        return EXIT_REMOTE
    emit({"ok": True, "base_url": base, "http_status": status, "engine": result,
          "version": _engine_version_info(base, timeout=args.timeout)})
    return EXIT_OK


def cmd_stop(args):
    """停止由 cyctl 启动的 Cytoscape。

    成功判据是**端口关闭**，不是 HTTP 返回码 —— 这是本命令最关键的设计点。

    背景：最优雅的退出方式是 CyREST 的 `command quit`（命名空间 command 下确实
    有 quit）。但它会让进程在响应写回之前就退出，客户端只会看到「连接被重置」，
    看起来像失败。若据此判定失败并回落到杀进程，而 pid 文件又不可靠
    （启动器是 .bat 包装，实测 pid 文件写 49612 而真正的 java 是 46776），
    就会出现「stop 报成功、引擎却还在跑」这种最糟糕的结果 —— 静默失败。

    所以：发完退出请求就只管等端口关，关了就成功；没关再按端口找占用者。
    """
    rd = _runtime_dir(args.runtime)
    port = args.port or cyrest.DEFAULT_PORT
    base = cyrest.resolve_base_url(args.base_url, port)
    pid_path = _pid_file(rd)
    steps = []

    # 0) 本来就没跑 —— 幂等，不报错
    if not _port_open(port):
        had_pid = pid_path.exists()
        if had_pid:
            pid_path.unlink(missing_ok=True)
        emit({"ok": True, "port": port, "running": False, "stopped_by": None,
              "note": "端口 %d 上没有实例；%s"
                      % (port, "已清理残留的 pid 文件。" if had_pid else "无需操作。")})
        return EXIT_OK

    # 1) 优雅退出：CyREST command quit
    # Accept 必须是 application/json —— 这是实测踩出来的：
    #   POST /v1/commands/{ns}/{cmd}  +  Accept: text/plain        -> HTTP 406
    #   POST /v1/commands/{ns}/{cmd}  +  Accept: application/json  -> HTTP 200
    # 只有 **GET** 的 help 端点要 text/plain。这里原来写的是 text/plain，
    # 于是「优雅退出」每次都被 406 挡住，再被下面那句「连接中断不代表失败」
    # 解释掉 —— 表现为 stop 永远 graceful:false，实际靠杀进程收场。
    try:
        _p, _r, _st = cyrest.cyrest_request("POST", base + "/commands/command/quit",
                                            body={}, accept="application/json",
                                            timeout=15)
        steps.append({"step": "cyrest:command quit", "http_status": _st,
                      "result": "request completed"})
    except cyrest.CyRestError as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            # 4xx 是真的没送进去，不能赖在「进程退出导致连接中断」头上。
            steps.append({"step": "cyrest:command quit", "http_status": exc.status,
                          "result": "rejected: %s" % exc,
                          "note": "请求被拒绝，引擎没收到退出指令 —— 将回落杀进程。"})
        else:
            steps.append({"step": "cyrest:command quit",
                          "result": "request interrupted: %s" % exc,
                          "note": "进程关闭会中断连接，这不代表退出失败 —— 看端口。"})
    except Exception as exc:                      # noqa: BLE001
        steps.append({"step": "cyrest:command quit",
                      "result": "request interrupted: %s" % exc,
                      "note": "进程关闭会中断连接，这不代表退出失败 —— 看端口。"})

    waited = _wait_port_closed(port, timeout=45)
    if waited is not None:
        pid_path.unlink(missing_ok=True)
        emit({"ok": True, "port": port, "running": True, "graceful": True,
              "stopped_by": "cyrest:command quit",
              "port_closed_after_seconds": waited, "steps": steps})
        return EXIT_OK

    # 2) 端口还开着 —— 按端口定位真正的占用进程
    owner = _port_owner_pid(port)
    killed = None
    if owner:
        ok, detail = _kill_pid(owner)
        killed = owner if ok else None
        steps.append({"step": "kill-port-owner", "pid": owner, "ok": ok,
                      "detail": detail})
        waited = _wait_port_closed(port, timeout=30) if ok else None

    # 3) 最后才退回 pid 文件
    pid = None
    if waited is None and pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if pid and pid != owner:
            ok, detail = _kill_pid(pid)
            steps.append({"step": "kill-pid-file", "pid": pid, "ok": ok,
                          "detail": detail})
            waited = _wait_port_closed(port, timeout=30) if ok else None

    if waited is not None:
        pid_path.unlink(missing_ok=True)
        emit({"ok": True, "port": port, "running": True, "graceful": False,
              "stopped_by": "kill-port-owner" if killed else "kill-pid-file",
              "port_closed_after_seconds": waited, "steps": steps})
        return EXIT_OK

    emit({"ok": False, "port": port, "running": True,
          "error": "端口 %d 仍被占用，未能停止引擎" % port,
          "port_owner_pid": owner, "pid_file_pid": pid, "steps": steps,
          "hint": "可能有实例不是由 cyctl 启动的。Windows 可用 "
                  "「netstat -ano | findstr :%d」查看占用者；"
                  "Linux/macOS 用「lsof -nP -iTCP:%d -sTCP:LISTEN」。" % (port, port)})
    return EXIT_ENV


def cmd_do_cmd(args):
    base = cyrest.resolve_base_url(args.base_url, args.port)
    try:
        result, raw, status = cyrest.run_command(
            args.command_string, base, use_post=not args.get, timeout=args.timeout)
    except cyrest.CyRestError as exc:
        emit({"ok": False, "command": args.command_string, **exc.to_dict()})
        return _exit_for(exc)
    except ValueError as exc:
        # 空命令 / 无法解析的命令字符串属于**用法错误**，不是内部错误。
        # 这里曾经漏掉一层捕获，于是 `cyctl cmd ""` 会落到 main 的兜底分支、
        # 以退出码 1 结束 —— 而契约规定退出码只能是 0/2/3/4/5，按契约解析的
        # 调用方遇到 1 会当成未知灾难。实测 `cmd ""` 与 `cmd "   "` 都会命中。
        fail(EXIT_USAGE, str(exc), command=args.command_string)
    emit({"ok": True, "command": args.command_string, "transport": "GET" if args.get else "POST",
          "http_status": status, "result": result})
    return EXIT_OK


def cmd_do_rest(args):
    base = cyrest.resolve_base_url(args.base_url, args.port)
    params = {}
    for item in (args.param or []):
        if "=" not in item:
            fail(EXIT_USAGE, "--param 需要 k=v 形式，收到: %r" % item)
        k, v = item.split("=", 1)
        params[k] = v
    body = None
    if args.body:
        try:
            body = json.loads(args.body)
        except ValueError as exc:
            fail(EXIT_USAGE, "--body 不是合法 JSON: %s" % exc)
    if args.out:
        # 二进制/大文件走字节通道，见 cyrest.cyrest_download 的说明。
        # 这里只在用户**没有显式指定** --accept 时才放开为 */*：
        # `application/json` 是 argparse 的默认值，而二进制端点对 Accept 敏感
        # （.pdf / .svg 发 json 会 406，实测），不能让它挡住导出。
        accept = "*/*" if args.accept == "application/json" else args.accept
        try:
            info = cyrest.cyrest_download(
                args.operation, base, args.out, method=args.method,
                params=params or None, body=body, timeout=args.timeout,
                accept=accept)
        except cyrest.CyRestError as exc:
            emit({"ok": False, "operation": args.operation, **exc.to_dict()})
            return _exit_for(exc)
        emit({"ok": True, "operation": args.operation, "method": args.method.upper(),
              "http_status": info["status"], "accept": accept,
              "saved": info["path"], "size": info["size"],
              "sha256": info["sha256"], "content_type": info["content_type"]})
        return EXIT_OK

    try:
        result, raw, status = cyrest.call_operation(
            args.operation, base, method=args.method, params=params or None,
            body=body, timeout=args.timeout, accept=args.accept)
    except cyrest.CyRestError as exc:
        emit({"ok": False, "operation": args.operation, **exc.to_dict()})
        return _exit_for(exc)
    emit({"ok": True, "operation": args.operation, "method": args.method.upper(),
          "http_status": status, "result": result})
    return EXIT_OK


def _hash_inputs(paths):
    out = []
    for p in paths or []:
        pth = Path(p)
        if pth.is_file():
            out.append({"path": str(pth.resolve()), "sha256": _sha256_of(pth),
                        "size_bytes": pth.stat().st_size})
        elif pth.exists():
            out.append({"path": str(pth.resolve()), "kind": "dir", "note": "目录未逐文件哈希"})
        else:
            out.append({"path": str(pth.resolve()), "kind": "missing"})
    return out


def cmd_run(args):
    """按工作流文件执行，产出可复现的溯源清单。

    工作流格式（JSON）：
      {
        "name": "demo",
        "inputs": ["data/network.sif"],
        "steps": [
          {"command": "network import file file=\"...\""},
          {"rest": {"method": "GET", "operation": "networks"}}
        ]
      }
    """
    wf_path = Path(args.workflow)
    if not wf_path.exists():
        fail(EXIT_USAGE, "工作流文件不存在: %s" % wf_path)
    try:
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        fail(EXIT_USAGE, "工作流文件不是合法 JSON: %s" % exc)
    base = cyrest.resolve_base_url(args.base_url, args.port)

    # 工作流里的相对路径一律相对**工作流文件所在目录**解析，而不是进程 cwd。
    # 这必须做：执行期真正读文件的是 Cytoscape 引擎，而引擎的 cwd 是它自己的
    # 安装目录（cyctl start 以 cwd=launcher 目录启动）—— 相对路径会解析到
    # 完全不相干的位置，而且报错信息里看不出是路径问题。
    wf_dir = wf_path.resolve().parent

    def _abs(p):
        # 规范成绝对路径：不这样做，`../data/x.sif` 会原样带进 {input}，
        # 引擎据此给网络命名，同一文件因写法不同就变成两个名字不同的网络。
        q = Path(str(p))
        return (q if q.is_absolute() else (wf_dir / q)).resolve()

    _inputs = [_abs(p) for p in (wf.get("inputs") or [])]
    # 命令字符串里可用的占位符 —— 这样工作流不必写死机器相关的绝对路径。
    subs = {"{workflow_dir}": str(wf_dir), "{input_dir}": str(wf_dir)}
    for _i, _p in enumerate(_inputs):
        subs["{input%d}" % _i] = str(_p)
    if _inputs:
        subs["{input}"] = str(_inputs[0])

    engine = None
    try:
        engine, _, _ = cyrest.cyrest_request("GET", base, timeout=10)
    except cyrest.CyRestError as exc:
        fail(EXIT_ENV, "Cytoscape 未就绪，工作流无法执行", detail=exc.to_dict())

    vinfo = _engine_version_info(base, timeout=10)

    manifest = {
        "schema": "cyctl/run-manifest/v1",
        "run_id": time.strftime("%Y%m%dT%H%M%S"),
        "cyctl_version": CYCTL_VERSION,
        "workflow": {"path": str(wf_path.resolve()),
                     "sha256": _sha256_of(wf_path),
                     "name": wf.get("name")},
        "engine": {"base_url": base, "info": engine, "version": vinfo},
        "host": {"platform": platform_key(), "python": sys.version.split()[0]},
        "inputs": _hash_inputs(_inputs),
        "steps": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": True,
    }

    for idx, step in enumerate(wf.get("steps", [])):
        entry = {"index": idx}
        try:
            if "command" in step:
                cmd_str = step["command"]
                for _k, _v in subs.items():
                    cmd_str = cmd_str.replace(_k, _v)
                # 工作流也要过危险命令闸。不能因为"是配置文件"就假定它安全 ——
                # 工作流文件同样可能是外部给的。
                _danger = cyrest.dangerous_command(cmd_str)
                if _danger:
                    raise ValueError("工作流步骤含危险命令 %r，已拒绝执行" % _danger)
                url, body = cyrest.command_to_post(cmd_str, base)
                result, raw, status = cyrest.cyrest_request("POST", url, body=body,
                                                            timeout=args.timeout)
                # 溯源清单里记**实际执行**的命令（占位符已展开），否则事后无法复原
                entry.update(kind="command", request={"command": cmd_str,
                                                      "url": url, "body": body},
                             http_status=status, result=result)
            elif "rest" in step:
                spec = step["rest"]
                if spec.get("out"):
                    # 二进制/大文件导出：走字节通道落盘，清单里记 sha256 与大小，
                    # 而不是把内容塞进 JSON（那会把图片按 UTF-8 解码毁掉）。
                    _dest = _abs(spec["out"])
                    info = cyrest.cyrest_download(
                        spec["operation"], base, str(_dest),
                        method=spec.get("method", "GET"),
                        params=spec.get("params"), body=spec.get("body"),
                        timeout=args.timeout,
                        accept=spec.get("accept", "*/*"))
                    entry.update(kind="rest", request=spec,
                                 http_status=info["status"], saved=info["path"],
                                 result={"size": info["size"],
                                         "sha256": info["sha256"],
                                         "content_type": info["content_type"]})
                    entry["result_sha256"] = hashlib.sha256(
                        json.dumps(entry["result"], sort_keys=True,
                                   ensure_ascii=False).encode("utf-8")).hexdigest()
                    entry["ok"] = True
                    manifest["steps"].append(entry)
                    continue
                result, raw, status = cyrest.call_operation(
                    spec["operation"], base, method=spec.get("method", "GET"),
                    params=spec.get("params"), body=spec.get("body"),
                    timeout=args.timeout,
                    # 工作流里 help 类端点要显式写 "accept": "text/plain"，
                    # 否则默认 json 会拿到 406。
                    accept=spec.get("accept", "application/json"))
                entry.update(kind="rest", request=spec, http_status=status, result=result)
            else:
                raise ValueError("步骤必须含 'command' 或 'rest'")
            # 结果指纹：同一输入在同版本引擎下应产生同一指纹
            entry["result_sha256"] = hashlib.sha256(
                json.dumps(entry.get("result"), sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            entry["ok"] = True
        except Exception as exc:                  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = str(exc)
            manifest["ok"] = False
            manifest["steps"].append(entry)
            break
        manifest["steps"].append(entry)

    manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        log("溯源清单已写入 %s" % outp)

    emit(manifest)
    return EXIT_OK if manifest["ok"] else EXIT_REMOTE


def cmd_prune(args):
    """清理下载缓存 runtime/_downloads。

    每次 provision 都会把归档留在缓存里（供复用与复核），长期运行会累积
    数百 MB。这里提供显式清理入口，默认 --dry-run 先看体积再动手。
    """
    rd = _runtime_dir(args.runtime)
    dl = rd / "_downloads"
    removed, freed = [], 0
    if dl.exists():
        for f in sorted(dl.iterdir()):
            if not f.is_file():
                continue
            sz = f.stat().st_size
            if not args.dry_run:
                try:
                    f.unlink()
                except OSError as exc:
                    log("删除失败 %s: %s" % (f, exc))
                    continue
            removed.append({"file": f.name, "size_bytes": sz})
            freed += sz
    emit({"ok": True, "dry_run": bool(args.dry_run), "downloads_dir": str(dl),
          "removed": removed, "removed_count": len(removed),
          "freed_bytes": freed, "freed_mb": round(freed / 1048576, 1),
          "note": "默认 dry-run；加 --yes 才真正删除。" if args.dry_run else "已删除。"})
    return EXIT_OK


def cmd_mcp(args):
    """输出全宿主接入矩阵，并可选地把配置**安全落盘**。

    这里只产出「连接信息」，不触碰计算。所有宿主用的是 Cytoscape 官方 MCP 服务
    （挂在 CyREST 端口上的 /mcp），不存在第二个实现，因此结果与原生一致。
    """
    port = args.port or cyrest.DEFAULT_PORT
    url = args.endpoint or ("http://localhost:%d/mcp" % port)
    python_exe = sys.executable
    cyctl_path = str(Path(__file__).resolve())
    project_dir = str(Path(args.project).resolve()) if args.project else os.getcwd()

    hosts = None
    if args.host:
        hosts = [h.strip() for h in args.host.split(",") if h.strip()]
        unknown = [h for h in hosts if h not in hmat.host_ids()]
        if unknown:
            fail(EXIT_USAGE, "未知宿主: %s" % ", ".join(unknown),
                 available=hmat.host_ids())

    matrix = hmat.build_matrix(url=url, port=port, python_exe=python_exe,
                               cyctl_path=cyctl_path, project_dir=project_dir,
                               hosts=hosts)

    payload = {
        "ok": True,
        "server_name": hmat.SERVER_NAME,
        "endpoint": {"mcp": url, "manifest": url + "/manifest"},
        "requires": "Cytoscape Desktop 运行中，且已安装官方 App「Cytoscape MCP Server」",
        "registry": hmat.MCP_REGISTRY_ID,
        "project_dir": project_dir,
        "python": python_exe,
        "cyctl": cyctl_path,
        "note": ("仅支持 stdio 的宿主（如 Claude Desktop）用 claude-desktop 项的配置即可，"
                 "它指向 cyctl bridge，无需下载官方 .mcpb 扩展。"),
        "clients": {h["id"]: h["cli"] for h in matrix if h.get("cli")},
        "hosts": matrix,
    }

    if args.host and len(hosts) == 1:
        payload["host"] = matrix[0]

    if args.write_config:
        results = []
        for h in matrix:
            if h.get("error"):
                continue
            try:
                entry = hmat.render_server_entry(
                    h["entry_style"], url=url, port=port,
                    python_exe=python_exe, cyctl_path=cyctl_path)
            except Exception as exc:              # noqa: BLE001
                results.append({"host": h["id"], "action": "skipped",
                                "reason": "无法渲染条目: %s" % exc})
                continue
            for c in h.get("configs", []):
                if c.get("error"):
                    continue
                if args.scope and c.get("scope") != args.scope:
                    continue
                if c.get("needs_project_dir"):
                    results.append({"host": h["id"], "scope": c.get("scope"),
                                    "path": c.get("path"), "action": "skipped",
                                    "reason": "需要 --project 指定项目目录（路径含占位符）"})
                    continue
                if args.only_dedicated and not c.get("dedicated"):
                    results.append({"host": h["id"], "scope": c.get("scope"),
                                    "path": c.get("path"), "action": "skipped",
                                    "reason": "混合配置文件，按 --only-dedicated 跳过"})
                    continue
                r = hmat.write_config(c["path"], key=c["key"], name=hmat.SERVER_NAME,
                                      entry=entry, fmt=c.get("format", "json"),
                                      allow_mixed=args.allow_mixed)
                r["host"] = h["id"]
                r["scope"] = c.get("scope")
                results.append(r)
        payload["written"] = results
        payload["written_summary"] = {
            "created": sum(1 for r in results if r.get("action") == "created"),
            "merged": sum(1 for r in results if r.get("action") == "merged"),
            "unchanged": sum(1 for r in results if r.get("action") == "unchanged"),
            "refused": sum(1 for r in results if r.get("action") == "refused"),
            "skipped": sum(1 for r in results if r.get("action") == "skipped"),
        }

    emit(payload)
    return EXIT_OK


_DANGER_VERBS = {"quit", "exit", "shutdown", "halt"}


def _is_danger_command(namespace, command):
    """识别「查询即关引擎」的命令，返回描述串（如 "command quit"）或 None。

    为什么要单独拦：CyREST 的 GET /commands/{ns}/{cmd} 对**不需要参数**的命令
    就是执行它。实测 `cyctl commands command quit` 直接把 Cytoscape 关掉了；更麻烦的是
    GUI 模式下 JVM 会卡在退出确认对话框上不退出，留下一个 1.3 GB 的僵尸进程，
    而端口已经关了 —— 表现成「明明没进程监听却还有进程占内存」。
    这类命令在任何命名空间下都没有「只想看帮助」的正当场景，永久拒绝。
    """
    if not command or not command.strip():
        return None
    first = command.strip().lower().split()[0]
    if first in _DANGER_VERBS:
        return ("%s %s" % (namespace or "", command)).strip()
    return None


def _parse_help_list(text):
    """解析 CyREST help 文本 -> (标题, 条目列表)。

    原文形如：
        Available commands for 'network':   add   add edge   add node ...
    条目之间用**多个空格**分隔 —— 命令名本身含空格（"add edge"），
    所以不能按单空格切，否则会把一条命令拆成两条。
    """
    head, _, tail = text.partition(":")
    items = [x.strip() for x in re.split(r"\s{2,}", tail.strip()) if x.strip()]
    if len(items) <= 1:                      # 某些版本可能用单空格分隔
        items = tail.split()
    return head.strip(), items


def _mcp_root_from_base(base_url):
    """从 CyREST base url 推出 MCP 端点的**根**（端口根，无 API 版本前缀）。

    base_url 形如 http://localhost:1234/v1，MCP 挂在 http://localhost:1234/mcp。
    统一在这里推导，避免各调用点各写一份 —— cmd_bridge 与 doctor 都曾
    直接用 base_url 拼 "/mcp"，得到 /v1/mcp（错的）。
    """
    u = urllib.parse.urlsplit(base_url)
    path = re.sub(r"/v\d+$", "", u.path.rstrip("/"))
    return "%s://%s%s" % (u.scheme, u.netloc, path)


def _mcp_endpoint(args):
    """MCP 端点 URL —— 在 CyREST **端口根**上，不是 API 版本前缀之下。

    官方 MCP 服务挂在 /mcp。cyctl mcp 一直是这么算的；但 cyctl bridge 早期
    直接拿 base_url 拼 "/mcp"，于是得到 http://host:1234/v1/mcp —— 这是错的。
    这个 bug 特别隐蔽：**没装 MCP App 时两种拼法都返回 404**，看不出差别；
    只有装了 App 之后才会表现为「mcp 子命令给的配置能用、bridge 却连不上」。
    两个子命令必须共用同一套推导。
    """
    if getattr(args, "endpoint", None):
        return args.endpoint
    if getattr(args, "base_url", None):
        return _mcp_root_from_base(args.base_url) + "/mcp"
    return "http://localhost:%d/mcp" % (getattr(args, "port", None) or cyrest.DEFAULT_PORT)


def _engine_version_info(base, lock=None, timeout=15):
    """读取引擎版本并与锁定版本比对。

    为什么必须比对而不只是记录：cyctl 允许 --engine system / attach，这两种来源的
    版本不受本仓库控制。版本不同就可能算法输出不同，而这件事**必须显式说出来** ——
    否则用户会以为「同一份数据 + 同一个工具 = 必然相同的结果」。
    """
    pinned = (lock or load_lock()).get("pinned", {}).get("cytoscape")
    try:
        ver, raw = cyrest.engine_version(base, timeout=timeout)
    except cyrest.CyRestError as exc:
        return {"available": False, "pinned": pinned, "error": str(exc),
                "kind": exc.kind}
    if not ver:
        return {"available": False, "pinned": pinned, "raw": raw,
                "note": "引擎未在 /version 返回 cytoscapeVersion。"}
    match = (str(ver) == str(pinned))
    return {
        "available": True, "engine": ver, "pinned": pinned, "match": match,
        "note": None if match else (
            "引擎版本 %s 与锁定版本 %s 不一致。算法输出可能不同；需要可复现结果时"
            "请改用已锁定的引擎（cyctl provision / --engine bundled），"
            "或先把该版本登记进溯源清单再比对。" % (ver, pinned)),
    }


def cmd_commands(args):
    """查询引擎的**真实**命令清单（命名空间 / 命令 / 参数），避免「猜名字」。

    CyREST 的 help 类端点返回的是**人类可读文本**，并且只接受 Accept: text/plain
    —— 传 application/json 会得到 HTTP 406 Not Acceptable。也就是说
    「查命令清单」与「执行命令」走的是两套内容协商，统一用 json 的客户端会在这里碰壁。

    这条能力很重要：实测 `network export` 的真实参数名是 outputFile 而非 file，
    靠猜必然写错，而写错的参数会被引擎静默忽略或报 500。
    """
    base = cyrest.resolve_base_url(args.base_url, args.port)

    # ---- 安全门禁 --------------------------------------------------------
    # CyREST 没有「只读地查看某条命令的参数」的独立端点：参数清单只能从
    # /commands/{ns}/{cmd} 拿，而对**无参命令**该请求就是执行。所以默认不放行，
    # 改为走只读的 /commands/{ns}（列出该命名空间的命令名），确认命令是否存在。
    if args.command:
        danger = _is_danger_command(args.namespace, args.command)
        if danger:
            fail(EXIT_USAGE, "拒绝查询 %r：该命令会终止/关闭引擎" % danger,
                 namespace=args.namespace, command=args.command, blocked=danger,
                 hint="需要关引擎请用 cyctl stop —— 它以「端口已关闭」为成功判据，"
                      "并能清理残留进程。")
        if not getattr(args, "allow_execute", False):
            ns_path = "/commands"
            if args.namespace:
                ns_path += "/" + urllib.parse.quote(args.namespace, safe="")
            ns_url = base + ns_path
            try:
                _p, _r, _s = cyrest.cyrest_request(
                    "GET", ns_url, accept="text/plain", timeout=args.timeout)
            except cyrest.CyRestError as exc:
                emit({"ok": False, "namespace": args.namespace,
                      "command": args.command, **exc.to_dict()})
                return EXIT_REMOTE
            ns_text = _r.strip() if isinstance(_r, str) else ""
            head, items = _parse_help_list(ns_text)
            exists = args.command in items
            emit({
                "ok": True, "url": ns_url, "http_status": _s,
                "namespace": args.namespace, "command": args.command,
                "executed": False, "source": "namespace-manifest",
                "exists": exists, "title": head,
                "count": len(items), "items": items,
                "note": ("命令 %r 存在于命名空间 %r（共 %d 条）。"
                         % (args.command, args.namespace, len(items))) if exists
                        else ("命令 %r 不在命名空间 %r 的清单里（共 %d 条）。"
                              % (args.command, args.namespace, len(items))),
                "hint": "本路径只读，因此拿不到参数清单 —— 参数只能从 "
                        "/commands/{ns}/{cmd} 获得，而对无参命令那就是执行它。"
                        "确需查看时加 --allow-execute。",
            })
            return EXIT_OK

    path = "/commands"
    if args.namespace:
        path += "/" + urllib.parse.quote(args.namespace, safe="")
    if args.command:
        path += "/" + urllib.parse.quote(args.command, safe="")
    url = base + path
    try:
        parsed, raw, status = cyrest.cyrest_request(
            "GET", url, accept="text/plain", timeout=args.timeout)
    except cyrest.CyRestError as exc:
        emit({"ok": False, "namespace": args.namespace, "command": args.command,
              **exc.to_dict()})
        return EXIT_REMOTE
    text = raw.strip() if isinstance(raw, str) else json.dumps(parsed, ensure_ascii=False)
    head, items = _parse_help_list(text)

    # executed 必须如实反映**到底执行了没有**。这个端点有两种回应：
    #   - 不需要参数的命令 -> 真的执行，返回结果；
    #   - 需要参数的命令  -> 返回参数清单（help），什么都没发生。
    # 原来无条件写 True，于是 `commands network export --allow-execute` 只拿到一份
    # 参数清单却报「已执行」—— 调用方会以为导出文件已经生成，这是会误导人的误报。
    # 判据：help 回应的标题以 "Available arguments for" 开头。
    _is_help = head.lower().startswith("available arguments")
    payload = {"ok": True, "url": url, "http_status": status,
               "namespace": args.namespace, "command": args.command,
               "executed": not _is_help,
               "mode": "help" if _is_help else "executed",
               "source": "live-command-endpoint",
               "title": head, "count": len(items), "items": items, "text": text}
    payload["caution"] = (
        "本结果来自 /commands/{namespace}/{command}（已显式 --allow-execute）："
        "CyREST 对**不需要参数**的命令就是执行它，对需要参数的命令返回参数清单"
        "（此时 executed=false、mode=help，代表什么都没执行）。"
        "quit/exit 类命令已被永久拦截，不受 --allow-execute 影响。")
    emit(payload)
    return EXIT_OK


def cmd_bridge(args):
    """stdio ↔ Streamable HTTP 桥。

    注意：**本子命令的 stdout 是 MCP 协议本身（逐行 JSON-RPC），
    不是 cyctl 的 JSON 信封** —— 这是唯一例外，因为宿主直接读它的 stdout 当协议用。
    需要机器可读的探测结果时用 --probe。
    """
    base = cyrest.resolve_base_url(args.base_url, args.port)
    endpoint = _mcp_endpoint(args)

    if args.probe:
        root = endpoint[: -len("/mcp")] if endpoint.endswith("/mcp") else endpoint
        info = mcpbridge.probe_manifest(root, timeout=args.timeout,
                                        insecure=args.insecure)
        emit({"ok": bool(info.get("ok")), "endpoint": endpoint,
              "base_url": base, "mcp_root": root, "manifest_probe": info})
        return EXIT_OK if info.get("ok") else EXIT_ENV

    return mcpbridge.run_bridge(endpoint, timeout=args.timeout, insecure=args.insecure)


def cmd_discover(args):
    """跨平台发现已有的 Cytoscape 与 Java 17。

    用于回答「我能不能不下载 580 MB」。--engine system 依赖本命令的探测结果。
    """
    rd = _runtime_dir(args.runtime)
    java = edis.find_java_installations(runtime_dir=rd, include_path=not args.no_path)
    cyto = edis.find_cytoscape_installations(runtime_dir=rd,
                                             extra_roots=args.root or None)
    emit({
        "ok": True,
        "platform": platform_key(),
        "arch": _platform.machine(),
        "runtime_dir": str(rd),
        "min_java_major": edis.MIN_JAVA_MAJOR,
        "java": java,
        "java_ok_count": sum(1 for j in java if j["ok_for_cytoscape"]),
        "cytoscape": cyto,
        "best_java": edis.best_java(runtime_dir=rd),
        "best_cytoscape": edis.best_cytoscape(runtime_dir=rd, prefer_bundled=False),
        "display": {"available": edis.display_available()},
        "xvfb": edis.xvfb_status(),
        "hint": ("发现可用 Cytoscape 时，可用 cyctl start --engine system 直接启动；"
                 "否则 cyctl provision --component cytoscape。"),
    })
    return EXIT_OK


def cmd_doctor(args):
    """全面体检：把「还缺什么、怎么补」一次说清，而不是让用户逐个试错。"""
    import json as _json

    rd = _runtime_dir(args.runtime)
    checks = []

    def add(cid, title, ok, level="error", **extra):
        item = {"id": cid, "title": title, "ok": bool(ok), "level": level}
        item.update({k: v for k, v in extra.items() if v is not None})
        checks.append(item)

    # 1 Python
    py_ok = sys.version_info >= (3, 8)
    add("python", "Python >= 3.8", py_ok,
        detail="%s" % sys.version.split()[0],
        fix=None if py_ok else "安装 Python 3.8+")

    # 2 版本锁定文件
    lock_ok, lock_detail = False, None
    try:
        lock = load_lock()
        lock_ok = True
        lock_detail = "pinned cytoscape=%s jre=%s" % (
            lock["pinned"]["cytoscape"], lock["pinned"]["jre"])
    except SystemExit:
        lock_detail = "缺失或非法: %s" % LOCK_PATH
    except Exception as exc:                      # noqa: BLE001
        lock_detail = str(exc)
    add("version_lock", "版本锁定文件可用", lock_ok, detail=lock_detail)

    # 3 Java 17
    java = edis.best_java(runtime_dir=rd)
    add("java", "存在 Java %d+" % edis.MIN_JAVA_MAJOR, bool(java), level="error",
        detail=("%s (major %s, 来源 %s)" % (java["path"], java["major"], java["source"])
                if java else "未找到"),
        fix=None if java else "cyctl provision --component jre（Windows/macOS 有官方包）"
                              "，或安装 OpenJDK 17 并设 JAVA_HOME",
        all_installations=[{"path": j["path"], "major": j["major"], "source": j["source"]}
                           for j in edis.find_java_installations(runtime_dir=rd)])

    # 4 Cytoscape 引擎
    bundled = find_launcher(rd)
    sys_installs = edis.find_cytoscape_installations(runtime_dir=rd)
    add("engine", "存在可用的 Cytoscape 引擎", bool(bundled or sys_installs),
        detail=("自带: %s" % bundled) if bundled else
               ("系统安装 %d 个" % len(sys_installs) if sys_installs else "未发现"),
        fix=None if (bundled or sys_installs) else
            "cyctl provision --component cytoscape，或安装 Cytoscape Desktop 后用 --engine system",
        bundled=str(bundled) if bundled else None,
        system=[c["launcher"] for c in sys_installs])

    # 5 CyREST 可达性
    base = cyrest.resolve_base_url(args.base_url, args.port)
    reachable, detail = False, None
    try:
        info, _, _ = cyrest.cyrest_request("GET", base, timeout=5)
        reachable, detail = True, info
    except cyrest.CyRestError as exc:
        detail = exc.to_dict()
    add("cyrest", "CyREST 可达（引擎在运行）", reachable, level="warn",
        base_url=base, detail=detail,
        fix=None if reachable else "cyctl start && cyctl wait")

    # 6 MCP App 是否装了（决定性检查：manifest 端点）
    mcp_probe = mcpbridge.probe_manifest(_mcp_root_from_base(base), timeout=5)
    add("mcp_app", "官方 MCP App 已安装并暴露 /mcp", bool(mcp_probe.get("ok")),
        level="warn",
        detail=(mcp_probe.get("error") or "OK，manifest 可读"),
        fix=None if mcp_probe.get("ok") else
            "在 Cytoscape 里安装/启用官方 App「Cytoscape MCP Server」；"
            "未安装时仍可用 cyctl cmd / rest / run 直接驱动引擎。")

    # 6b 引擎版本与锁定版本是否一致（可复现性的前提）
    if reachable:
        vinfo = _engine_version_info(base, timeout=5)
        add("engine_version",
            "引擎版本与锁定版本一致（锁定 %s）" % (vinfo.get("pinned") or "?"),
            bool(vinfo.get("available") and vinfo.get("match")), level="warn",
            detail=(vinfo.get("engine") or vinfo.get("error") or vinfo.get("note")),
            engine_version=vinfo.get("engine"), pinned=vinfo.get("pinned"),
            note=vinfo.get("note"),
            fix=None if vinfo.get("match") else
                "版本不一致会让算法输出漂移。改用 cyctl provision 得到的引擎"
                "（--engine bundled），或在溯源清单里登记该版本后再比对结果。")
    else:
        add("engine_version", "引擎版本与锁定版本一致", False, level="warn",
            detail="引擎不可达，无法读取版本",
            fix="cyctl start && cyctl wait")

    # 7 代理环境变量
    proxy_vars = {k: v for k, v in os.environ.items()
                  if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    add("proxy", "环境代理不会劫持本机请求", True, level="info",
        detail=("检测到代理变量 %s；cyctl 对 CyREST 请求已强制绕过代理。"
                % ", ".join(proxy_vars) if proxy_vars else "未设置代理变量"),
        variables=proxy_vars or None)

    # 8 运行时目录可写
    writable = False
    try:
        rd.mkdir(parents=True, exist_ok=True)
        probe = rd / ".cyctl-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        detail = str(exc)
    add("runtime_writable", "运行时目录可写", writable, detail=str(rd),
        fix=None if writable else "检查目录权限，或用 --runtime 指定其它位置")

    # 9 磁盘空间
    free = edis.disk_free_bytes(rd)
    need = 1200 * 1024 * 1024        # 自带引擎约需 580MB，留一倍余量
    add("disk", "磁盘余量足够自带引擎（≥1.2 GB）", (free or 0) >= need, level="warn",
        detail=("%.1f GB 可用" % (free / 2**30)) if free else "无法读取磁盘信息",
        free_gb=(round(free / 2**30, 1) if free else None),
        fix=None if (free or 0) >= need else "清理磁盘，或改用 --engine system 复用已有安装")

    # 10 虚拟显示（Linux）
    if platform_key() == "linux":
        disp = edis.display_available()
        xv = edis.xvfb_status()
        add("display", "有图形显示或可用的虚拟显示", disp or xv.get("available"),
            level="warn",
            detail=("DISPLAY=%s" % os.environ.get("DISPLAY")) if disp
                   else ("未检测到 DISPLAY；xvfb 可用=%s" % xv.get("available")),
            fix=None if (disp or xv.get("available")) else xv.get("hint"))

    # 11 端口占用
    in_use = edis.port_in_use(port=args.port or cyrest.DEFAULT_PORT)
    add("port", "CyREST 端口状态已知", True, level="info",
        port=args.port or cyrest.DEFAULT_PORT,
        detail=("端口已被占用" + ("（若正是 Cytoscape，属正常）" if in_use else ""))
               if in_use else "端口空闲")

    blocking = [c for c in checks if not c["ok"] and c["level"] == "error"]
    warnings = [c for c in checks if not c["ok"] and c["level"] == "warn"]

    emit({
        "ok": not blocking,
        "cyctl_version": CYCTL_VERSION,
        "platform": platform_key(),
        "arch": _platform.machine(),
        "summary": {
            "total": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "blocking": len(blocking),
            "warnings": len(warnings),
        },
        "checks": checks,
        "blocking_ids": [c["id"] for c in blocking],
        "warning_ids": [c["id"] for c in warnings],
        "next": ("全部关键项通过；可 cyctl start 启动引擎。"
                 if not blocking else
                 "先解决 blocking_ids 中的项：%s" % ", ".join(c["id"] for c in blocking)),
    })
    return EXIT_OK if not blocking else EXIT_ENV


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser():
    p = ArgParser(
        prog="cyctl",
        description="确定性驱动原版 Cytoscape 引擎（CyREST / Commands API）。")
    p.add_argument("--version", action=VersionAction, help="输出版本信息（JSON）")
    # 全局参数（放在子命令前，也可放子命令后）
    # 注意：--timeout 不放进 common，因为不同子命令的合理默认值不同
    # （wait 需要长超时，status 需要短超时）。放进父解析器会与子解析器
    # 重复定义同一 option string，argparse 会直接抛 ArgumentError。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=None, help="CyREST 端口，默认 1234")
    common.add_argument("--base-url", default=None, help="完整 base url，覆盖 --port")
    common.add_argument("--runtime", default=None, help="运行时目录，默认 <skill>/runtime")

    def with_timeout(parser, default=120.0):
        parser.add_argument("--timeout", type=float, default=default,
                            help="单次请求/等待超时秒数（默认 %g）" % default)
        return parser

    sub = p.add_subparsers(dest="cmd", required=True)

    with_timeout(sub.add_parser("env", parents=[common], help="探测环境")).set_defaults(func=cmd_env)
    with_timeout(sub.add_parser("describe", parents=[common], help="自描述能力清单")).set_defaults(func=cmd_describe)

    sp = with_timeout(sub.add_parser("provision", parents=[common], help="下载并校验引擎"))
    sp.add_argument("--component", choices=["cytoscape", "jre"], required=True)
    sp.add_argument("--variant", default=None, help="选择产物变体，默认取锁定文件中的首选")
    sp.add_argument("--force", action="store_true", help="目标已存在时强制重装")
    sp.add_argument("--execute-installer", action="store_true",
                    help="对 .exe/.sh 安装包执行静默安装（默认只下载并校验）")
    sp.add_argument("--allow-unverified", action="store_true",
                    help="接受官方未提供 sha256 的归档（默认阻断，退出码 5）")
    sp.add_argument("--proxy", choices=["auto", "env", "system", "none"],
                    default=None,
                    help="下载代理策略：auto=直连优先、不可达才回落系统代理（默认）；"
                         "env=只读环境变量（同 curl）；system=读系统设置"
                         "（Windows 注册表 / macOS 网络偏好）；none=直连。"
                         "urllib 默认走 system，会在用户从未配置代理时也套上代理，"
                         "实测可慢数十倍，故本工具默认改为 auto。")
    sp.add_argument("--no-proxy", action="store_true",
                    help="等价于 --proxy none（保留以兼容旧脚本）")
    sp.set_defaults(func=cmd_provision)

    sp = with_timeout(sub.add_parser("start", parents=[common], help="启动 Cytoscape"))
    sp.add_argument("--engine", choices=["auto", "bundled", "system", "attach"],
                    default="auto",
                    help="引擎来源：auto=优先自带并回落系统安装（默认）；"
                         "bundled=只用自带；system=只用系统安装；attach=不启动，仅连接已运行实例")
    sp.add_argument("--launcher", default=None, help="显式指定启动脚本路径（优先级最高）")
    sp.add_argument("--headless", action="store_true",
                    help="强制用虚拟显示启动（Linux 走 xvfb-run）；无 DISPLAY 时自动启用")
    sp.add_argument("--display", default=None, help="显式指定 DISPLAY（Linux）")
    sp.add_argument("--command-file", default=None, help="传给引擎的 -c 命令文件")
    sp.add_argument("--extra", nargs=argparse.REMAINDER, help="附加给启动脚本的参数")
    sp.set_defaults(func=cmd_start)

    with_timeout(sub.add_parser("wait", parents=[common], help="等待引擎就绪"),
                 default=300.0).set_defaults(func=cmd_wait)

    with_timeout(sub.add_parser("status", parents=[common], help="读取引擎信息"),
                 default=10.0).set_defaults(func=cmd_status)

    with_timeout(sub.add_parser("stop", parents=[common], help="停止引擎"),
                 default=15.0).set_defaults(func=cmd_stop)

    sp = with_timeout(sub.add_parser("cmd", parents=[common], help="执行 Cytoscape 命令"))
    sp.add_argument("command_string")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--get", action="store_true", help="用 GET 传输（默认 POST）")
    g.add_argument("--post", action="store_true", help="用 POST 传输（默认）")
    sp.set_defaults(func=cmd_do_cmd)

    sp = with_timeout(sub.add_parser("rest", parents=[common], help="调用 CyREST 函数"))
    sp.add_argument("method", help="GET/POST/PUT/DELETE")
    sp.add_argument("operation", help="如 networks / styles/Directed")
    sp.add_argument("--param", action="append", help="query 参数 k=v，可重复")
    sp.add_argument("--body", default=None, help="JSON 请求体")
    sp.add_argument("--accept", default="application/json",
                    help="Accept 头。help 类端点（commands / commands/{ns}）"
                         "只接受 text/plain，发默认的 json 会得到 HTTP 406")
    sp.add_argument("--out", default=None,
                    help="把响应体**按字节**写入该文件，不放进 stdout。"
                         "导出二进制产物（views/{id}.png|pdf、networks/{id}.cx）"
                         "必须用它：默认通道会把响应体按 UTF-8 解码，"
                         "二进制会被替换字符不可逆地破坏，而响应仍是 ok:true。"
                         "用 --out 时 Accept 默认改为 */*（PDF/SVG 端点发 json 会 406）")
    sp.set_defaults(func=cmd_do_rest)

    sp = with_timeout(sub.add_parser("run", parents=[common], help="执行工作流并产出溯源清单"))
    sp.add_argument("workflow")
    sp.add_argument("--out", default=None, help="溯源清单输出路径")
    sp.set_defaults(func=cmd_run)

    sp = with_timeout(sub.add_parser("commands", parents=[common],
                                     help="查询引擎真实命令清单（不猜名字）"),
                      default=30.0)
    sp.add_argument("namespace", nargs="?", default=None,
                    help="命名空间，如 network；省略则列出全部命名空间")
    sp.add_argument("command", nargs="?", default=None,
                    help="命令名，如 export；省略则列出该命名空间下的命令。"
                         "给出命令名会请求 /commands/{ns}/{cmd} —— 对**无参命令**"
                         "这一请求就是执行它，因此需配合 --allow-execute")
    sp.add_argument("--allow-execute", action="store_true",
                    help="允许请求 /commands/{ns}/{cmd}（对无参命令会真的执行）。"
                         "默认关闭；quit/exit 类命令即使加了也永久拒绝，请用 cyctl stop")
    sp.set_defaults(func=cmd_commands)

    sp = with_timeout(sub.add_parser("mcp", parents=[common],
                                     help="输出全宿主 MCP 接入矩阵，可安全落盘"), default=10.0)
    sp.add_argument("--host", default=None,
                    help="只处理指定宿主，逗号分隔。可选: %s" % ", ".join(hmat.host_ids()))
    sp.add_argument("--endpoint", default=None, help="覆盖 MCP 端点 URL")
    sp.add_argument("--project", default=None, help="项目目录（项目级配置落点），默认当前目录")
    sp.add_argument("--write-config", action="store_true", help="把配置写入磁盘（默认只打印）")
    sp.add_argument("--scope", default=None, help="只写指定 scope，如 project / user / workspace")
    sp.add_argument("--only-dedicated", action="store_true",
                    help="跳过混合配置文件（更保守，推荐）")
    sp.add_argument("--allow-mixed", action="store_true",
                    help="允许改写混合配置文件（如 ~/.claude.json）。"
                         "JSON 会保留其它键，但仍会整份重写；有备份。")
    sp.set_defaults(func=cmd_mcp)

    sp = with_timeout(sub.add_parser("bridge", parents=[common],
                                     help="stdio ↔ Streamable HTTP MCP 桥"), default=300.0)
    sp.add_argument("--endpoint", default=None,
                    help="覆盖 MCP 端点。默认 http://localhost:<port>/mcp —— 注意在端口根上，不是 /v1/mcp")
    sp.add_argument("--probe", action="store_true",
                    help="只探测 /mcp/manifest 并按 JSON 输出（不进入桥循环）")
    sp.add_argument("--insecure", action="store_true", help="跳过 TLS 校验")
    sp.set_defaults(func=cmd_bridge)

    sp = with_timeout(sub.add_parser("discover", parents=[common],
                                     help="发现已有的 Cytoscape 与 Java 17"), default=10.0)
    sp.add_argument("--no-path", action="store_true", help="不把 PATH 上的 java 计入")
    sp.add_argument("--root", action="append", help="额外的搜索根目录，可重复")
    sp.set_defaults(func=cmd_discover)

    with_timeout(sub.add_parser("doctor", parents=[common], help="全面体检"),
                 default=10.0).set_defaults(func=cmd_doctor)

    sp = with_timeout(sub.add_parser("prune", parents=[common], help="清理下载缓存"))
    sp.add_argument("--yes", dest="dry_run", action="store_false",
                    help="真正删除（默认只做 dry-run 列出体积）")
    sp.set_defaults(func=cmd_prune, dry_run=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except cyrest.CyRestError as exc:
        emit({"ok": False, **exc.to_dict()})
        return _exit_for(exc)
    except KeyboardInterrupt:
        emit({"ok": False, "error": "被用户中断"})
        return 130
    except SystemExit:
        raise
    except Exception as exc:                      # noqa: BLE001
        # 兜底：任何未预期异常也必须产出 JSON。否则 traceback 打到 stderr、
        # stdout 为空，按契约解析的调用方无法区分「内部错误」和「没有输出」。
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "kind": "internal",
              "error": "内部错误: %s: %s" % (type(exc).__name__, exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
