# Install ProUse for a user

This is the authoritative procedure for a local coding agent. Work only on the user's
machine, preserve existing `PROUSE_HOME` configuration, and report what you actually
verified. A request to install ProUse authorizes its local prerequisites and workspace
setup; complete those steps without asking again. Editing the user's MCP client
configuration and restarting the client remain the user's decisions unless they asked
you to do them.

Use the user's current project as the workspace. Record its absolute path **before**
changing into a ProUse checkout; do not register the installer checkout by accident.
Use the official installer below. If the user explicitly supplied a local ProUse
checkout, use that checkout instead.

## Install or update

The installer supports macOS and Linux. It bootstraps `uv` from
[Astral's official installer](https://docs.astral.sh/uv/reference/installer/) when
missing and lets uv supply Python 3.13. `PROUSE_PYTHON` can override the interpreter;
manual installations require Python 3.11+. No sudo or virtualenv activation is needed.

```bash
curl -fsSL https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/install.sh -o /tmp/prouse-install.sh
sh /tmp/prouse-install.sh
```

For an explicitly supplied local checkout, run `sh install.sh --source .` from that
checkout instead.

The installer uses `uv tool install --force --reinstall`, so rerunning it updates even
when the package version has not changed. It does not edit `~/.prouse` or any MCP
client configuration. It reports the installed executable's absolute path and updates
future shells if its bin directory is missing from PATH. Use that absolute path for
the following commands when the current PATH has not refreshed. `--no-modify-path`
disables shell changes. Do not substitute a PyPI package name or a different source if
the official installer is unavailable; report that error.

ProUse uses ripgrep for `grep` and `find` when it is installed and falls back to a
slower built-in search otherwise. Install ripgrep with the system package manager if
the user agrees (`brew install ripgrep` on macOS).

## Register the project

Quote paths containing spaces.

```bash
prouse setup --workspace "/absolute/path/to/project" --no-input --json
```

`PROUSE_HOME` defaults to `~/.prouse`; set it to an absolute isolated directory for
tests or a separate instance. Setup is repeatable: existing workspaces and settings are
kept, and registering the same folder again returns its existing id. The first
workspace becomes the default; add `--default` to make this project the default when
others are already registered. Exit 0 with `status: configured` means the registry was
written and validated. Exit 2 means the input was missing or invalid (for example the
home folder, or a path that does not exist).

## Verify

```bash
prouse doctor --json
prouse mcp check --json
prouse mcp config
```

`doctor` exits 0 when the installation is usable, possibly with warnings (such as
ripgrep missing), and 1 for a fatal configuration error. `mcp check` starts a temporary
stdio server exactly as an MCP client would, performs `initialize` and `tools/list`,
calls `workspaces` and lists the default workspace with `ls`, then stops the server.
It exits 0 only when all of that succeeds. A success reports
`client_connection: not_verified`, because no local check can prove that the user's
chat app has connected.

## MCP client handoff

`prouse mcp config` prints the configuration for this installation and instance:

```json
{
  "mcpServers": {
    "prouse": {
      "command": "/absolute/path/to/prouse",
      "args": ["mcp", "serve"],
      "env": {"PROUSE_HOME": "/absolute/path/to/prouse-home"}
    }
  }
}
```

Merge the `prouse` entry into the client's existing `mcpServers` map and keep every
other entry. For Claude Desktop on macOS the file is
`~/Library/Application Support/Claude/claude_desktop_config.json`; back it up before
editing, and fully quit and reopen Claude Desktop afterwards. Never run
`prouse mcp serve` yourself in the background; its stdin and stdout belong to the
client that starts it.

ProUse speaks MCP over stdio only, so it works with desktop clients that start local
MCP servers. Web and mobile chat apps need a remote MCP endpoint with authentication,
which ProUse does not provide; do not tunnel the stdio server to the Internet, because
the `bash` tool runs commands as the user.

## Optional dashboard

The MCP server does not need it. If the user wants a local page for managing
workspaces:

```bash
prouse start --json
prouse status --json
```

`prouse start` starts a detached process and returns after its health check passes;
running it again returns the existing instance. `status` exits 0 when the dashboard
answers its health check and 3 when it is stopped. `prouse stop` stops it, and
`prouse service install` starts it at login (launchd on macOS, systemd user units on
Linux). It binds to `127.0.0.1:8848`; do not bind it to another address unless the user
asks, because it has no login.

## Receipt to the user

Report the installed `prouse --version`, executable path, `PROUSE_HOME`, workspace id,
the `doctor` and `mcp check` results with any warnings, and the exact output of
`prouse mcp config` with where it goes. State the client connection as `not_verified`
until the user's chat app has called a ProUse tool. If `PROUSE_HOME` was customized,
prefix follow-up commands with the same value. Keep the handoff short, and give a
concrete next step for any failed check instead of reporting the installation as
complete.
