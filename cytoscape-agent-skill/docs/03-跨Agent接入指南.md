# 03 · 跨 Agent 接入指南

目标：同一套 Cytoscape 能力，在**所有**主流 Agent 上都能用 —— Claude Code、Codex CLI、
GitHub Copilot（VS Code / CLI）、Cursor、Windsurf / Devin、Gemini CLI、Claude Desktop，
以及任何自研编排或 CI 脚本。

---

## 0. 先选通道

| 你的宿主 | 用哪条 | 为什么 |
|---|---|---|
| 支持 **Streamable HTTP** 的 MCP 宿主（Claude Code、Codex CLI、Copilot、Cursor、Gemini CLI、Windsurf/Devin…） | **通道 1：MCP 直连** | 零额外进程；工具由 Cytoscape 自己发布 |
| **只支持 stdio** 的 MCP 宿主（Claude Desktop 等） | **通道 2：cyctl bridge** | 本技能自带零依赖 stdio↔HTTP 桥，不需要下载官方 `.mcpb` |
| 不带 MCP 的 Agent / 自研编排 / CI | **通道 3：cyctl CLI** | 最通用，只要能执行 shell |
| 需要严格溯源、批量跑工作流 | **通道 3：`cyctl run`** | 产出溯源清单与每步结果指纹 |

三条通道最终打的是**同一个引擎、同一套命令**，结果不会有差异。

> **一条命令拿到全部宿主的配置**：
> ```bash
> cyctl mcp                      # 打印 9 个宿主的配置文件位置 + 可直接粘贴的内容 + CLI 命令
> cyctl mcp --write-config --only-dedicated --project .   # 安全落盘（见 §2.5）
> ```

---

## 1. 三条通道的前置条件

### 1.1 拿到引擎（三选一，见 `docs/05` 的完整对比）

```bash
cyctl discover                  # 先看机器上有什么：已有 Cytoscape？已有 JDK 17？

# A. 自带引擎：版本锁定，结果可复现（推荐用于需要严格复现的场景）
cyctl provision --component jre
cyctl provision --component cytoscape --execute-installer

# B. 复用系统安装：0 字节下载
cyctl start --engine system

# C. 接管已手工打开的实例：不启动新进程
cyctl start --engine attach
```

默认 `cyctl start`（`--engine auto`）会**优先用自带引擎，没有则回落系统安装**。

### 1.2 起引擎并等待就绪

```bash
cyctl start --port 1234           # Linux 无显示器会自动套 xvfb-run
cyctl wait                        # 轮询 /v1/ 直到就绪
cyctl doctor                      # 一次看全部：缺什么、怎么补
```

### 1.3 通道 1 / 2 额外需要：官方 MCP App

通道 1 与通道 2 都依赖 Cytoscape 官方的 **Cytoscape MCP Server** App：

- 应用内 **Apps → App Manager**，搜索 `Cytoscape MCP Server` 安装；
- 或从 Releases 下载 `cytoscape-mcp-<VERSION>.jar`，**Apps → App Manager → Install from File**。

装好后可直接验证（`cyctl doctor` 也会查这一项）：

```bash
cyctl bridge --probe              # 探测 /mcp/manifest；读得到就说明 App 已生效
```

> 没装 MCP App 时，**通道 3（cyctl CLI）依然完全可用** —— 它走 CyREST，不依赖 MCP。

> Cytoscape 是**单用户**应用。多个 Agent 同时连一个实例会互相干扰。需要并行就起多个实例 + 不同 `-R` 端口。

---

## 2. 通道 1：MCP 直连（Streamable HTTP）

端点：`http://localhost:1234/mcp`（改了 CyREST 端口就换成你的端口）。

### 2.0 宿主 × 字段名对照（**这张表是本节的全部价值**）

各宿主的字段名互不相同，**写错不会报错，只会永远连不上**。以下每一项都经过官方文档核验，
并在 `tests/test_hosts_bridge.py` 里有逐条断言。

| 宿主 | 配置文件 | 顶层键 | 远程 HTTP 的字段 |
|---|---|---|---|
| Claude Code | `<项目>/.mcp.json`（项目）/ `~/.claude.json`（用户） | `mcpServers` | `type:"http"` + `url` |
| VS Code / Copilot | `<项目>/.vscode/mcp.json` | **`servers`** | `type:"http"` + `url` |
| VS Code（可移植）/ Copilot CLI | `<项目>/.mcp.json` / `~/.copilot/mcp-config.json` | `mcpServers` | `type:"http"` + `url` |
| Cursor | `<项目>/.cursor/mcp.json` / `~/.cursor/mcp.json` | `mcpServers` | **`url`（无 `type`）** |
| Gemini CLI | `~/.gemini/settings.json` / `<项目>/.gemini/settings.json` | `mcpServers` | **`httpUrl`** |
| Windsurf / Cascade / Devin | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` | **`serverUrl`** |
| OpenAI Codex CLI | `~/.codex/config.toml` | TOML 节 `[mcp_servers.<id>]` | `url` |
| Claude Desktop | `claude_desktop_config.json` | `mcpServers` | **只说 stdio → 用桥，见 §3** |
| 其它 | `<项目>/.mcp.json` | `mcpServers` | `url` |

> Gemini 里 `url` 是 **SSE**（已弃用）、`httpUrl` 才是 Streamable HTTP —— 这是最容易写错的一处。
> VS Code 里顶层键是 `servers`，写成 `mcpServers` 会被静默忽略。

### 2.1 Claude Code

```bash
claude mcp add --transport http cytoscape-mcp http://localhost:1234/mcp --scope project
claude mcp list        # 验证
```

项目级配置写在 `<项目>/.mcp.json`：

```json
{ "mcpServers": { "cytoscape-mcp": { "type": "http", "url": "http://localhost:1234/mcp" } } }
```

> 注意：Claude Code 里**只有 `url` 而没有 `type`** 是配置错误，它会被当成 stdio 服务器并报
> `has a "url" but no "type"`。

### 2.2 OpenAI Codex CLI

```bash
codex mcp add cytoscape-mcp --http-url http://localhost:1234/mcp
codex mcp list         # 或在 TUI 内输入 /mcp
```

`~/.codex/config.toml`（**混合配置文件，请手工合并，不要让工具改写**）：

```toml
[mcp_servers.cytoscape_mcp]
url = "http://localhost:1234/mcp"
```

### 2.3 GitHub Copilot

**VS Code** —— 命令面板（`Ctrl+Shift+P`）→ **MCP: Add Server** → 选 HTTP → 填 URL → 命名 `cytoscape-mcp`。
命令行等价：

```bash
code --add-mcp '{"name":"cytoscape-mcp","type":"http","url":"http://localhost:1234/mcp"}'
```

工作区文件 `<项目>/.vscode/mcp.json`：

```json
{ "servers": { "cytoscape-mcp": { "type": "http", "url": "http://localhost:1234/mcp" } } }
```

**Copilot CLI**：

```bash
copilot mcp add --transport http cytoscape-mcp http://localhost:1234/mcp
copilot mcp list
```

### 2.4 其它宿主

```bash
cyctl mcp --host cursor        # 只输出 Cursor 的配置
cyctl mcp --host gemini-cli,windsurf
```

### 2.5 让 cyctl 帮你写配置（**安全写入**）

```bash
cyctl mcp --write-config --only-dedicated --project /path/to/project --scope project
```

写入规则（这些规则本身也有测试锁定）：

| 情况 | 行为 |
|---|---|
| 目标不存在 | 新建 |
| 目标存在且是**专用** MCP 配置文件（`.mcp.json`、`.cursor/mcp.json`、`mcp_config.json`…） | **合并** —— 只新增/替换 `cytoscape-mcp` 这一个条目，其它条目原样保留，并生成 `.cyctl-bak` 备份 |
| 目标存在且是**混合**配置文件（`~/.claude.json`、`config.toml`、`settings.json`） | **拒绝改写**，只输出内容让你手工合并 |
| 内容已是最新 | 报告 `unchanged`，不写盘 |

**默认不写盘** —— 不加 `--write-config` 只打印。想连混合文件一起改必须显式加 `--allow-mixed`。

### 2.6 通道 1 自检

```bash
cyctl bridge --probe                # 探测 /mcp/manifest（推荐）
curl http://localhost:1234/mcp/health
curl http://localhost:1234/mcp/manifest   # 人可读的完整工具目录
```

---

## 3. 通道 2：stdio ↔ HTTP 桥（给只说 stdio 的宿主）

Claude Desktop 以及一些老客户端**只支持 stdio 传输**。官方为此专门发了一个 `.mcpb` 扩展；
本技能自带一个**零依赖**的等价桥，省掉下载扩展这一步，也让任何 stdio-only 客户端都能接上。

```bash
cyctl bridge --port 1234            # stdio ⇄ http://localhost:1234/mcp
```

宿主侧配置（`cyctl mcp --host claude-desktop` 会直接给你这段）：

```json
{
  "mcpServers": {
    "cytoscape-mcp": {
      "command": "<python 解释器绝对路径>",
      "args": ["<cyctl.py 绝对路径>", "bridge", "--port", "1234"]
    }
  }
}
```

**Claude Desktop 的配置位置**：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

**必须先在 Claude Desktop 里开启** Settings → Extensions → Advanced → **Use Built-in Node.js for MCP**。
改完配置要**完全退出并重启** Claude Desktop。

桥的行为要点（均已测试锁定）：

- `initialize` 同步处理，捕获并转发 `Mcp-Session-Id` —— 后续请求若不带这个头会丢会话，
  表现为「初始化成功但工具列表为空」；
- 后续请求带 `MCP-Protocol-Version`；
- 通知（无 `id`）→ 服务端 202，桥不产生任何输出；
- 服务端返回 `text/event-stream` 的 SSE 响应会被解析成普通 JSON-RPC 消息；
- **stdout 上只有 JSON-RPC 消息**，日志全在 stderr。

> `cyctl bridge` 是全套命令里**唯一** stdout 不是 cyctl JSON 信封的子命令 —— 因为它要直接充当
> 宿主的协议端点。需要机器可读结果时用 `cyctl bridge --probe`。

---

## 4. 通道 3：cyctl CLI（通用兜底）

任何能执行 shell 的宿主都能用。为了不重复，这里只列**接入方式**与**常用调用**。

### 4.1 自描述

```bash
cyctl describe     # 能力清单 + 输出契约 + 支持的平台/宿主/引擎模式
cyctl doctor       # 全面体检：每一项「缺什么 + 怎么补」
cyctl discover     # 发现已有的 Cytoscape 与 Java 17
cyctl env          # 精简环境探测
```

### 4.2 输出契约（务必遵守）

- **stdout：只放一个 JSON 对象**（唯一例外：`cyctl bridge`）
- **stderr：人类日志**
- 退出码：`0` 成功 / `2` 用法错误 / `3` 环境未就绪 / `4` 远端错误 / `5` 完整性校验失败

### 4.3 常用调用

```bash
cyctl status
cyctl cmd 'network import file file="/data/network.sif"'
cyctl cmd 'layout apply preferredLayout="force-directed" network=SUID:52'
cyctl rest GET networks
cyctl rest POST networks --body '{"data":{"name":"demo"}}'
cyctl run workflows/demo.workflow.json --out runs/demo.manifest.json
```

工作流文件格式见 `README.md`。

### 4.4 接到各宿主

| 宿主 | 做法 |
|---|---|
| **Codex / 通用 Agent** | 本目录的 `AGENTS.md` 已写好调用约定，Agent 进入目录自动读取 |
| **Claude Code** | 本目录的 `SKILL.md` 即技能清单；整个目录放到 `~/.claude/skills/` 或项目 `.claude/skills/` |
| **自研编排 / CI** | `subprocess` 调 `python scripts/cyctl.py ...`，解析 stdout JSON |
| **其它任何 Agent** | 把 `AGENTS.md` 的内容喂给它 |

---

## 5. 无头 / 服务器 / CI 部署

### 5.1 Linux 无显示器

Cytoscape Desktop 需要图形环境。官方**没有**「无 GUI」开关（网上流传的 `-N` 不存在，
见 `docs/01`），所以必须用虚拟显示。cyctl 会自动处理：

```bash
cyctl start --headless            # 自动探测并套 xvfb-run -a
```

等价的裸命令：

```bash
sudo apt-get install -y xvfb
xvfb-run -a --server-args="-screen 0 1600x1200x24" ./cytoscape.sh -R 1234
```

行为矩阵：

| 平台 | `--headless` 行为 |
|---|---|
| Windows | 不改变启动方式（GUI 进程在交互式会话中运行）；在无桌面的服务会话里仍需要交互式桌面 |
| macOS | 忽略（始终有 WindowServer） |
| Linux 有 DISPLAY | 直接启动 |
| Linux 无 DISPLAY | 自动套 `xvfb-run -a`；找不到 Xvfb 时 `cyctl doctor` 会给出安装命令 |

### 5.2 Linux 的 JDK 17

官方 `jres/` 目录**只提供 macOS 与 Windows 包**，没有 Linux 包：

```bash
sudo apt-get install -y openjdk-17-jdk          # Debian/Ubuntu
sudo yum install -y java-17-openjdk             # RHEL/CentOS
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
```

`cyctl discover` 会自动扫 `/usr/lib/jvm`、Homebrew、SDKMAN、scoop、chocolatey 等位置。

### 5.3 显存 / 驱动异常的兜底

启动报 AWT/OpenGL 错误（`GLXBadContext` 等）时追加：

```
-Dsun.java2d.opengl.fbobject=false
-Dprism.order=sw
```

经 `cyctl start --extra` 传入。**注意**：改渲染后端**可能影响导出的图像像素**，
若产物要做逐像素比对，请固定这些参数并记录进溯源清单。

---

## 6. 故障排查速查

| 现象 | 排查 |
|---|---|
| `cyctl doctor` 有用例失败 | 直接看该项的 `fix` 字段，那是可执行的下一步 |
| `ready: false` | 看 `java.ok_for_cytoscape`、`cytoscape.installed`、`cyrest.reachable` 三项 |
| 引擎在跑但连不上，报 **502** | 环境代理劫持。cyctl 对 CyREST 已强制绕过代理；自研脚本需显式禁用 |
| 引擎在跑但连不上，报 **10061/ECONNREFUSED** | 引擎确实没起，或端口不对 —— `cyctl start && cyctl wait` |
| MCP 工具列表为空 | 会话头丢失（用 `cyctl bridge` 已自动处理）；或宿主不支持 Streamable HTTP |
| MCP 连不上 | Cytoscape 没跑 / 端口不对 / **MCP App 没装**（`cyctl bridge --probe` 一测便知） |
| VS Code 里配置不生效 | 顶层键必须是 `servers`（不是 `mcpServers`）；或用了 `.vscode/mcp.json` 与 `.mcp.json` 混用 |
| Gemini CLI 连不上 | 用了 `url`（那是 SSE）—— 必须用 `httpUrl` |
| 端口 1234 被占用 | `cyctl start --port 8888`，同时改 MCP URL 与 Cytoscape 的 `rest.port` |
| 结果与上次不一致 | 按 `docs/02` §4 跑对照：先看 `inputs[*].sha256`，再看 `result_sha256` |
| 想换引擎版本 | 改 `assets/versions.lock.json` 的 `pinned` → 重跑 `tests/` → 比对 `result_sha256` |
