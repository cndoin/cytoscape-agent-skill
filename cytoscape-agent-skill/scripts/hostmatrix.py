#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hostmatrix.py —— 主流 Agent 宿主的 MCP 配置矩阵（纯数据 + 纯函数）。

职责：把「宿主要连 Cytoscape 的 MCP 服务」这一件事，翻译成**该宿主能直接吃下的
配置片段**。每个宿主的字段名都不一样，猜错就是静默失败（配置写进去了但连不上），
所以本文件的每一条格式都带 `doc` 字段指向核验过的官方文档。

事实来源（均为官方文档，2026-10 核验）：
  * 官方 Cytoscape MCP 接入说明（各宿主命令由 Cytoscape 官方给出）：
    evidence/cytoscape-mcp-agent-config.md
  * VS Code / GitHub Copilot：
    https://code.visualstudio.com/docs/copilot/chat/mcp-servers
  * Cursor：https://cursor.com/docs/context/mcp
  * Claude Code：https://code.claude.com/docs/en/mcp
  * Gemini CLI：
    https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md
  * Codex CLI：https://learn.chatgpt.com/docs/config-file/config-reference
  * Windsurf / Cascade（现由 Devin Desktop 文档承接）：
    https://docs.windsurf.com/windsurf/cascade/mcp
  * Claude Desktop 配置文件路径：
    https://modelcontextprotocol.io/quickstart/user

关键差异（这就是必须逐宿主定制的原因）：
  | 宿主            | 顶层键        | 远程 HTTP 字段      |
  |----------------|--------------|--------------------|
  | Claude Code    | mcpServers   | type=http + url    |
  | VS Code        | servers      | type=http + url    |
  | Copilot CLI    | mcpServers   | —                  |
  | Cursor         | mcpServers   | url（无 type）      |
  | Gemini CLI     | mcpServers   | httpUrl            |
  | Windsurf/Devin | mcpServers   | serverUrl          |
  | Codex          | TOML 节       | url                |
  | Claude Desktop | mcpServers   | 仅 stdio → 需桥接    |

仅使用标准库。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "SERVER_NAME",
    "HOSTS",
    "entry_style_for",
    "render_server_entry",
    "render_container",
    "iter_configs",
    "cli_command_for",
    "build_matrix",
]

#: MCP 服务名。官方文档统一叫 cytoscape-mcp，沿用可让注册表安装与手工配置一致。
SERVER_NAME = "cytoscape-mcp"

#: 官方 MCP 注册表标识（MCP Registry）
MCP_REGISTRY_ID = "io.github.cytoscape/cytoscape-desktop-mcp-bridge"


# ---------------------------------------------------------------------------
# 宿主定义
# ---------------------------------------------------------------------------
# entry_style 决定「单个 server 条目」的 JSON 形状：
#   type_url   -> {"type":"http","url":U}
#   url_only   -> {"url":U}
#   http_url   -> {"httpUrl":U}
#   server_url -> {"serverUrl":U}
#   stdio_bridge -> {"command":PY,"args":[CTL,"bridge","--port",P]}
#
# configs 里每条 = 一个可落盘位置：
#   scope / path 模板 / key（JSON 顶层键；Codex 用 toml）/ platforms（为空=通用）/ note

HOSTS = [
    {
        "id": "claude-code",
        "name": "Claude Code",
        "aka": ["claude", "claude cli", "anthropic claude code"],
        "transport": "http",
        "entry_style": "type_url",
        "configs": [
            {"scope": "project", "path": "{proj}/.mcp.json", "key": "mcpServers",
             "note": "项目级，随仓库提交，团队共享。"},
            {"scope": "user", "path": "{home}/.claude.json", "key": "mcpServers",
             "note": "用户级。注意 MCP 的 local 作用域也落在这个文件里，"
                     "按项目路径存在 projects.<路径>.mcpServers 下。"},
        ],
        "cli": "claude mcp add --transport http {name} {url} --scope project",
        "verify": "claude mcp list",
        "doc": "https://code.claude.com/docs/en/mcp",
    },
    {
        "id": "vscode",
        "name": "VS Code / GitHub Copilot",
        "aka": ["copilot", "github copilot", "vs code", "vscode", "code"],
        "transport": "http",
        "entry_style": "type_url",
        "configs": [
            {"scope": "workspace", "path": "{proj}/.vscode/mcp.json", "key": "servers",
             "note": "VS Code 原生格式，顶层键是 servers（不是 mcpServers）。"},
            {"scope": "workspace-portable", "path": "{proj}/.mcp.json", "key": "mcpServers",
             "note": "跨工具可移植格式，Agent Host 也读它。"},
            {"scope": "user", "path": "{home}/.copilot/mcp-config.json", "key": "mcpServers",
             "note": "Copilot Agent Host 原生读取的用户级位置。"},
        ],
        "cli": "code --add-mcp '{{\"name\":\"{name}\",\"type\":\"http\",\"url\":\"{url}\"}}'",
        "verify": "MCP: List Servers（命令面板）",
        "doc": "https://code.visualstudio.com/docs/copilot/chat/mcp-servers",
    },
    {
        "id": "copilot-cli",
        "name": "GitHub Copilot CLI",
        "aka": ["copilot cli", "gh copilot"],
        "transport": "http",
        "entry_style": "type_url",
        "configs": [
            {"scope": "user", "path": "{home}/.copilot/mcp-config.json", "key": "mcpServers",
             "note": "与 VS Code 的 Agent Host 共用同一文件。"},
        ],
        "cli": "copilot mcp add --transport http {name} {url}",
        "verify": "copilot mcp list",
        "doc": "evidence/cytoscape-mcp-agent-config.md",
    },
    {
        "id": "codex",
        "name": "OpenAI Codex CLI",
        "aka": ["codex", "codex cli", "openai codex"],
        "transport": "http",
        "entry_style": "toml_url",
        "configs": [
            {"scope": "user", "path": "{home}/.codex/config.toml", "key": "mcp_servers",
             "format": "toml",
             "note": "TOML 节 [mcp_servers.<id>]，远程用 url 字段（流式 HTTP）。"},
        ],
        "cli": "codex mcp add {name} --http-url {url}",
        "verify": "codex mcp list",
        "doc": "https://learn.chatgpt.com/docs/config-file/config-reference",
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "aka": ["cursor ai", "cursor ide"],
        "transport": "http",
        "entry_style": "url_only",
        "configs": [
            {"scope": "project", "path": "{proj}/.cursor/mcp.json", "key": "mcpServers",
             "note": "项目级。"},
            {"scope": "user", "path": "{home}/.cursor/mcp.json", "key": "mcpServers",
             "note": "全局级。"},
        ],
        "cli": None,
        "verify": "Cursor → Settings → MCP（或 Customize 页面）",
        "doc": "https://cursor.com/docs/context/mcp",
    },
    {
        "id": "gemini-cli",
        "name": "Gemini CLI",
        "aka": ["gemini", "google gemini cli"],
        "transport": "http",
        "entry_style": "http_url",
        "configs": [
            {"scope": "user", "path": "{home}/.gemini/settings.json", "key": "mcpServers",
             "note": "流式 HTTP 用 httpUrl（url 字段在 Gemini 里是 SSE，语义不同，别写错）。"},
            {"scope": "project", "path": "{proj}/.gemini/settings.json", "key": "mcpServers",
             "note": "项目级。"},
        ],
        "cli": "gemini mcp add --transport http {name} {url}",
        "verify": "gemini mcp list",
        "doc": "https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md",
    },
    {
        "id": "windsurf",
        "name": "Windsurf / Cascade（Devin Desktop）",
        "aka": ["windsurf", "cascade", "codeium", "devin"],
        "transport": "http",
        "entry_style": "server_url",
        "configs": [
            {"scope": "user", "path": "{home}/.codeium/windsurf/mcp_config.json",
             "key": "mcpServers",
             "note": "Windsurf 经典路径。"},
            {"scope": "user-devin", "path": "{xdg}/devin/mcp_config.json",
             "key": "mcpServers", "platforms": ["linux", "darwin"],
             "note": "2026 年文档已由 Devin Desktop 承接，Linux/macOS 落到 XDG 目录。"},
            {"scope": "user-devin", "path": "{appdata}/devin/mcp_config.json",
             "key": "mcpServers", "platforms": ["windows"],
             "note": "同上，Windows 落到 %APPDATA%。"},
        ],
        "cli": None,
        "verify": "Cascade 面板 → ... → MCPs",
        "doc": "https://docs.windsurf.com/windsurf/cascade/mcp",
    },
    {
        "id": "claude-desktop",
        "name": "Claude Desktop",
        "aka": ["claude desktop app"],
        "transport": "stdio",
        # 关键事实：Claude Desktop 扩展只讲 stdio，官方为此专门发了 .mcpb 桥。
        # 我们提供一个零依赖桥，省掉下载 .mcpb 这一步。
        "entry_style": "stdio_bridge",
        "configs": [
            {"scope": "user", "path": "{home}/Library/Application Support/Claude/claude_desktop_config.json",
             "key": "mcpServers", "platforms": ["darwin"], "format": "json",
             "note": "需在 设置 → 扩展 → 高级 里启用「使用内置 Node.js for MCP」。"},
            {"scope": "user", "path": "{appdata}/Claude/claude_desktop_config.json",
             "key": "mcpServers", "platforms": ["windows"], "format": "json",
             "note": "需在 设置 → 扩展 → 高级 里启用「使用内置 Node.js for MCP」。"},
            {"scope": "user", "path": "{xdg}/Claude/claude_desktop_config.json",
             "key": "mcpServers", "platforms": ["linux"], "format": "json",
             "note": "Linux 上 Claude Desktop 支持有限，路径按 XDG 惯例。"},
        ],
        "cli": None,
        "verify": "Customize → Connectors",
        "doc": "https://modelcontextprotocol.io/quickstart/user",
    },
    {
        "id": "generic-mcp",
        "name": "任何支持 Streamable HTTP 的 MCP 客户端",
        "aka": ["generic", "generic-mcp", "other", "自定义"],
        "transport": "http",
        "entry_style": "url_only",
        "configs": [
            {"scope": "portable", "path": "{proj}/.mcp.json", "key": "mcpServers",
             "note": "通用可移植格式（mcpServers + url）。不确定宿主格式时先试这个。"},
        ],
        "cli": None,
        "verify": "客户端自带的连接状态页",
        "doc": "https://modelcontextprotocol.io/",
    },
]


# ---------------------------------------------------------------------------
# 路径模板展开
# ---------------------------------------------------------------------------

def _path_vars(project_dir=None):
    home = Path.home()
    appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return {
        "home": str(home),
        "appdata": appdata,
        "xdg": xdg,
        "proj": str(Path(project_dir).resolve()) if project_dir else "<PROJECT_DIR>",
    }


def _expand(template, project_dir=None):
    return template.format(**_path_vars(project_dir))


def _host(host_id):
    for h in HOSTS:
        if h["id"] == host_id:
            return h
    return None


def host_ids():
    return [h["id"] for h in HOSTS]


def entry_style_for(host_id):
    h = _host(host_id)
    if not h:
        raise KeyError("未知宿主: %s（可用: %s）" % (host_id, ", ".join(host_ids())))
    return h["entry_style"]


# ---------------------------------------------------------------------------
# 渲染：单个 server 条目
# ---------------------------------------------------------------------------

def render_server_entry(entry_style, *, url, port, python_exe, cyctl_path,
                        name=SERVER_NAME):
    """按宿主的 entry_style 生成「一个 server 条目」的 dict（或 Codex 的 TOML 行）。

    :param entry_style: type_url / url_only / http_url / server_url / stdio_bridge / toml_url
    :param url: MCP Streamable HTTP 端点，例如 http://localhost:1234/mcp
    :param port: CyREST 端口（stdio 桥需要）
    :param python_exe: 用于 stdio 桥的解释器绝对路径
    :param cyctl_path: cyctl.py 的绝对路径（stdio 桥的入口）
    """
    if entry_style == "type_url":
        return {"type": "http", "url": url}
    if entry_style == "url_only":
        return {"url": url}
    if entry_style == "http_url":
        return {"httpUrl": url}
    if entry_style == "server_url":
        return {"serverUrl": url}
    if entry_style == "stdio_bridge":
        # stdio 桥：宿主按命令行拉起进程，桥再把 stdio 的 JSON-RPC 转发到 HTTP。
        return {
            "command": str(python_exe),
            "args": [str(cyctl_path), "bridge", "--port", str(port)],
        }
    if entry_style == "toml_url":
        # Codex 的 TOML 节内容（由 render_container 负责加节头）
        return {"url": url}
    raise ValueError("未知 entry_style: %s" % entry_style)


def render_container(key, name, entry):
    """把条目包进宿主要求的顶层容器。"""
    return {key: {name: entry}}


# ---------------------------------------------------------------------------
# TOML（仅 Codex 需要；零依赖手写，避免引入 toml 库）
# ---------------------------------------------------------------------------

def _toml_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _toml_string(s):
    return '"%s"' % _toml_escape(s)


def _toml_array(items):
    return "[%s]" % ", ".join(_toml_string(i) for i in items)


def render_toml_section(key, name, entry):
    """生成 Codex 的 [mcp_servers.<name>] 节文本（含前置说明注释）。

    注意：Codex 的服务器 id 用下划线更稳妥（TOML 点号会被解析成嵌套表），
    这里把名字里的短横线换成下划线。
    """
    sid = name.replace("-", "_")
    lines = [
        "# ---- Cytoscape MCP（由 cyctl 生成）----",
        "# 前置条件：Cytoscape Desktop 正在运行，且已安装官方 App「Cytoscape MCP Server」。",
        "[%s.%s]" % (key, sid),
    ]
    for k, v in entry.items():
        if isinstance(v, list):
            lines.append("%s = %s" % (k, _toml_array(v)))
        elif isinstance(v, bool):
            lines.append("%s = %s" % (k, "true" if v else "false"))
        elif isinstance(v, int):
            lines.append("%s = %d" % (k, v))
        else:
            lines.append("%s = %s" % (k, _toml_string(v)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------

def iter_configs(host_id, project_dir=None, platform_key=None):
    """列出该宿主在**当前平台**适用的可落盘位置。

    返回 [{scope, path, key, format, exists, note}]
    """
    h = _host(host_id)
    if not h:
        raise KeyError("未知宿主: %s" % host_id)
    plat = platform_key or _current_platform()
    out = []
    for c in h["configs"]:
        if c.get("platforms") and plat not in c["platforms"]:
            continue
        p = _expand(c["path"], project_dir)
        # 含未解析占位符（如 <PROJECT_DIR>）时标记为需要用户指定
        unresolved = "<PROJECT_DIR>" in p
        out.append({
            "scope": c["scope"],
            "path": p,
            "key": c["key"],
            "format": c.get("format", "json"),
            "exists": (not unresolved) and Path(p).exists(),
            "needs_project_dir": unresolved,
            "dedicated": is_dedicated_config(p),
            "note": c.get("note"),
        })
    return out


def _current_platform():
    import platform as _p
    s = _p.system().lower()
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "darwin"
    return "linux"


#: 「混合」配置文件 —— 里面除了 MCP 还装着宿主的其它设置（历史记录、主题等）。
#: 对这类文件**绝不能**整份覆写，也不宜自动改写：它们往往很大且结构私有。
#: 专用 MCP 配置文件则可以安全创建/合并。
_MIXED_CONFIG_BASENAMES = {"config.toml", "settings.json", ".claude.json"}


def is_dedicated_config(path):
    """该配置文件是否「只为 MCP 而存在」（可安全自动写入）。"""
    return Path(path).name.lower() not in _MIXED_CONFIG_BASENAMES


def render_file(host_id, *, url, port, python_exe, cyctl_path,
                project_dir=None, scope=None, name=SERVER_NAME, platform_key=None):
    """渲染出「可直接落盘的内容」。

    返回 (config_info, text)。config_info 是 iter_configs 的对应项。
    仅处理该宿主在当前平台适用的第一个（或指定 scope 的）位置。
    """
    h = _host(host_id)
    if not h:
        raise KeyError("未知宿主: %s" % host_id)
    configs = iter_configs(host_id, project_dir, platform_key)
    if not configs:
        raise KeyError("宿主 %s 在当前平台没有可用的配置位置" % host_id)
    cfg = configs[0]
    if scope:
        cfg = next((c for c in configs if c["scope"] == scope), None)
        if cfg is None:
            raise KeyError("宿主 %s 没有 scope=%s 的配置位置" % (host_id, scope))

    style = entry_style_for(host_id)
    entry = render_server_entry(style, url=url, port=port,
                               python_exe=python_exe, cyctl_path=cyctl_path, name=name)

    if cfg["format"] == "toml":
        text = render_toml_section(cfg["key"], name, entry)
    else:
        import json
        text = json.dumps(render_container(cfg["key"], name, entry),
                          ensure_ascii=False, indent=2) + "\n"
    return cfg, text


def cli_command_for(host_id, *, url, name=SERVER_NAME):
    """返回该宿主推荐的 CLI 安装命令（没有则为 None）。"""
    h = _host(host_id)
    if not h or not h.get("cli"):
        return None
    return h["cli"].format(name=name, url=url)


def build_matrix(*, url, port, python_exe, cyctl_path, project_dir=None,
                 platform_key=None, hosts=None):
    """产出完整矩阵（供 cyctl mcp 输出 JSON）。

    对每个宿主给出：容器格式、可落盘位置、渲染好的内容、CLI 命令、校验方式、文档来源。
    """
    plat = platform_key or _current_platform()
    targets = hosts or [h["id"] for h in HOSTS]
    out = []
    for hid in targets:
        h = _host(hid)
        if not h:
            out.append({"id": hid, "error": "未知宿主"})
            continue
        cfgs = iter_configs(hid, project_dir, plat)
        rendered = []
        for c in cfgs:
            try:
                _, text = render_file(hid, url=url, port=port, python_exe=python_exe,
                                      cyctl_path=cyctl_path, project_dir=project_dir,
                                      scope=c["scope"], platform_key=plat)
                rendered.append({"scope": c["scope"], "path": c["path"],
                                 "key": c["key"],
                                 "format": c["format"], "exists": c["exists"],
                                 "needs_project_dir": c["needs_project_dir"],
                                 "dedicated": c["dedicated"],
                                 "note": c["note"], "content": text})
            except Exception as exc:              # noqa: BLE001
                rendered.append({"scope": c["scope"], "path": c["path"],
                                 "error": str(exc)})
        out.append({
            "id": hid,
            "name": h["name"],
            "aka": h.get("aka", []),
            "transport": h["transport"],
            "entry_style": h["entry_style"],
            "configs": rendered,
            "cli": cli_command_for(hid, url=url),
            "verify": h.get("verify"),
            "doc": h.get("doc"),
        })
    return out


# ---------------------------------------------------------------------------
# 安全写入
# ---------------------------------------------------------------------------
# 写入用户配置属于「修改别人的文件」，必须极其保守：
#   1. 默认只打印、不落盘（由 cyctl mcp --write-config 显式开启）；
#   2. 混合配置文件（~/.claude.json / config.toml / settings.json）一律拒绝自动改写，
#      它们包含宿主的其它全部设置，整份重写风险不可接受；
#   3. 任何改动前先备份原文件；
#   4. 合并不是覆盖 —— 只新增/替换本服务那一个条目。

def _backup(path):
    """在目标同目录生成 .cyctl-bak 备份，返回备份路径（失败返回 None）。"""
    p = Path(path)
    if not p.exists():
        return None
    bak = p.with_name(p.name + ".cyctl-bak")
    try:
        bak.write_bytes(p.read_bytes())
        return str(bak)
    except OSError:
        return None


def write_config(path, *, key, name, entry, fmt="json", force=False,
                 allow_mixed=False, backup=True):
    """把条目安全写入目标配置。

    返回 {action, path, backup?, reason?}，action ∈
      created / merged / unchanged / refused
    """
    p = Path(path)
    dedicated = is_dedicated_config(p)

    if not dedicated and not allow_mixed:
        return {
            "action": "refused", "path": str(p),
            "reason": "这是混合配置文件（除 MCP 外还含宿主其它设置），"
                      "cyctl 不会自动改写它。请按输出的 content 手工合并，"
                      "或改用该宿主的 CLI 命令。",
        }

    if fmt == "toml":
        section = "[%s.%s]" % (key, name.replace("-", "_"))
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if section in old:
            return {"action": "unchanged", "path": str(p),
                    "reason": "已存在 %s 节" % section}
        bak = _backup(p) if (backup and p.exists()) else None
        p.parent.mkdir(parents=True, exist_ok=True)
        text = old
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += render_toml_section(key, name, entry)
        p.write_text(text, encoding="utf-8", newline="\n")
        return {"action": "merged" if bak else "created", "path": str(p), "backup": bak}

    # JSON
    import json
    existed = p.exists()
    obj = {}
    if existed:
        raw = p.read_text(encoding="utf-8")
        if raw.strip():
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                return {"action": "refused", "path": str(p),
                        "reason": "现有文件不是合法 JSON（%s），拒绝改写以免破坏它。" % exc}
            if not isinstance(obj, dict):
                return {"action": "refused", "path": str(p),
                        "reason": "现有 JSON 顶层不是对象，拒绝改写。"}
    bucket = obj.setdefault(key, {})
    if not isinstance(bucket, dict):
        return {"action": "refused", "path": str(p),
                "reason": "现有配置里 %r 不是对象，拒绝改写。" % key}
    if bucket.get(name) == entry:
        return {"action": "unchanged", "path": str(p), "reason": "条目已是最新"}

    bak = _backup(p) if (backup and existed) else None
    bucket[name] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8", newline="\n")
    return {"action": "merged" if existed else "created", "path": str(p), "backup": bak}
