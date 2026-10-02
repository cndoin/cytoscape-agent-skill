---
name: cytoscape
description: >-
  确定性驱动原版 Cytoscape 桌面引擎做网络分析与可视化。当用户要求导入/导出生物网络、
  应用布局算法、计算网络统计（度、中心性、聚类）、设置可视化样式与映射、做表格数据操作、
  或者要求「结果可复现 / 与原版 Cytoscape 完全一致」时使用。结果由 Cytoscape 3.10.5
  原版 Java 引擎产生，AI 不参与任何数值计算。适用于医学与生物信息学场景。
  支持 Windows / Linux / macOS，自带引擎或复用系统安装，无头服务器可跑，
  可接入 Claude Code、Codex、Copilot、Cursor、Gemini CLI、Windsurf、Claude Desktop 等。
license: LGPL-2.1（仅驱动，不修改原引擎）
---

# 技能：cytoscape

## 这个技能做什么

把 **Cytoscape 3.10.5 原版引擎**接入到 Agent 里。它**不重新实现**任何算法 —— 所有计算都由
Cytoscape 自己的 Java 代码完成，因此结果与人工操作原版 Cytoscape 完全一致。

**AI 的唯一职责是选择调用哪一条官方命令。**

## 铁律（必须遵守）

1. **绝不用其他库（networkx / igraph / 自写实现）去「算」Cytoscape 该算的东西。**
   一旦自己算，结果就不再与原版一致。要什么功能就找对应的 Cytoscape 命令。
2. **绝不猜测命令名或参数名。** 用 `command_gateway_search`（MCP）或
   `cyctl commands <namespace> [command]` 先查，再调用。
   —— 实测踩过：`layout` 命名空间下**没有** `getLayoutNames`，真实命令是
   `get preferred`，猜错会得到 HTTP 500（`Failed to find command`）。
3. **绝不在失败后继续执行后续步骤。** 每一步的 `ok` 为 false 就停下并如实报告。
4. **不要承诺结果确定性而忽略随机性。** 力导向类布局是随机的，见
   `docs/02-功能覆盖与确定性边界.md` §2.1。可复现的是拓扑、属性与表数据，不是节点坐标。
5. **不要绕过完整性校验。** `provision` 对官方未提供 sha256 的产物默认阻断（退出码 `5`）。
   需要放行时必须显式 `--allow-unverified`，并把「接受了未校验产物」这件事告诉用户。
6. **用了 system / attach 引擎就必须报版本。** 这两种模式复用机器上已有的 Cytoscape，
   版本可能与锁定版本不同 —— 报结果时必须带上实际引擎版本，否则「结果一致」无从谈起。
7. **导出二进制（png / pdf / svg / cx）必须走 `rest --out`。** 普通 `rest GET` 会把响应体
   按 UTF-8 解码，非文本字节被替换成 U+FFFD 且**不可逆** —— 但信封仍然是 `ok:true`。
   拿到「成功」不代表文件能用，这一点在医学场景下尤其不能含糊。

## 支持范围

| 维度 | 覆盖 |
|---|---|
| 平台 | Windows / Linux / macOS（x64 与 aarch64，见 `docs/05`） |
| 引擎来源 | `bundled`（自带，版本锁定）/ `system`（复用系统安装）/ `attach`（接管运行中实例） |
| 显示 | 桌面 GUI / Linux 无头（`--headless` 自动套 `xvfb-run`） |
| 宿主 | Claude Code、Codex CLI、Copilot（VS Code/CLI）、Cursor、Gemini CLI、Windsurf/Devin、Claude Desktop、任意 MCP/CLI 客户端 |

## 执行方式（三条通道，按宿主能力选）

### 通道 A：MCP 直连（宿主支持 Streamable HTTP 时首选）

端点 `http://localhost:1234/mcp`（**端口根**，不是 `/v1/mcp`）。
工具清单用 MCP 的 `tools/list` 取（实测 **25 个**工具）；`/mcp/manifest` 只是文档，
只覆盖 4 个，**不要拿它当工具清单的事实源**。
关键工具：**`command_gateway_invoke`**（执行命令，会改状态）、
`command_gateway_get`（查命令 schema，只读）、`command_gateway_search`（全文搜命令，只读）。

```bash
cyctl mcp                    # 打印九类宿主各自的配置文件与命令
cyctl mcp --write-config --only-dedicated --project .   # 安全落盘（默认只打印）
```

### 通道 B：stdio 桥（宿主只说 stdio 时，如 Claude Desktop）

```bash
cyctl bridge --port 1234
```

### 通道 C：CLI（通用兜底）

```bash
python scripts/cyctl.py describe         # 能力清单 + 输出契约
python scripts/cyctl.py doctor           # 全面体检：缺什么 + 怎么补
python scripts/cyctl.py discover         # 发现已有的 Cytoscape 与 Java 17
python scripts/cyctl.py start --engine auto
python scripts/cyctl.py wait
python scripts/cyctl.py commands network export   # 查真实命令/参数（绝不猜名字）
python scripts/cyctl.py cmd '<命令字符串>'
python scripts/cyctl.py rest GET networks
python scripts/cyctl.py rest GET commands --accept text/plain   # help 类端点必须声明 text/plain
python scripts/cyctl.py rest GET "networks/52/views/60.png" --out view.png   # 二进制必须走 --out
python scripts/cyctl.py run <workflow.json> --out <manifest.json>
python scripts/cyctl.py prune            # 清理下载缓存（默认 dry-run）
```

**导出图片/PDF/SVG/CX 必须用 `--out`**，不要用普通 `rest GET` 去取 ——
二进制响应体会被不可逆地解码损坏，而信封里依然是 `ok:true`（详见 README「已知的坑 18」）。

**输出契约：stdout 只放一个 JSON 对象，stderr 放日志。**
（唯一例外：`cyctl bridge` 的 stdout 是 MCP 协议本身。）
退出码：`0` 成功 / `2` 用法错误 / `3` 环境未就绪 / `4` 远端错误 / `5` 校验失败。

## 标准流程

1. `cyctl doctor` —— 一次看清环境缺什么。有阻断项就按 `fix` 字段补齐，别硬闯。
2. 没有引擎时二选一：`cyctl provision --component cytoscape --execute-installer`（自带，最确定），
   或 `cyctl start --engine system`（复用已装，0 下载）。
3. `cyctl start && cyctl wait`，确认就绪。
4. 查可用命令，再调用。
5. 需要可追溯时用 `cyctl run <workflow.json>`：产出溯源清单，含引擎版本、输入哈希、每步结果指纹。
6. 报结果时，**把引擎版本和溯源清单路径一起报出来**。

## 详细的命令写法与坑

- 命令语言与 URL 映射细节、随机性清单 → `docs/02-功能覆盖与确定性边界.md`
- 各宿主接入、配置写入安全规则、无头部署、故障排查 → `docs/03-跨Agent接入指南.md`
- 平台/架构/引擎/宿主的完整覆盖矩阵 → `docs/05-全平台全引擎全宿主矩阵.md`
- 为什么这样设计、覆盖边界在哪 → `docs/01-可行性评估与架构决策.md`
- **与官方上游逐项比对的结果、上游自身有哪些坏接口 → `docs/08-上游对齐审计报告-2026-10-02.md`**

## 别做的事

- 不要为了让「看起来能跑」而伪造结果或跳过校验。
- 不要在环境未就绪时假装成功。
- 不要修改 `scripts/cyrest.py` 的命令→URL 映射算法 —— 它逐字复刻官方客户端，有测试锁定。
- 不要用工具自动改写**混合配置文件**（`~/.claude.json`、`config.toml`、`settings.json`）——
  cyctl 默认会拒绝，这是有意的。
- 不要对**无参命令**发 `GET /commands/{ns}/{cmd}` 去「看帮助」—— CyREST 的语义是
  「该请求即执行」，`cyctl commands command quit` 会**真的把引擎关掉**（GUI 模式下
  JVM 还可能卡在退出确认框上不退出，留下一个占 GB 的僵尸进程）。cyctl 已默认拦截：
  指定 command 时只做只读的存在性查询，要拿参数清单必须显式 `--allow-execute`，
  而 quit/exit 类命令**永久拒绝**。关引擎请用 `cyctl stop`。
- 工作流里不要写相对路径就以为引擎能找到 —— 执行期读文件的是引擎，它的 cwd 是
  自己的安装目录。用 `{input}` / `{workflow_dir}` 占位符，inputs 的相对路径会按
  **工作流文件所在目录**解析。
