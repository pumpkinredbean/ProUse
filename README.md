# ProUse

**Use Pro models across your local workspaces.**

ProUse gives an AI chat app the tools of a coding agent on your own computer. It is a
local MCP server with the small tool set of the [pi](https://github.com/earendil-works/pi)
coding agent: `read`, `write`, `edit`, `bash`, `grep`, `find` and `ls`, scoped to the
project folders you register. Connect it to Claude Desktop or another MCP client, and
the model in your chat can read your code, change it, and run your builds and tests
directly, with no second agent in between.

## Get started

Run these commands from your project directory:

```bash
curl -fsSL https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/install.sh | sh
prouse setup --workspace .
prouse mcp config
```

Add the printed `prouse` entry to your client's MCP configuration and restart the
client. For Claude Desktop the file is
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS; keep any
other servers already listed under `mcpServers`. Run `prouse mcp check` to verify that
the server starts, lists its tools and reads the workspace.

The installer sets up `uv` and Python 3.13 when needed, installs an isolated user
command, and adds it to future shells. If `prouse` is not found, reopen your terminal
or use the executable path printed by the installer. Configuration lives under
`~/.prouse` (or `PROUSE_HOME`) and survives reinstallation. To update, rerun the
installer and restart your MCP client.

### Or ask your coding agent

```text
Install and set up ProUse for this project by following:
https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/docs/install-for-agents.md

Install missing prerequisites, preserve my existing settings, register the current
project, and verify the MCP handshake with a workspace read. Give me the MCP client
configuration and tell me where to put it.
```

## Tools

| Tool | What it does |
| --- | --- |
| `workspaces` | Lists the registered folders and loads a project's `AGENTS.md` / `CLAUDE.md` instructions. |
| `read` | Reads a text file (2000 lines or 50KB per call, continue with `offset`) or attaches an image. |
| `write` | Creates or overwrites a file, creating parent folders. |
| `edit` | Replaces exact text; several `edits[]` per call, matched against the original file, tolerant of whitespace and quote differences. |
| `bash` | Runs a command in a workspace and returns the last 2000 lines or 50KB of output; the full output is saved to a file the model can read. |
| `bash_job` | Waits for, or stops, a command that is still running in the background. |
| `grep` | Searches file contents with ripgrep, respecting `.gitignore`. |
| `find` | Finds files by glob, respecting `.gitignore`. |
| `ls` | Lists a folder, including dotfiles. |

Paths can be absolute, start with `~`, or be relative to the default workspace. The file
tools refuse anything outside the registered folders, after resolving symlinks.

Chat apps cancel long tool calls (Claude Desktop after about a minute), so a command
that is still running after 45 seconds keeps running as a background job. `bash`
returns the output so far with the job number, and `bash_job` picks it up from there.
Commands run with your login shell's environment, so tools installed with Homebrew,
nvm, pyenv and the like are found even though the chat app starts ProUse with a
minimal `PATH`.

See [MCP tools](docs/mcp-tools.md) for the exact contracts.

## Workspaces

```bash
prouse workspace list
prouse workspace add ~/code/another-project
prouse workspace default another-project
prouse workspace remove another-project   # unregisters it; files are not touched
```

Changes apply on the next tool call; the MCP client does not need a restart. The
model should call `workspaces` first, then `workspaces name=<id>` to read that
project's instructions, the same `AGENTS.md` files pi and other coding agents load.

### Dashboard

`prouse start` opens an optional local web page at `http://127.0.0.1:8848/` for the
same workspace management (add with a folder picker, rename, disable, set the default,
remove), the MCP client configuration to copy, and a connection check. It runs in the
background until `prouse stop`.

```bash
prouse start
prouse status --json
prouse logs -f
prouse stop
```

`prouse service install` starts the dashboard at login on macOS (launchd) or Linux
(systemd user service). The MCP server itself does not need the dashboard: the MCP
client starts `prouse mcp serve` on its own.

## Safety model

ProUse is privileged developer tooling. A chat connected to it can do what you can do
in a terminal.

- `bash` runs commands as you, with your environment, network and credentials. It is
  not sandboxed and not confined to the workspace; `cwd` only picks where it starts.
- `read`, `write`, `edit`, `grep`, `find` and `ls` only reach the registered folders.
  Your home folder and the filesystem root cannot be registered.
- Your MCP client decides which tool calls need your approval. Claude Desktop asks
  before each tool unless you allow it.
- The dashboard binds to `127.0.0.1`, has no login, and accepts only same-origin
  requests carrying a per-process token.

Register project folders only, and read [SECURITY.md](SECURITY.md) and the
[security model](docs/security-model.md) before exposing anything beyond localhost.

## Upgrading from 0.1

The tools changed. Codex delegation, direct `argv` execution, context policies and
SHA-256 write preconditions are gone; `bash`, `edit` and the other pi-style tools
replace them. Codex is no longer needed. Your existing registry keeps working: worker
profiles and context policies in it are ignored, and it is rewritten in the version 2
format the next time a workspace changes. The MCP client configuration is unchanged;
restart the client after updating so it sees the new tool list.

## Documentation

- [MCP tools](docs/mcp-tools.md)
- [Configuration](docs/configuration.md)
- [Install for agents](docs/install-for-agents.md)
- [Security model](docs/security-model.md)
- [CLI design](docs/cli-design.md)
- [Roadmap](ROADMAP.md)

## Repository layout

```text
prouse/server.py             MCP server and tool definitions
prouse/tools/                read/write/ls, edit, grep/find, bash and output truncation
prouse/workspaces.py         registered folders, path resolution, project instructions
prouse/configuration.py      registry and dashboard settings
prouse/commands/             CLI commands
prouse/runtime/              dashboard server and its background process
prouse/integrations/         MCP client config/check, login services
prouse_assets/admin_ui/      dashboard page
tests/                       regression tests
```

## Tests

```bash
python -m compileall -q prouse tests
python -m unittest discover -s tests -p 'test_*.py'
python scripts/release_check.py
```

CI runs the same validation on Python 3.11, 3.12 and 3.13.

## License

MIT. See [LICENSE](LICENSE).
