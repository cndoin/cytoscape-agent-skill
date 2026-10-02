# Connecting an Agent to the Cytoscape MCP Server

**MCP URL:** `http://localhost:{rest.port}/mcp`

The commands below show the port as `{rest.port}` — use Cytoscape's actual CyREST port, which is **1234** unless you changed it under **Edit > Preferences > REST API**.

Cytoscape must be running with this app installed. The app will run an mcp server that runs under the existing Cyrest port.

Multiple agents can connect simultaneously. Cytoscape is a single-user application — concurrent agents may issue conflicting commands.

If your agent is not listed below, consult its documentation for configuring an MCP server with Streamable HTTP transport.

## Which option should I use?

There are two ways to connect, and they are not equivalent. Prefer the first.

| Your agent | Use this |
|----------|----------|
| Anything that supports Streamable HTTP — Claude Code, GitHub Copilot, Codex CLI, and most others | The MCP URL above, configured directly. Nothing to install, no extra process. |
| Claude Desktop | The `Cytoscape MCP` extension (`.mcpb`), which bundles a small stdio-to-HTTP bridge. Desktop extensions speak stdio, so the bridge is required there. |

Both end up talking to the same MCP server inside Cytoscape. If your agent can take a URL, give it the URL.

---

## Claude Desktop

**Prerequisite:** Go to **Settings > Extensions > Advanced** and enable
**Use Built-in Node.js for MCP**. This is required for the extension to function.

Download the **Cytoscape MCP** extension from the
[releases page](https://github.com/cytoscape/cytoscape-desktop-mcp/releases/)
(`cytoscape-mcp.mcpb`). In Claude Desktop go to **Settings > Extensions**, click
**Install Extension**, and select the downloaded `cytoscape-mcp.mcpb` file.

To verify: the **Cytoscape MCP** connector will appear in **Customize > Connectors**. 
This screen will display the CyREST port as a config defaulted to 1234, change that if you have changed the CyRest port on Desktop as
the mcp server is hosted on CyRest.

---

## Claude Code

```
claude mcp add --transport http cytoscape-mcp http://localhost:{rest.port}/mcp
```

To verify:

```
claude mcp list
```

---

## GitHub Copilot (VS Code)

**Option A — Command Palette:** Open Command Palette (Cmd+Shift+P / Ctrl+Shift+P), run **MCP: Add Server**, choose HTTP, enter the URL below, name it `cytoscape-mcp`.

```
http://localhost:{rest.port}/mcp
```

**Option B — CLI:**

```
code --add-mcp '{"name":"cytoscape-mcp","type":"http","url":"http://localhost:{rest.port}/mcp"}'
```

---

## GitHub Copilot CLI

```
copilot mcp add --transport http cytoscape-mcp http://localhost:{rest.port}/mcp
```

To verify: 
```
copilot mcp list
```

---

## OpenAI Codex CLI

```
codex mcp add cytoscape-mcp --http-url http://localhost:{rest.port}/mcp
```

To verify: run `codex mcp list` or type `/mcp` inside the Codex TUI.

---

## Installing from the MCP Registry

This server is published to the official MCP Registry as:

```text
io.github.cytoscape/cytoscape-desktop-mcp-bridge
```

If your agent or an MCP marketplace installs from the registry, it will find it there and handle the install for you.

Cytoscape Desktop must already be running with this app installed. A registry install only sets up the client side — with no Cytoscape behind it, there is nothing to connect to.

---

## Diagnostics

To interrogate the MCP server direct, use browser and go to http://localhost:{rest.port}/mcp/manifest.
The MCP server exposes a `/mcp/manifest` endpoint to provide a lightweight, human-readable catalog of every tool, prompt, resource, and resource template registered   on the server, formatted as Markdown. 

