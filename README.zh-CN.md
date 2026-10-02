# Cytoscape Agent Skill（简体中文）

**让 AI Agent 驱动原版 Cytoscape Desktop 引擎。** 分析与可视化仍由 Cytoscape 完成；Agent 负责选择官方命令。本项目不重写网络算法，也不使用 NetworkX/igraph 替代 Cytoscape 计算。

[English](README.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md) · [العربية](README.ar.md) · [हिन्दी](README.hi.md)

## 功能

- 通过 CyREST 和 Commands API 导入/导出网络、表格、会话及可视资源。
- 先查询实际命令及参数，再执行；失败的工作流步骤会立即停止。
- 通过 MCP Streamable HTTP 或内置 stdio 桥连接 AI 客户端；可生成九类宿主配置。
- JSON 工作流可生成溯源清单，记录引擎版本、输入 SHA-256、请求与结果指纹。
- 提供环境体检、引擎发现、生命周期管理和安全配置写入。

## 快速开始

需要 Python 3.8+、Cytoscape Desktop 3.10+、Java 17+。Python 部分零第三方依赖。

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill/cytoscape-agent-skill
python scripts/cyctl.py doctor
python scripts/cyctl.py discover
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py commands network
```

如需下载锁定版本引擎，请先阅读 [`INSTALL.md`](INSTALL.md) 和 `docs/03-跨Agent接入指南.md`。下载或运行安装器会修改本机环境，应先审阅来源、校验状态和目标版本。

## 连接 Agent

```bash
python scripts/cyctl.py mcp --host codex
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

第一条仅预览配置；第二条会明确写入项目配置。混合配置文件会被拒绝自动改写。Claude Desktop 等 stdio 宿主可用 `python scripts/cyctl.py bridge --port 1234`。详见 `docs/03-跨Agent接入指南.md`。

## 安全与边界

- 力导向等布局可能含随机性；溯源清单不等于坐标确定性。
- 使用 system/attach 模式时必须记录真实引擎版本。
- PNG/PDF/SVG/CX 导出必须使用 `rest ... --out FILE`，确保二进制字节不被破坏。
- 不可校验的下载默认阻断；危险命令受保护；工作流任何一步失败都会停止。

## 验证与文档

```bash
python -m unittest discover -s tests -v
python tools/package.py --verify
```

真实引擎测试需要 Cytoscape 和对应 App。`docs/07`、`docs/08` 是有日期的实测记录，其数值代表当时快照，不是对所有机器的永久保证。入口说明见 [`SKILL.md`](cytoscape-agent-skill/SKILL.md) 与 [`AGENTS.md`](cytoscape-agent-skill/AGENTS.md)。

## AI 安装

将此仓库交给你的编程 Agent，并要求先读取 `AGENTS.md` 与 `SKILL.md`、运行 `doctor`/`discover` 并汇报拟议改动。**未经你对具体操作的确认，不应安装软件、下载大型引擎、运行安装器或修改全局 Agent 配置。**完整提示词见 [`INSTALL.md`](INSTALL.md)。

代码与文档采用 LGPL-2.1，详见 [`LICENSE`](LICENSE)。Cytoscape Desktop 与各 App 是独立上游软件，不包含在本仓库中。欢迎提交 Issue 与 PR，参见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。
