# AGENTS.md

本目录是一个 Agent 技能包，用于**确定性驱动原版 Cytoscape 3.10.5 引擎**。

适用于 Codex CLI、以及任何读取 `AGENTS.md` 的 Agent。
支持 MCP 的宿主也可以走 MCP 通道（见下）。

---

## 硬约束（违反即交付无效）

1. **不要用 networkx / igraph / 自写代码实现 Cytoscape 的算法。**
   所有计算必须交给 Cytoscape 引擎，否则结果不再与原版一致。
2. **不要猜测命令名或参数。** 先查再调。
3. **任何一步失败就停止**，如实报告，不要「继续跑完」。
4. **不要声称结果确定**，除非你已固定布局随机种子并把输入本地化（见 `docs/02`）。
5. **用了 system / attach 引擎就必须报实际版本** —— 这两种模式复用机器上已有的 Cytoscape，
   版本可能与锁定版本不同。

---

## 怎么用

### 0) 先体检

```bash
python scripts/cyctl.py doctor
```

一次看清环境缺什么：每一项给出 `ok` / `level` / `detail` / `fix`。
有 `level=error` 的项会汇总进 `blocking_ids`，按它给的 `fix` 补齐即可。
退出码 `0` = 关键项全过，`3` = 有阻断项。

### 1) 没有引擎？二选一

```bash
python scripts/cyctl.py discover        # 先看机器上有没有现成的
python scripts/cyctl.py start --engine system        # A. 复用系统安装（0 下载）
python scripts/cyctl.py provision --component jre \
  && python scripts/cyctl.py provision --component cytoscape --execute-installer
                                                     # B. 自带引擎（版本锁定，最确定）
```

### 2) 看能力清单

```bash
python scripts/cyctl.py describe
```

返回 JSON，含所有子命令、输出契约、支持的平台 / 宿主 / 引擎模式。

### 3) 起引擎

```bash
python scripts/cyctl.py start --port 1234     # 无头 Linux 会自动套 xvfb-run
python scripts/cyctl.py wait                  # 轮询 /v1/ 直到就绪
python scripts/cyctl.py status
```

`--engine` 取值：`auto`（默认，优先自带、回落系统）/ `bundled` / `system` / `attach`（接管已运行实例）。

### 4) 查可用命令（不要猜）

```bash
python scripts/cyctl.py rest GET commands
python scripts/cyctl.py rest GET commands/network
```

### 5) 执行

```bash
python scripts/cyctl.py cmd 'network import file file="/data/net.sif"'
python scripts/cyctl.py cmd 'layout getLayoutNames'
python scripts/cyctl.py cmd 'layout apply preferredLayout="force-directed" network=SUID:52'
python scripts/cyctl.py rest GET networks
python scripts/cyctl.py rest GET styles
```

### 6) 需要可追溯时

```bash
python scripts/cyctl.py run workflows/demo.workflow.json --out runs/demo.manifest.json
```

产出溯源清单：引擎版本、输入文件 sha256、每步请求与结果的 `result_sha256`。

---

## 输出契约

- **stdout**：唯一一个 JSON 对象。直接 `json.loads`。
- **stderr**：人类日志，忽略。
- **退出码**：`0` 成功 ｜ `2` 用法错误 ｜ `3` 环境未就绪 ｜ `4` 远端错误 ｜ `5` 完整性校验失败。
- **唯一例外**：`cyctl bridge` 的 stdout 是 MCP 协议本身（逐行 JSON-RPC），供宿主直接当端点用。

---

## MCP 通道（可选，但更省事）

九类主流的宿主配置各不相同，**直接让 cyctl 生成**，不要手写：

```bash
python scripts/cyctl.py mcp                          # 打印全部九类的配置与命令（默认不落盘）
python scripts/cyctl.py mcp --host cursor            # 只看某一类
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

各宿主的关键差异（写错不会报错，只会连不上）：

| 宿主 | 顶层键 | HTTP 字段 |
|---|---|---|
| Claude Code | `mcpServers` | `type:"http"` + `url` |
| VS Code / Copilot | **`servers`** | `type:"http"` + `url` |
| Cursor | `mcpServers` | `url`（无 `type`） |
| Gemini CLI | `mcpServers` | **`httpUrl`** |
| Windsurf / Devin | `mcpServers` | **`serverUrl`** |
| Codex CLI | TOML `[mcp_servers.<id>]` | `url` |
| Claude Desktop | `mcpServers` | 只说 stdio → `command`/`args` 走桥 |

常用命令行形式：

```bash
codex mcp add cytoscape-mcp --http-url http://localhost:1234/mcp
claude mcp add --transport http cytoscape-mcp http://localhost:1234/mcp --scope project
copilot mcp add --transport http cytoscape-mcp http://localhost:1234/mcp
```

**只支持 stdio 的宿主**（如 Claude Desktop）用本技能自带的桥，不必下载官方扩展：

```bash
python scripts/cyctl.py bridge --port 1234
```

需要先安装官方 App「Cytoscape MCP Server」。
**注意**：cyctl 拒绝自动改写混合配置文件（`~/.claude.json`、`config.toml`、`settings.json`）——
那是有意的安全设计，请按输出内容手工合并。

详见 `docs/03-跨Agent接入指南.md`。

---

## 前置条件

- Cytoscape **3.10+**，需要 **Java 17**（3.10 起不再支持 Java 11/8）。
  `cyctl discover` 会自动扫各平台的常见 JDK 位置。
- Cytoscape Desktop 是 GUI 应用，**没有**官方无头开关；
  无显示器的 Linux 用 `cyctl start --headless`（自动套 `xvfb-run`）。
- 官方只为 macOS 提供 aarch64 产物；Windows/Linux 的 ARM64 需走 `--engine system`。

---

## 参考

| 文档 | 内容 |
|---|---|
| `docs/01-可行性评估与架构决策.md` | 为什么是「驱动」而不是「重写」；覆盖天花板 |
| `docs/02-功能覆盖与确定性边界.md` | 功能矩阵、随机性来源、可复现检查清单 |
| `docs/03-跨Agent接入指南.md` | 九类宿主接入、配置写入安全规则、无头部署、故障排查 |
| `docs/05-全平台全引擎全宿主矩阵.md` | 平台 × 架构 × 引擎 × 宿主的完整覆盖矩阵 |
| `docs/07-验证与稳定性报告-2026-10-01.md` | 打包前验证与稳定性实测（当时的快照） |
| `docs/08-上游对齐审计报告-2026-10-02.md` | **与官方上游逐项比对**：能力面数字、逐端点取证、上游自身的坏接口 |
