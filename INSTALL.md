# Install with an AI agent

This guide helps an agent set up the **skill** and connect it to a local Cytoscape installation. It does not authorize the agent to install system software or change global client settings without your approval.

## 1. Download the project

Clone this repository or download the source archive.

```bash
git clone https://github.com/cndoin/cytoscape-agent-skill.git
cd cytoscape-agent-skill
```

The reusable skill folder is `cytoscape-agent-skill/` inside this repository. Copy that entire folder, including `SKILL.md`, `AGENTS.md`, `scripts/`, `docs/`, and `assets/`, to the directory used by your agent.

### Codex

For a personal installation, copy the skill to `$CODEX_HOME/skills/cytoscape` (normally `~/.codex/skills/cytoscape`). For a project installation, place it under `.agents/skills/cytoscape/` in your project. Codex reads `AGENTS.md` from the project context; open/work from the skill folder when you want those instructions applied.

macOS/Linux, from the cloned repository root:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/cytoscape"
cp -R cytoscape-agent-skill/. "${CODEX_HOME:-$HOME/.codex}/skills/cytoscape/"
```

Windows PowerShell:

```powershell
$target = Join-Path $HOME '.codex\skills\cytoscape'
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -Path '.\cytoscape-agent-skill\*' -Destination $target -Recurse -Force
```

### Claude Code

For personal use, copy the folder to `~/.claude/skills/cytoscape/`. For one project, use `<project>/.claude/skills/cytoscape/`. Restart or reload Claude Code if the skill does not appear.

### VS Code / GitHub Copilot

For a repository skill, use `<project>/.github/skills/cytoscape/` or `<project>/.agents/skills/cytoscape/`. For a personal skill, VS Code documents `~/.copilot/skills/`, `~/.claude/skills/`, and `~/.agents/skills/` as supported locations. Check the [VS Code Agent Skills guide](https://code.visualstudio.com/docs/agent-customization/agent-skills) for current locations and settings.

Other clients can read `SKILL.md` directly and run the CLI. Skill discovery differs by host; verify that the client lists `cytoscape` after reloading it.

## 2. Ask the agent to inspect first

Paste this prompt:

```text
Read AGENTS.md and SKILL.md completely. First run `python scripts/cyctl.py doctor` and `python scripts/cyctl.py discover`. Report prerequisites, detected Cytoscape/Java versions, and any proposed downloads or configuration edits. Do not install software, download large engine files, run installers, or edit user/global agent configuration unless I approve the exact action. Use Cytoscape's documented commands; never substitute another graph library for engine calculations.
```

## 3. Set up the engine

Use an existing Cytoscape Desktop installation when possible:

```bash
python scripts/cyctl.py start --engine system
python scripts/cyctl.py wait
python scripts/cyctl.py doctor
```

Otherwise, inspect `doctor`'s fix guidance and `docs/03-跨Agent接入指南.md`. Provisioning downloads software and may run an installer; review version, source, digest status, disk requirements, and platform support before approving. Do not bypass an integrity failure casually.

## 4. Connect MCP

The default is preview only:

```bash
python scripts/cyctl.py mcp --host codex
```

Review the generated configuration. For a dedicated project config, explicitly write it:

```bash
python scripts/cyctl.py mcp --write-config --only-dedicated --project . --scope project
```

Restart/reload the agent client as it requires, then ask it to list Cytoscape tools. Mixed configuration files are not automatically rewritten. Exact host instructions and stdio bridge setup are in `docs/03-跨Agent接入指南.md`.

## Troubleshooting

- `doctor` exits 3: inspect `blocking_ids` and follow the per-check `fix` guidance.
- CyREST unavailable: ensure Cytoscape is running and check the configured port.
- Version mismatch: report the actual version when using `system` or `attach` mode.
- Random layout differs: layout coordinates can be stochastic; see `docs/02-功能覆盖与确定性边界.md`.
- Binary output: use `rest ... --out FILE` for PNG, PDF, SVG, or CX.
