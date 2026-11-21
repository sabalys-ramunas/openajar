# openajar

**A computer for your agent**: a cloud VM with a terminal, a real browser and your repo, saved when
idle and back in the next chat. For Claude Code, Codex, Cursor, Claude Desktop or any MCP client.

This package is a small launcher for MCP clients that start servers with `npx`. It runs the openajar
CLI installed on your computer as a stdio MCP server (`openajar mcp`). It has no dependencies.

## Install openajar first

openajar runs on macOS and Linux. Install it, then let setup walk you through the rest (VMs on
Hetzner Cloud, or Docker on your computer; GitHub; Claude Code and Codex):

```sh
curl -fsSL https://openajar.com/install.sh | sh
openajar setup
```

## Add it to your MCP client

```json
{
  "mcpServers": {
    "openajar": { "command": "npx", "args": ["-y", "openajar"] }
  }
}
```

`npx -y openajar` with no arguments runs `openajar mcp` from the installed CLI (the `openajar` on your
PATH, else `~/.local/bin/openajar`), passes stdio through and gives its exit code back. `openajar mcp`
starts openajar's local service when it is stopped.

If openajar is not installed yet, the launcher still answers your client: it serves a minimal MCP
server whose one tool and instructions tell the agent what to install, and it says the same on stderr.
Nothing hangs, and nothing is installed without you.

`npx openajar <arguments>` runs `openajar <arguments>`, for example `npx openajar local status`.

## Links

- Website: https://openajar.com
- Source, docs and issues: https://github.com/sabalys-ramunas/openajar
- In the MCP Registry as `com.openajar/openajar`

Apache-2.0
