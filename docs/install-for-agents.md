# Install ProUse for a user

This is the authoritative procedure for a local coding agent. Work only on the user's
machine, preserve existing `PROUSE_HOME` configuration, and report what you actually
verified. Do not publish, deploy, or create a ChatGPT connection without the user's
explicit participation.

## Install or update

Python 3.11+ and `uv` are supported on macOS and Linux. From a checked-out candidate,
install or update the isolated user command with:

```bash
sh install.sh --source .
```

For the official repository after this installer is present on the selected ref:

```bash
sh install.sh --source https://github.com/pumpkinredbean/ProUse.git --ref main
```

The installer uses `uv tool install --force`, does not edit `~/.prouse`, and reports the
tool bin directory if `prouse` is not on `PATH`. A source checkout is not needed after
installation. Do not substitute a PyPI package name or release URL unless one has
actually been published.

## Configure without prompts

Choose an existing explicit project directory. Quote paths containing spaces.

```bash
prouse setup --workspace "/absolute/path/to/project" --no-input --json
```

`PROUSE_HOME` defaults to `~/.prouse`; set it to an absolute isolated directory for
tests or a separate instance. Setup is repeatable: an already registered workspace and
all existing profiles, workspaces, settings, policies, receipts, and state are kept.
Exit 0 with `status: configured` means the registry was validated. Exit 2 means required
noninteractive input was missing or invalid; exit 1 means configuration could not be
validated.

## Start and verify

`prouse start` runs the Admin server in the foreground. Keep that process attached to a
terminal or install a login service after the user asks for autostart.

```bash
prouse start
# In another terminal:
prouse status --json
prouse doctor --json
```

`status` exits 0 only when the owned Admin process answers its health check and exits 3
when stopped. Its fields are deliberately separate:

- `local_installation`: the installed command and version are ready;
- `configured`: local registry exists;
- `running` / `admin_ready`: the Admin lifecycle and HTTP health check;
- `mcp.handshake`: stdio MCP is client-owned and is not inferred from Admin health;
- `client_connection`: connection/authorization cannot be inferred locally.

`doctor` treats a missing Codex executable as a warning: local context and Admin remain
usable, while delegated workers/direct Codex execution do not. It exits 0 when the
installation/configuration is usable (warnings may remain) and 1 for a fatal local
configuration error. Retry with `prouse logs` and `prouse doctor --json`. Use
`prouse restart` after correcting configuration. Stop the
owned instance with `prouse stop`; it never uses a generic process kill.

For login autostart, use `prouse service install`, `status`, or `uninstall`. This creates
an instance-specific macOS launchd or Linux systemd-user unit. `prouse stop` stops the
current process; service `uninstall` also disables future autostart. Windows login
services are not currently supported.

Admin binds to `127.0.0.1` by default. Binding `--host 0.0.0.0` exposes the unauthenticated
operator dashboard to the LAN and should be an explicit, trusted-network decision; report
the actual reachable LAN URL when that mode is requested.

## MCP client handoff

Local MCP clients should own this stdio process:

```json
{
  "command": "/absolute/path/from-command-v/prouse",
  "args": ["mcp", "serve"]
}
```

Do not detach `prouse mcp serve`; stdin/stdout belong to the client. Verify an actual
MCP `initialize`, `tools/list`, and a workspace read before reporting handshake success.

ChatGPT does not connect to the local Admin URL and a healthy dashboard does not imply a
ChatGPT connection. Follow OpenAI's current [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt/)
instructions: enable developer mode, then use either a public HTTPS MCP endpoint or the
documented [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
The user must complete any browser sign-in, connection selection, and authorization.
ProUse does not claim that handoff succeeded until ChatGPT shows and exercises the MCP
connection.

## Receipt to the user

Report the installed `prouse --version`, executable path, `PROUSE_HOME`, workspace ID,
dashboard URL(s), exact setup/status/doctor results, MCP handshake result (or
`not_tested`), ChatGPT connection state (`not_verified` until user authorization), and
the `prouse stop` / `prouse restart` commands. Never turn Admin health into a connection
claim.
