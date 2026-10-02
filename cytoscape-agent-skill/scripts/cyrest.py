#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cyrest.py —— CyREST / Commands API 的纯逻辑访问层。

本模块是「确定性」的关键：Cytoscape 命令字符串到 HTTP URL 的映射算法
**逐字复刻**官方 Python 客户端 py4cytoscape 的实现
（py4cytoscape/commands.py 中的 _command_2_get_query / _command_2_post_query_url /
_command_2_post_query_body）。

为什么必须复刻而不是自己设计：
    映射算法一旦有偏差，命令会打到错误端点或被静默忽略，
    在医学/生信场景属于「结果不可复现」的致命缺陷。
    复刻官方实现 = 与官方客户端行为等价，可被 RCy3 / py4cytoscape / CyREST 用户交叉验证。

模块内不打印任何东西，不做参数校验以外的副作用，便于单元测试。
仅使用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_BASE_URL",
    "CyRestError",
    "resolve_base_url",
    "split_command",
    "command_to_get",
    "command_to_post",
    "cyrest_request",
    "run_command",
    "call_operation",
    "engine_version",
    "cyrest_download",
    "dangerous_command",
    "dangerous_operation",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: CyREST 默认端口。官方文档："位于 TCP/IP 端口 1234，可用 -R 参数或 rest.port 属性更改"
DEFAULT_PORT = 1234

#: 默认 base url。端点形如 http://localhost:1234/v1/networks
DEFAULT_BASE_URL = "http://localhost:%d/v1" % DEFAULT_PORT

#: 官方命令字符串中「参数名=」的识别正则。
#: 注意：等号必须放在捕获组**内部**（官方为 r' ([A-Za-z0-9_-]*=)'）。
#: 若把 '=' 放在组外，re.sub 会连同 '=' 一起消费掉而不回填，参数名会被拼接进食参值，
#: 造成「URL 正确但请求体错乱」的静默失败 —— 该坑已由 tests/test_cyctl.py 锁定。
_PARAM_MARKER_RE = r" ([A-Za-z0-9_-]*=)"

#: 参数名的识别正则（用于 GET 时把参数拆成 query dict）
_PARAM_NAME_RE = re.compile(r"[A-Za-z0-9_-]+=")

#: 参数值的切分正则
_PARAM_SPLIT_RE = re.compile(r" *[A-Za-z0-9_-]+=")


class CyRestError(RuntimeError):
    """CyREST 调用失败。attributes 携带结构化诊断信息，便于上游输出 JSON。"""

    def __init__(self, message, *, status=None, url=None, body=None, kind="remote"):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body
        self.kind = kind  # remote / transport / not_ready

    def to_dict(self):
        return {
            "error": str(self),
            "kind": self.kind,
            "http_status": self.status,
            "url": self.url,
            "body": (self.body[:4000] if isinstance(self.body, str) else self.body),
        }


# ---------------------------------------------------------------------------
# base url 解析
# ---------------------------------------------------------------------------

def resolve_base_url(explicit=None, port=None, env=None):
    """解析 base url，优先级：显式参数 > 端口参数 > 环境变量 CYTOSCAPE_BASE_URL > 默认。

    :param explicit: 完整 base url，例如 ``http://127.0.0.1:1234/v1``
    :param port: 端口号；与 explicit 二选一
    :param env: 环境变量字典（便于测试注入），默认 os.environ
    """
    if explicit:
        return explicit.rstrip("/")
    if port:
        return "http://localhost:%d/v1" % int(port)
    if env is None:
        import os
        env = os.environ
    from_env = env.get("CYTOSCAPE_BASE_URL")
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# 命令字符串 -> HTTP 请求（逐字复刻 py4cytoscape）
# ---------------------------------------------------------------------------

def split_command(cmd_string):
    """把 Cytoscape 命令字符串拆成 (命令部分, [参数片段, ...])。

    'network get attribute network="test" columnList="SUID"'
        -> ('network get attribute', ['network="test"', 'columnList="SUID"'])
    """
    if not isinstance(cmd_string, str) or not cmd_string.strip():
        raise ValueError("命令字符串不能为空")
    marked = re.sub(_PARAM_MARKER_RE, r"XXXXXX\1", cmd_string)
    parts = marked.split("XXXXXX")
    cy_cmd = parts[0] or ""
    return cy_cmd, parts[1:]


def _command_path(cy_cmd):
    """复刻 py4cytoscape：只把命令部分的**第一个**空格替换成 '/'，再做 URL 编码。"""
    namespace_rest = re.sub(" ", "/", cy_cmd, count=1)
    return urllib.parse.quote("/commands/" + namespace_rest)


def command_to_get(cmd_string, base_url=DEFAULT_BASE_URL):
    """构造 GET 请求：返回 (url, params_dict_or_None)。

    端点： ``{base_url}/commands/{namespace}/{command}``，参数走 query string。
    """
    cy_cmd, params = split_command(cmd_string)
    url = base_url + _command_path(cy_cmd)

    if not params:
        return url, None

    args = " ".join(params)
    args = re.sub(r'"', "", args)                      # 去掉所有双引号
    names = [re.sub("=", "", n) for n in _PARAM_NAME_RE.findall(args)]
    values = _PARAM_SPLIT_RE.split(args)[1:]
    arg_dict = dict(zip(names, values))
    return url, arg_dict


def command_to_post(cmd_string, base_url=DEFAULT_BASE_URL):
    """构造 POST 请求：返回 (url, body_dict)。

    端点同上，参数走 JSON body（Content-Type: application/json）。
    值按第一个 '=' 切分，双引号一律剥除 —— 与官方客户端一致。
    """
    cy_cmd, params = split_command(cmd_string)
    url = base_url + _command_path(cy_cmd)

    body = {}
    for raw in params:
        pair = re.sub(r'"', "", raw).split("=", 1)
        if not pair[0]:
            raise ValueError('命令参数缺少参数名: %r' % raw)
        body[pair[0]] = pair[1] if len(pair) > 1 else ""
    return url, body


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _build_opener(verify_tls=True, use_proxy=False):
    """构造 urllib opener。

    默认 **绕过代理**:CyREST 服务几乎总是跑在 localhost（或被显式指定的内网地址）。
    若让 urllib 走 http_proxy/HTTPS_PROXY，本机请求会被企业代理拦截并返回 502/407，
    表现为「引擎明明在跑却连不上」——这是极难定位的静默故障，实测已复现。
    """
    handlers = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


_DEFAULT_OPENER = _build_opener()


#: 「查询即执行」的高危命令动词。
#: CyREST 的 ``GET /commands/{ns}/{cmd}`` 对**不需要参数**的命令就是执行它 ——
#: 没有「只看帮助」这一说。实测这类命令会把引擎关掉，而且 GUI 模式下 JVM
#: 会卡在退出确认对话框上不退出，留下一个占 GB 的僵尸进程而端口已关。
DANGER_VERBS = frozenset({"quit", "exit", "shutdown", "halt"})


def dangerous_command(cmd_string):
    """命令字符串是否属于高危命令；是则返回归一化名字（如 ``command quit``）。

    判定只认**命令名**的第二个词（namespace 之后的那一段），不看参数值 ——
    否则 `network set attribute name="halt"` 这种正常命令会被误伤。
    """
    if not cmd_string or not cmd_string.strip():
        return None
    try:
        cy_cmd, _ = split_command(cmd_string)
    except ValueError:
        return None
    toks = cy_cmd.strip().split()
    if len(toks) < 2:
        return None
    if toks[1].lower() in DANGER_VERBS:
        return " ".join(toks[:2])
    return None


def dangerous_operation(operation):
    """REST operation 是否落在那组「查询即执行」的危险端点上。

    为什么必须在这一层拦：``cyctl commands`` 子命令早就拦了 quit/exit 类命令，
    但 ``cyctl cmd``、``cyctl rest``、以及工作流步骤是**另外的入口**。
    不在这里收口，那道保护就是形同虚设 ——
    一条 ``cyctl rest GET commands/command/quit`` 就能把引擎关掉。
    """
    op = (operation or "").lstrip("/")
    if not op.startswith("commands/"):
        return None
    rest = op[len("commands/"):]
    for sep in ("?", "#"):
        rest = rest.split(sep, 1)[0]
    parts = rest.split("/", 1)
    if len(parts) < 2:
        return None
    return dangerous_command("%s %s" % (parts[0], urllib.parse.unquote(parts[1])))


#: operation 路径里**原样保留**的字符。
#: `%` 必须在里面：否则调用方已经手写的 `%20` 会被二次编码成 `%2520`。
_OP_PATH_SAFE = "/%:@!$&'()*+,;=[]~-._"


def _encode_operation(operation):
    """把 CyREST operation 规范化成合法 URL 路径。

    为什么需要这一步：命令类端点的路径里**含空格**
    （``commands/layout/get preferred``）。urllib 遇到空格会抛
    ``http.client.InvalidURL``，而该异常既不是 OSError 也不是我们原先捕获的类型，
    会一路穿透到 main 的兜底分支 —— 实测 ``cyctl rest GET "commands/network/get preferred"``
    以**退出码 1** 结束，违反「退出码只能是 0/2/3/4/5」的契约。

    带 ``?`` 的 operation（如 ``networks/1/tables/defaultnode/rows?limit=2``）
    要拆开处理：只编码路径部分，查询串原样透传 —— 查询串里已经含有
    ``=`` ``&`` 这类语义字符，重新编码会破坏语义。
    """
    op = operation.lstrip("/")
    if "?" in op:
        path, _, query = op.partition("?")
        return urllib.parse.quote(path, safe=_OP_PATH_SAFE) + "?" + query
    return urllib.parse.quote(op, safe=_OP_PATH_SAFE)


def cyrest_request(method, url, *, params=None, body=None, timeout=120,
                   accept="application/json", verify_tls=True, use_proxy=False):
    """发起一次 CyREST HTTP 请求，返回 (parsed_result, raw_text, status)。

    - JSON 响应自动解析，非 JSON 原样返回字符串。
    - 4xx/5xx 抛 CyRestError（携带 url / status / body）。
    - HTTP 响应体里的业务错误（含 "error" 字段）也会被识别为失败，
      避免「HTTP 200 但命令实际失败」被当成成功 —— 这是静默失败的高发点。
    - 连接类错误统一归类为 kind="not_ready"，便于上游区分「引擎没起」与「引擎报错」。
    """
    data = None
    headers = {"Accept": accept}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    opener = _DEFAULT_OPENER if (verify_tls and not use_proxy) else _build_opener(verify_tls, use_proxy)

    try:
        # Request 的构造本身就可能对畸形 URL 抛 ValueError。必须一起放进 try，
        # 否则会绕过下面所有分类，直接穿透到 main 的兜底分支变成退出码 1。
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise CyRestError(
            "CyREST 返回 HTTP %s" % e.code, status=e.code, url=url, body=raw,
        )
    except urllib.error.URLError as e:
        raise CyRestError(
            "无法连接 CyREST（%s）：%s" % (url, e.reason), url=url, kind="not_ready",
        )
    except OSError as e:
        # ConnectionRefused / timeout / DNS 等
        raise CyRestError(
            "无法连接 CyREST（%s）：%s" % (url, e), url=url, kind="not_ready",
        )
    except (ValueError, http.client.HTTPException) as e:
        # URL 里含非法字符（路径带空格）、请求体不可序列化等。
        # 这类是**用法错误**，按契约应为退出码 2，不是「内部错误」。
        raise CyRestError(
            "请求无法发出（%s）：%s" % (url, e), url=url, kind="usage",
        )

    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        parsed = raw

    # 业务层错误识别：官方在 200 响应里返回 {"errors":[...]} / {"error":"..."}
    if isinstance(parsed, dict):
        for key in ("errors", "error"):
            if key in parsed and parsed[key]:
                raise CyRestError(
                    "Cytoscape 业务错误: %s" % json.dumps(parsed[key], ensure_ascii=False)[:500],
                    status=status, url=url, body=raw,
                )

    return parsed, raw, status


# ---------------------------------------------------------------------------
# 高层封装
# ---------------------------------------------------------------------------

def run_command(cmd_string, base_url=DEFAULT_BASE_URL, *, use_post=True, timeout=120):
    """执行一条 Cytoscape 命令。

    默认走 POST（官方推荐：参数无长度限制、无转义歧义）。
    POST 失败且疑似端点不支持时，可显式改用 GET。
    """
    danger = dangerous_command(cmd_string)
    if danger:
        raise CyRestError(
            "拒绝执行 %r：这类命令会让引擎退出（CyREST 的 GET /commands/{ns}/{cmd} "
            "对无参命令等于执行）。关引擎请用 `cyctl stop`。" % danger,
            kind="danger",
        )
    if use_post:
        url, body = command_to_post(cmd_string, base_url)
        return cyrest_request("POST", url, body=body, timeout=timeout)
    url, params = command_to_get(cmd_string, base_url)
    return cyrest_request("GET", url, params=params, accept="text/plain", timeout=timeout)


def call_operation(operation, base_url=DEFAULT_BASE_URL, *, method="GET",
                   params=None, body=None, timeout=120,
                   accept="application/json"):
    """调用 CyREST 函数（非命令）。

    operation 形如 ``networks`` / ``styles/Directed`` / ``apply/layouts/force-directed/53``。

    为什么必须能指定 accept：CyREST 里有一组**纯文本**端点（``commands``、
    ``commands/{ns}``、``commands/{ns}/{cmd}`` 这类 help 端点），它们只接受
    ``Accept: text/plain`` —— 发默认的 ``application/json`` 会拿到 HTTP 406。
    这不是可有可无的选项：官方自带示例工作流的第一步就是 ``rest GET commands``，
    少了这个参数那条工作流必然失败。
    """
    danger = dangerous_operation(operation)
    if danger:
        raise CyRestError(
            "拒绝调用 %r：CyREST 的 GET /commands/{ns}/{cmd} 对无参命令等于**执行**，"
            "这条会让引擎退出。关引擎请用 `cyctl stop`。" % danger,
            kind="danger",
        )
    op = _encode_operation(operation)
    url = base_url + "/" + op if op else base_url
    return cyrest_request(method.upper(), url, params=params, body=body,
                          timeout=timeout, accept=accept)


def engine_version(base_url=DEFAULT_BASE_URL, timeout=15):
    """读取引擎版本，返回 ``(version_or_None, raw_payload)``。

    端点是 ``{base}/version``，**不是** ``{base}/`` ——
    根端点只返回 allAppsStarted / apiVersion / numberOfCores / memoryStatus，
    **不含版本号**。这个差别很要命：不查清楚就会写出「拿不到版本」的溯源清单，
    而版本是判断「结果能否复现」的第一要素（同一份数据在不同引擎版本上
    可能给出不同的布局与统计结果）。

    实测返回值：``{"apiVersion": "v1", "cytoscapeVersion": "3.10.5"}``
    """
    res, raw, _ = cyrest_request("GET", base_url.rstrip("/") + "/version",
                                 timeout=timeout)
    if isinstance(res, dict):
        return res.get("cytoscapeVersion"), res
    return None, res


def _unlink_quiet(path):
    """删除临时文件，失败不抛 —— 清理动作不该掩盖真正的错误。"""
    try:
        os.remove(path)
    except OSError:
        pass


#: 判定「引擎把错误当成 200 回话」时，最多读多少字节去嗅探 JSON 信封
_JSON_SNIFF_LIMIT = 4 << 20


def cyrest_download(operation, base_url, dest, *, method="GET", params=None,
                    body=None, timeout=300, accept="*/*", verify_tls=True,
                    use_proxy=False):
    """把响应体**按字节**落盘，返回 ``{path, size, sha256, content_type, status}``。

    为什么必须有这条通道（这是一个实测确认的静默失败）：
      CyREST 有一组端点返回二进制 —— ``views/{id}.png``、``views/{id}.pdf``、
      ``networks/{id}.cx``。而 ``cyrest_request`` 会把响应体做
      ``.decode("utf-8", errors="replace")``，二进制里的非 UTF-8 字节会被替换成
      U+FFFD，**不可逆**。实测一次 72 KB 的 PNG 导出让调用方拿到一个含
      **28,798 个替换字符**的字符串，而响应仍然是 ``ok: true, http_status: 200``。
      这是比报错危险得多的失败模式：成功信封里装着垃圾，下游无从察觉。
      （对照：``.svg`` 与 ``.csv`` 是文本端点，走原通道无损；
       ``.png`` / ``.pdf`` 是二进制，必须走这里。）

    另外两点实现上的讲究：
      - ``accept`` 默认 ``*/*`` —— 与 JSON 端点不同，二进制端点对 Accept 很敏感，
        发 ``application/json`` 会让 ``.pdf`` / ``.svg`` 直接回 HTTP 406（实测）。
      - 落盘用 ``.part`` + ``os.replace`` —— 中途失败不会留下半截文件被下游
        当成完整产物；并且校验 Content-Length，防止截断被静默接受。
    """
    danger = dangerous_operation(operation)
    if danger:
        raise CyRestError(
            "拒绝调用 %r：这类命令会让引擎退出。关引擎请用 `cyctl stop`。" % danger,
            kind="danger",
        )
    op = _encode_operation(operation)
    url = base_url + "/" + op if op else base_url

    data = None
    headers = {"Accept": accept}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    dest = os.fspath(dest)
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".part"
    opener = _DEFAULT_OPENER if (verify_tls and not use_proxy) else _build_opener(verify_tls, use_proxy)

    digest = hashlib.sha256()
    written = 0
    try:
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            ctype = resp.headers.get("Content-Type")
            declared = resp.headers.get("Content-Length")
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    except urllib.error.HTTPError as e:
        _unlink_quiet(tmp)
        raise CyRestError(
            "CyREST 返回 HTTP %s" % e.code, status=e.code, url=url,
            body=e.read().decode("utf-8", errors="replace"),
        )
    except urllib.error.URLError as e:
        _unlink_quiet(tmp)
        raise CyRestError("无法连接 CyREST（%s）：%s" % (url, e.reason),
                          url=url, kind="not_ready")
    except OSError as e:
        _unlink_quiet(tmp)
        raise CyRestError("无法连接 CyREST（%s）：%s" % (url, e),
                          url=url, kind="not_ready")
    except (ValueError, http.client.HTTPException) as e:
        _unlink_quiet(tmp)
        raise CyRestError("请求无法发出（%s）：%s" % (url, e), url=url, kind="usage")

    if declared is not None:
        try:
            expect = int(declared)
        except (TypeError, ValueError):
            expect = None
        if expect is not None and expect != written:
            _unlink_quiet(tmp)
            raise CyRestError(
                "响应体长度不符：声明 %d 字节，实收 %d 字节" % (expect, written),
                status=status, url=url,
            )

    # CyREST 有「HTTP 200 + JSON 错误信封」的回应方式。二进制通道拿不到
    # cyrest_request 那层业务错误判定，这里补一次：引擎回的是 JSON 又带 errors，
    # 就不能把那张错误页当成功产物留下来。
    if written and (ctype or "").lower().startswith("application/json") \
            and written <= _JSON_SNIFF_LIMIT:
        try:
            with open(tmp, "rb") as fh:
                payload = json.loads(fh.read().decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            for key in ("errors", "error"):
                if payload.get(key):
                    _unlink_quiet(tmp)
                    raise CyRestError(
                        "Cytoscape 业务错误: %s" % json.dumps(
                            payload[key], ensure_ascii=False)[:400],
                        status=status, url=url, body=json.dumps(payload)[:4000],
                    )

    os.replace(tmp, dest)
    return {"path": os.path.abspath(dest), "size": written,
            "sha256": digest.hexdigest(), "content_type": ctype, "status": status}
