# cytoscape-agent-skill

把 **Cytoscape 3.10.5 原版引擎**接入任意 Agent 的技能包。

**核心设计：不重写任何算法。** 所有计算由 Cytoscape 自己的 Java 代码完成，
AI 只决定"调用哪一条官方命令"。因此结果与人工操作原版 Cytoscape **构造性一致**——
不是因为"努力保证"，而是因为跑的就是同一份代码。

---

## 快速开始

```bash
# 1) 体检：一次看清环境缺什么、怎么补
python scripts/cyctl.py doctor

# 2) 三选一拿到引擎
python scripts/cyctl.py discover                                    # 先看机器上有没有现成的
python scripts/cyctl.py start --engine system                       # A. 复用系统安装（0 下载）
python scripts/cyctl.py provision --component jre \
  && python scripts/cyctl.py provision --component cytoscape --execute-installer
                                                                    # B. 自带引擎（约 580 MB，版本锁定）
# C. 已经手工开着 Cytoscape？直接接管，不启动新进程
python scripts/cyctl.py start --engine attach

# 3) 启动并等待就绪（无头 Linux 会自动套 xvfb-run）
python scripts/cyctl.py start --port 1234
python scripts/cyctl.py wait

# 4) 执行命令
python scripts/cyctl.py cmd 'network import file file="/data/network.sif"'
python scripts/cyctl.py rest GET networks

# 5) 把 MCP 接到你的 Agent（九类宿主一次生成，默认只打印不落盘）
python scripts/cyctl.py mcp
```

---

## 目录结构

```
cytoscape-agent-skill/
├── SKILL.md                          # Claude Code / 通用 Skill 清单
├── AGENTS.md                         # Codex / 通用 Agent 入口
├── README.md                         # 本文件
├── docs/
│   ├── 01-可行性评估与架构决策.md       # 先读这个：为什么这么设计、边界在哪
│   ├── 02-功能覆盖与确定性边界.md       # 功能矩阵、随机性来源、可复现清单
│   ├── 03-跨Agent接入指南.md            # 九类宿主接入、配置写入安全规则、无头部署、排障
│   ├── 04-全面检查报告-2026-10-01.md    # 完整性/稳定性全面检查：发现的问题与修复
│   ├── 05-全平台全引擎全宿主矩阵.md      # 平台 × 架构 × 引擎 × 宿主的完整覆盖矩阵
│   ├── 06-全平台全宿主扩展报告.md        # 扩展过程中的取舍与实测
│   ├── 07-验证与稳定性报告-2026-10-01.md # 端到端联调与稳定性实验
│   └── 08-上游对齐审计报告-2026-10-02.md # 与官方上游逐项比对 + 7 个缺陷的取证
├── assets/versions.lock.json          # 版本锁定 + 官方 sha256/md5 + 支持矩阵
├── scripts/
│   ├── cyrest.py                      # 纯逻辑层：命令→URL 映射、HTTP、错误分类、二进制通道
│   ├── cyctl.py                       # CLI 执行器（命令编排）
│   ├── hostmatrix.py                  # 九类宿主的 MCP 配置矩阵 + 安全写入
│   ├── mcpbridge.py                   # stdio ⇄ Streamable HTTP 的 MCP 桥
│   └── enginediscovery.py             # 跨平台发现 Cytoscape 与 Java 17
├── tests/
│   ├── test_cyctl.py                  # 命令解析 / URL 映射 / 版本锁定 / 输出契约
│   ├── test_safety.py                 # 归档安全 / 下载稳定性 / 危险命令 / 二进制契约
│   ├── test_hosts_bridge.py           # 宿主字段名 / 配置安全写入 / MCP 桥 / 引擎发现
│   └── test_stability.py              # 代理策略 / 并发 / 幂等 / 敌意输入 / 实引擎端到端
├── tools/
│   ├── package.py                     # 打包（zip/tar.gz + SHA256SUMS + --verify）
│   ├── mcp_e2e.py                     # MCP 协议往返验证
│   └── audit/                         # 上游对齐审计脚本（docs/08 §11 的复现入口）
│       ├── engine_command_surface.py  # 枚举引擎命令面    → evidence/engine-command-surface.json
│       ├── upstream_client_surface.py # 枚举官方客户端函数面 → evidence/py4cytoscape-…-surface.json
│       ├── probe_rest_endpoints.py    # REST 端点逐条实跑  → evidence/rest-endpoint-probe.json
│       └── audit_cli_contract.py      # CLI 输出契约取证  → evidence/cli-entry-audit.json
├── workflows/demo.workflow.json       # 工作流示例
├── evidence/                          # 官方哈希清单 / swagger 与函数面快照 / 各项审计证据
└── runtime/                           # （运行时生成）引擎与下载缓存
```

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `cyctl doctor` | **全面体检**：逐项给出「缺什么 + 怎么补」，有阻断项时退出码 `3` |
| `cyctl discover` | **跨平台发现**已有的 Cytoscape 与 Java 17（多来源、多路径） |
| `cyctl env` | 精简环境探测，给出 `ready` 结论 |
| `cyctl describe` | 机器可读的能力清单 + 输出契约 + 支持的平台/宿主/引擎模式 |
| `cyctl provision --component jre\|cytoscape` | 按锁定版本下载并**强制校验**；`--execute-installer` 静默安装；`--variant` 选架构 |
| `cyctl start` | 启动引擎；`--engine auto\|bundled\|system\|attach`；`--headless` 自动套 xvfb |
| `cyctl wait [--timeout 300]` | 轮询 `/v1/` 直到就绪 |
| `cyctl status` | 读取 `GET /v1/` 引擎信息 |
| `cyctl cmd '<命令>' [--get]` | 执行 Cytoscape 命令（默认 POST） |
| `cyctl rest <METHOD> <操作> [--param k=v] [--body JSON] [--accept MIME] [--out FILE]` | 调用 CyREST 函数；`--out` 走**字节通道**导出 PNG/PDF/SVG/CX 等二进制 |
| `cyctl commands [ns] [cmd] [--allow-execute]` | 只读查命令面；指定命令名时只做存在性查询（见「已知的坑 14」） |
| `cyctl run <workflow.json> [--out manifest.json]` | 确定性执行 + 溯源清单 |
| `cyctl mcp` | **九类宿主的 MCP 配置矩阵**；`--write-config` 安全落盘（默认只打印） |
| `cyctl bridge` | **stdio ⇄ Streamable HTTP 桥**（供 Claude Desktop 等只说 stdio 的宿主） |
| `cyctl prune [--yes]` | 清理下载缓存（默认 dry-run，只报告体积） |
| `cyctl stop` | 停止引擎：先发 `POST command quit` 优雅退出（`Accept: application/json`），确认端口关闭；未关再按端口定位进程 |

**输出契约**：stdout 只放一个 JSON 对象；stderr 放日志。
退出码 `0/2/3/4/5` = 成功/用法错误/环境未就绪/远端错误/完整性校验失败。

契约是**全路径**的：用法错误、参数非法、内部异常……任何退出路径下，
stdout 都有且只有一个合法 JSON 对象。调用方可以直接 `json.loads(stdout)`，
不需要先判断它是否为空。

---

## 工作流格式

`cyctl run` 读取的 JSON：

```json
{
  "name": "demo",
  "inputs": ["data/network.sif", "data/expr.csv"],
  "steps": [
    { "command": "network import file file=\"data/network.sif\"" },
    { "command": "table import file file=\"data/expr.csv\" keyColumnList=\"name\"" },
    { "command": "layout apply preferredLayout=\"force-directed\" network=SUID:52" },
    { "rest": { "method": "GET", "operation": "networks" } },
    { "rest": { "method": "GET", "operation": "styles" } }
  ]
}
```

执行后产出溯源清单（`schema: cyctl/run-manifest/v1`），含：

- `engine.info` —— 引擎版本原样返回
- `inputs[*].sha256` —— 输入文件指纹
- `steps[*].request` —— 实际发出的 URL 与请求体
- `steps[*].result` 与 `result_sha256` —— 每步结果及其指纹
- `ok` —— 任一步失败即为 `false`，且**立即中断**

**同一引擎版本 + 同一输入 + 同一工作流 ⇒ `result_sha256` 应完全一致。**
指纹变化就是有人动了版本、数据或 App 集合——见 `docs/02` §4 的回归方法。

---

## 跨 Agent 兼容

**九类宿主一次生成**，不要手写（字段名各家不同，写错不报错、只会连不上）：

```bash
python scripts/cyctl.py mcp                        # 全部打印
python scripts/cyctl.py mcp --host cursor          # 指定宿主
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

| 宿主 | 传输 | 关键字段 |
|---|---|---|
| Claude Code | HTTP | `mcpServers` + `type:"http"` + `url` |
| VS Code / Copilot | HTTP | **`servers`** + `type:"http"` + `url` |
| Copilot CLI | HTTP | `mcpServers` + `type:"http"` + `url` |
| OpenAI Codex CLI | HTTP | TOML `[mcp_servers.<id>]` + `url` |
| Cursor | HTTP | `mcpServers` + `url`（无 `type`） |
| Gemini CLI | HTTP | `mcpServers` + **`httpUrl`** |
| Windsurf / Devin | HTTP | `mcpServers` + **`serverUrl`** |
| Claude Desktop | **stdio** | `mcpservers` + `command`/`args` → 走 `cyctl bridge` |
| 任意自定义 / CI | 任意 | 走 CLI；把 `AGENTS.md` 内容喂给它 |

MCP 通道需要额外安装官方 App「**Cytoscape MCP Server**」。
**只支持 stdio 的宿主不必下载官方 `.mcpb`** —— `cyctl bridge` 就是零依赖的等价桥。
完整说明见 `docs/03`，平台覆盖见 `docs/05`。

---

## 前置条件

- **Java 17** —— Cytoscape 3.10+ 强制要求（不再支持 Java 8/11）。
  `cyctl discover` 会自动扫各平台常见 JDK 位置；官方 JRE 包**只覆盖 Windows/macOS**，
  Linux 请用发行版的 OpenJDK 17。
- **Cytoscape 3.10+** —— 三种来源任选：自带（`provision`，版本锁定）/
  系统安装（`--engine system`）/ 接管已运行实例（`--engine attach`）。
- **图形环境** —— Cytoscape Desktop 是 GUI 应用，官方**没有**无头开关。
  无显示器的 Linux 用 `cyctl start --headless`（自动套 `xvfb-run -a`）；
  找不到 Xvfb 时 `cyctl doctor` 会给出安装命令。
- **架构** —— 官方只为 macOS 提供 aarch64 产物；Windows/Linux 的 ARM64 走 `--engine system`。
  详见 `docs/05`。

---

## 当前状态（诚实版）

| 项 | 状态 |
|---|---|
| 逻辑层（命令解析、URL 映射、错误分类、代理绕过） | ✅ 已实现 |
| 版本锁定与官方哈希交叉校验 | ✅ 已实现并测试 |
| CLI 契约（**全路径** JSON 输出、退出码分级） | ✅ 已验证 |
| 归档解压安全（Zip/Tar Slip 防护） | ✅ 已实现，有恶意归档用例锁定 |
| 下载稳定性（原子落盘、长度校验、重试） | ✅ 已实现，有失败路径用例锁定 |
| provision 完整性策略（fail-closed + TOFU 基准） | ✅ 已实现并测试 |
| 九类宿主的 MCP 配置矩阵（字段名逐条对齐官方文档） | ✅ 已实现，49 个用例锁定 |
| 配置安全写入（合并/备份/幂等/**拒绝改写混合文件**） | ✅ 已实现并测试 |
| stdio ⇄ Streamable HTTP 桥（会话头、SSE、通知 202） | ✅ 已实现，有假服务端往返测试 |
| 跨平台引擎发现（Windows/macOS/Linux 多来源路径） | ✅ 已实现并测试 |
| 三种引擎来源（bundled / system / attach） | ✅ 已实现 |
| 无头启动（Linux 自动 xvfb-run 包装） | ✅ 已实现 |
| 全面体检 `cyctl doctor`（11 项检查 + 可执行修复建议） | ✅ 已实现 |
| **单元测试总数** | ✅ **188 个全绿**（`python -m unittest discover -s tests`；1 个跳过 —— Windows 无法读句柄数） |
| **与真实 Cytoscape 引擎的端到端联调** | ✅ **已完成**，自带引擎 3.10.5（= 锁定版本），`allAppsStarted: true` |
| **MCP 直连协议往返** | ✅ **已完成** —— `initialize` / `tools/list`（25 个工具）/ `tools/call` 只读调用；`tools/mcp_e2e.py` 可复跑 |
| **上游能力面逐项比对** | ✅ **已完成** —— 命令面 195/195、REST 面 171/171、MCP 25/25 全部可达；见 `docs/08` |
| **危险命令防护** | ✅ 三个访问层出口统一拦截，工作流不可绕过；9 个用例 + 4 个 CLI 变体实测 |
| **优雅停止** | ✅ `cyctl stop` 首次真正生效：`graceful:true`、2 秒关端口、无进程残留 |
| **二进制导出（PNG/PDF/SVG/CX）** | ✅ `rest --out` 字节通道；四类产物魔数校验通过。图像字节是否重复取决于完整视图状态，见 `docs/08` 后续边界补充 |
| 官方示例网络导入（`galFiltered.sif`） | ✅ **330 节点 / 359 边**，与官方示例一致 |
| 固定实验下的导出指纹 | ✅ 2026-10-01 样例三次 sha256 相同；**不构成任意活动视图的确定性保证**，见 `docs/08` |
| `workflows/demo.workflow.json` 实跑 | ✅ **5 步全绿**，产出含引擎版本与输入哈希的溯源清单 |
| **打包产物可独立运行** | ✅ `tools/package.py --verify` 解压到临时目录后再跑全量测试（当前复核结果见仓库 CI；历史 187 项快照见 `evidence/`） |

联调与稳定性验证见 `docs/07-验证与稳定性报告-2026-10-01.md`；
与官方上游的逐项比对审计见 `docs/08-上游对齐审计报告-2026-10-02.md`。

---

## 已知的坑（都已处理，记录备查）

1. **网上流传的 `-N` 无头参数不存在。** 核对官方源码，
   `headless-cmdline-parser-impl` 只注册了 `-h` / `-v` / `-c`。
2. **环境代理会劫持本机请求。** 实测设了 `http_proxy` 后，
   连 `localhost:1234` 都会被代理拦下返回 502。cyctl 已默认绕过代理，并有行为级回归测试锁定。
3. **命令→URL 映射的等号位置。** 官方正则是 `r' ([A-Za-z0-9_-]*=)'`——
   等号必须在**捕获组内部**。放外面会把 `=` 吃掉，造成"URL 正确但请求体错乱"的静默失败。
   已修正并被测试锁定。
4. **HTTP 200 不代表命令成功。** Cytoscape 会在 200 响应里返回 `errors` 字段。
   cyctl 会解析该字段并判为失败。
5. **归档解压的路径穿越（Zip Slip / Tar Slip）。** 原实现直接 `extractall()`。
   归档的条目名由归档自己控制，`../` 能写到目标目录之外；叠加「部分产物官方没给哈希」，
   等于在下载链路被劫持时获得任意文件写入能力。现已逐条校验条目，越界即拒绝，
   符号链接的指向也必须落在目标目录内。
6. **官方哈希只覆盖 4 个安装包。** `sha256sums` 里仅有 macOS dmg ×2 / `unix.sh` /
   `windows.exe`；`tar.gz`、`zip` 与官方 JRE 全都没有哈希。原实现对这类产物
   `verified: null` 却照样解压安装 —— 等于静默信任一个未校验的二进制。
   现改为 **fail-closed**：默认阻断（退出码 `5`），必须显式 `--allow-unverified` 放行；
   实测哈希会随结果一起返回，便于核对后回填锁定文件。
7. **`--force` 曾经在说谎。** 原实现遇到「目标已存在且未加 `--force`」时，
   只打印一句“已存在，`--force` 可强制重装”，**然后继续解压覆盖** —— 日志与行为相反，
   调用方会误判为「已跳过」。现改为真正的幂等：已存在即返回 `already_installed`。
8. **退出码 2 时 stdout 曾经是空的。** argparse 自行打印到 stderr 后 `sys.exit(2)`，
   绕过了 JSON 输出层。按契约解析 stdout 的调用方会直接崩溃。
   现已把 argparse 的报错与 `--version` 一并纳入 JSON 输出。
9. **下载中断会留下半截文件被复用。** 原实现直接写目标路径；中断后下次调用会把
   半截归档当作有效缓存，对无哈希产物更是直接解压。现改为写 `.part` + `os.replace`
   原子改名，中断只会留下可识别的临时文件。
10. **Python 3.14 的 tar 解压默认行为会变。** 3.12 起 `tarfile` 引入 `filter` 参数，
    未来默认值将由 `fully_trusted` 变为 `data`，会拒绝符号链接、改写元数据 ——
    解压结果会随解释器版本漂移。现已显式声明 `filter`，把确定性的决定权收回自己手里。
11. **各 MCP 宿主的字段名互不相同，写错不会报错。** VS Code 的顶层键是 `servers`（不是
    `mcpServers`）；Gemini CLI 用 `httpUrl`（它的 `url` 是已弃用的 SSE）；Windsurf 用
    `serverUrl`；Cursor 只认 `url`，而 Claude Code 只有 `url` 没 `type` 会被当成 stdio 服务器。
    宿主矩阵里每一项都注明官方文档来源，并有逐条断言锁定。
12. **自动改用户配置是危险动作。** `~/.claude.json`、`~/.codex/config.toml`、
    `~/.gemini/settings.json` 里除了 MCP 还装着宿主的其它全部设置 —— 整份重写等于赌博。
    现策略：默认只打印；`--write-config` 时才写；专用 MCP 配置文件才**合并**（保留其它条目 +
    生成 `.cyctl-bak` 备份）；混合配置文件一律**拒绝改写**。
13. **MCP 工具清单的事实源是 `tools/list`，不是 `/mcp/manifest`。** 实测 manifest 只文档化了
    4 个工具，而 `tools/list` 返回 **25 个**。照 manifest 写客户端会白白丢掉 21 个工具能力。
14. **`GET /commands/{ns}/{cmd}` 对无参命令就是「执行它」，不是「查看帮助」。** 实测
    `cyctl commands command quit` 直接把 Cytoscape 关掉了；更麻烦的是 GUI 模式下 JVM 会卡在
    退出确认对话框上不退出，留下一个占 1.3 GB 的僵尸进程，而端口已经关闭 —— 表现成
    「没有进程在监听，却还有进程吃内存」。现已默认拦截（指定命令名只做只读的存在性查询，
    要看参数清单须显式 `--allow-execute`，quit/exit 类**永久拒绝**）。关引擎请用 `cyctl stop`。
15. **`cyctl rest` 缺 Accept 协商会得到 406。** CyREST 的 help 类端点（`commands`、
    `commands/{ns}`）只接受 `Accept: text/plain`，发默认的 `application/json` 必然 406 ——
    而自带示例工作流的第一步正是 `rest GET commands`。现 `rest` 与工作流步骤都能声明 `accept`。
16. **工作流里的相对路径会解析到引擎的安装目录。** 执行期真正读文件的是 Cytoscape 引擎，
    它的 cwd 是自己的安装目录而不是你的项目目录，且报错信息里看不出是路径问题。
    现 inputs 的相对路径按**工作流文件所在目录**解析，命令字符串支持 `{input}` /
    `{workflow_dir}` / `{input0}` 占位符。
17. **空命令曾以退出码 1 结束。** `cyctl cmd ""` 抛出的 ValueError 没被捕获，一路落到兜底
    分支返回 1 —— 违反输出契约（只允许 0/2/3/4/5），按契约解析的调用方会当成未知灾难。
    现归为用法错误（`2`），并有专门用例锁定。
18. **★ 二进制导出曾被静默损坏（本轮最严重）。** `cyctl rest GET networks/…/views/….png`
    返回 `ok:true, http_status:200`，但 `result` 里 **28,798 个字符是 U+FFFD 替换字符**。
    根因是对响应体做了 `decode("utf-8", errors="replace")` —— 非 UTF-8 字节被替换掉且**不可逆**。
    这是「成功信封里装垃圾」：调用方看到 200 + ok:true，会以为图已导出。
    影响 `views/*.png`、`views/*.pdf`、`networks/*.cx`（`.svg`/`.csv` 是文本，不受影响）。
    现新增 `cyrest.cyrest_download()` 字节通道 + `cyctl rest --out`：`.part` 原子落盘、
    校验 `Content-Length` 防截断、默认 `Accept: */*`。
19. **`--help` 曾输出纯文本。** 退出码 0 但 stdout 不是 JSON，`json.loads` 直接炸。
    `error()` 与 `--version` 早就为同一原因 JSON 化了，help 是漏掉的一处。
    现覆写 `ArgumentParser.print_help()`，一处覆盖主解析器与全部子解析器。
20. **危险命令保护曾形同虚设（#14 的补漏）。** 上一轮只拦了 `cyctl commands`；
    实测 `cyctl cmd "command quit"`、`cyctl rest GET commands/command/quit` 以及工作流步骤
    **都能绕过去**。现把判定收到访问层的三个出口（`run_command` / `call_operation` /
    `cyrest_download`），退出码 2。**不误伤**：只认命令名的第二个词，
    `network set attribute name="halt"` 正常放行（已实测）。
21. **★ `cyctl stop` 的优雅退出从来没生效过。** 一直报 `graceful:false` 靠杀进程收场。
    根因：优雅退出发的是 **POST** `/commands/command/quit`，却带了 `Accept: text/plain` ——
    实测 POST + `text/plain` 得 **406**，而 POST + `application/json` 得 200。
    只有 **GET** 的 help 端点要 `text/plain`。原来的 except 分支把所有异常都写成
    「进程关闭会中断连接，这不代表退出失败」，一个**持续存在**的 406 被这句话盖住了。
    现改为 `application/json`，并把 **4xx 与「连接中断」分开**。修复后首次出现
    `graceful:true, http_status:200, port_closed_after_seconds:2.0`，且无 java 残留。
22. **`commands --allow-execute` 的 `executed` 标志曾误报。** `cyctl commands network export
    --allow-execute` 只拿到一份**参数清单**，却报 `executed: true` —— 调用方会以为导出已完成。
    现按回应标题区分：`Available arguments for …` ⇒ `executed:false, mode:"help"`。
23. **工作流输入路径未规范化导致同一文件变成两个网络。** 写 `../data/x.sif` 时会原样交给
    引擎，引擎据此命名 —— 实测 `networks.names` 里同时出现两种写法。而输入路径是溯源清单的
    关键字段。现统一 `resolve()` 成绝对路径。
24. **含空格的 operation 曾以退出码 1 结束。** `cyctl rest GET "commands/network/get preferred"`
    让 urllib 抛 `http.client.InvalidURL`，该异常既不是 `OSError` 也没被原先捕获，
    一路穿透到兜底分支。而 `commands/{ns}/{cmd}` 正是这类端点。
    现新增 `_encode_operation()`（只编码路径、保留已写的 `%XX`、查询串原样透传），
    并把 `InvalidURL`/`ValueError` 归类为用法错误 → 退出码 `2`。

---

## 许可与合规

- 本技能包**只驱动** Cytoscape，不修改其源码。Cytoscape 引擎本身为
  **LGPL-2.1**（`cytoscape-impl` 仓库声明）。
- 官方 MCP 服务器 `cytoscape-desktop-mcp` 为 **BSD-3-Clause**。
- 分发本技能包时，请遵守上述许可；`assets/versions.lock.json` 中记录的下载产物来自官方发布渠道。
- **医学用途提醒**：使用前请确认你的验证方案（见 `docs/02` §3 检查清单）。
  本技能包保证"跑的是原版引擎"，但**不替代**你对自己数据与结论的复核责任。
