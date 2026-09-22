# Install ProUse for a user

This is the authoritative procedure for a local coding agent. Work only on the user's
machine, preserve existing `PROUSE_HOME` configuration, and report what you actually
verified. A request to install ProUse authorizes its local prerequisites, workspace
setup, and background Admin process. Complete those steps without asking again.
Browser sign-in and client authorization remain the user's actions.

Use the user's current project as the workspace. Record its absolute path **before**
changing into a ProUse checkout; do not accidentally register the installer checkout.
If the user supplied a checkout, branch, or commit, use it. When this guide came from
a preview URL, install that same ref. Otherwise use the official `main` branch.

## Install or update

The installer supports macOS and Linux. It bootstraps `uv` from
[Astral's official installer](https://docs.astral.sh/uv/reference/installer/) when
missing and lets uv supply Python 3.13. `PROUSE_PYTHON` can override the interpreter;
manual installations require Python 3.11+. No sudo or virtualenv activation is needed.

From an existing ProUse checkout, install the isolated user command with:

```bash
sh install.sh --source .
```

Without a checkout, fetch the installer from the selected official ref and install
that same ref. This example uses `main`; replace **both** occurrences for a preview:

```bash
curl -fsSL https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/install.sh -o /tmp/prouse-install.sh
sh /tmp/prouse-install.sh --ref main
```

The installer uses `uv tool install --force --reinstall`, so rerunning it updates even
when the package version has not changed. It does not edit `~/.prouse`. It reports the
installed executable's absolute path and updates future shells if its bin directory
is missing from PATH. Use that absolute path for all following commands when the
agent's current PATH has not refreshed. `--no-modify-path` disables shell changes.
A source checkout is not needed after installation. Do not substitute a PyPI package
name or a different source if the selected ref is unavailable; report that error.

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

On a remote development host where the user expects LAN access, add `--host 0.0.0.0`
to setup. The CLI adds the detected LAN address to the allowed hosts. Preserve a
previous host/port setting unless the task calls for changing it.

## Start and verify

`prouse admin` starts a detached process and returns only after its health check passes.
It survives the agent's terminal ending. Running it again returns the existing instance.

```bash
prouse admin --json
prouse status --json
prouse doctor --json
prouse mcp check --workspace-id WORKSPACE_ID_FROM_SETUP --json
prouse mcp config
```

The MCP check starts a temporary client-owned stdio server, performs `initialize`,
`tools/list`, `list_workspaces`, and a scoped `list_directory` read, then shuts that
temporary server down. It exits 0 only when those steps succeed; failure or timeout
exits 1. It does not authorize or connect an external client. Check the requested
workspace ID from setup instead of relying on a different registered workspace.

Fetch the reported dashboard URL and `/healthz` before handing it to the user.
On a remote host, verify and report the actual LAN IP and port, not `0.0.0.0` or
localhost. Also keep any diagnostic warnings in the final handoff.

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
`prouse restart --background` after correcting configuration or updating the package. Stop the
owned instance with `prouse stop`; it never uses a generic process kill.

For an attached terminal instead, `prouse start` (or `prouse admin --foreground`)
runs in the foreground and Ctrl-C stops it. Do not use that mode for a one-shot agent
installation that needs to return while keeping Admin running.

For login autostart, use `prouse service install`, `status`, or `uninstall`. This creates
an instance-specific macOS launchd or Linux systemd-user unit. `prouse stop` stops the
current process; service `uninstall` also disables future autostart. Windows login
services are not currently supported.

Admin binds to `127.0.0.1` by default. Binding `--host 0.0.0.0` exposes the unauthenticated
operator dashboard to the LAN and should be an explicit, trusted-network decision; report
the actual reachable LAN URL when that mode is requested.

## MCP client handoff

Run `prouse mcp config` to generate the configuration for the installed executable
and selected instance. Merge the `prouse` entry into the client's existing
`mcpServers` map; preserve unrelated entries. Its shape is:

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

Do not detach `prouse mcp serve`; stdin/stdout belong to the client. Verify an actual
MCP `initialize`, `tools/list`, and a workspace read with `prouse mcp check` before
reporting local handshake success. An external client's connection remains unverified
until that client exercises the tools.

ChatGPT does not connect to the local Admin URL and a healthy dashboard does not imply a
ChatGPT connection. Follow OpenAI's current [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt/)
instructions: enable developer mode, then use either a public HTTPS MCP endpoint or the
documented [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
The user must complete any browser sign-in, connection selection, and authorization.
ProUse does not claim that handoff succeeded until ChatGPT shows and exercises the MCP
connection.

## Receipt to the user

Report the installed `prouse --version`, executable path, `PROUSE_HOME`, workspace ID,
dashboard URL(s), setup/status/doctor results, MCP handshake result (or
`not_tested`), ChatGPT connection state (`not_verified` until user authorization), and
the `prouse stop` / `prouse restart --background` commands. Include the exact generated
MCP config. If `PROUSE_HOME` was customized, prefix follow-up commands with that same
value so they target the right instance. Keep the handoff short; give a concrete next
step for any failed check instead of reporting the installation as fully complete.
