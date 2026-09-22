# ProUse

**Use Pro models across your local workspaces.**

ProUse is an open-source local MCP control layer for giving an AI conversation
explicit, inspectable access to approved project workspaces. It keeps source
inspection, bounded file mutation, delegated Codex work, and direct host command
execution behind one workspace registry with durable receipts.

ProUse is for developers who want a strong interactive model to remain the
research and orchestration layer while local tools perform mechanical work.

## Get started

### Paste into your coding agent

Copy this prompt into a local coding agent with terminal access:

```text
Install ProUse for this project. Read and follow:
https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/docs/install-for-agents.md

Install missing prerequisites, preserve my existing settings, register the current
project, and start the Admin dashboard in the background. Verify the dashboard and
the MCP handshake with a workspace read. Give me the dashboard URL, MCP client
configuration, and the stop/restart commands. Complete the local setup; ask me only
for sign-in or connection approval that requires my participation.
```

**Trying a preview?** The `main` URL works after this change is merged. Until then,
replace `main` in the prompt URL with `codex/cli-onboarding` and add
`Install that same ref: codex/cli-onboarding.` If your agent already has this checkout,
ask it to follow [the local installation guide](docs/install-for-agents.md).

### Or use your terminal

From this checkout:

```bash
sh install.sh --source .
prouse setup --workspace "/absolute/path/to/project"
prouse admin
```

`prouse admin` starts in the background, checks that the server is ready, and prints
the dashboard URL. No virtualenv activation or terminal left running is needed.

For a published Git ref, download its installer and install that same ref:

```bash
curl -fsSL https://raw.githubusercontent.com/pumpkinredbean/ProUse/main/install.sh -o /tmp/prouse-install.sh
sh /tmp/prouse-install.sh --ref main
```

For the preview, replace **both** occurrences of `main` with `codex/cli-onboarding`.
The installer sets up `uv` and Python 3.13 when needed, installs an isolated user
command, and adds its bin directory to future shells. If the current shell does not
find `prouse`, use the absolute executable path printed by the installer or reopen
the terminal. Use `--no-modify-path` to manage PATH yourself.

Configuration, policies, receipts, and logs live under `~/.prouse` (or `PROUSE_HOME`)
and survive reinstallation. To update, rerun the installer and
`prouse restart --background`.

## What it provides

- **Multiple approved workspaces** with stable IDs instead of arbitrary filesystem roots.
- **Code-oriented context tools**: directory listing, globbing, literal search, regex grep,
  line-range reads, byte-window reads, and path/hash change detection.
- **Bounded file mutation**: exact-string edits, full-file writes, and deletes inside the
  same context policy used for reads. Optimistic `expected_sha256` checks prevent silent
  overwrite of unseen changes.
- **Independent Codex workers** with explicit model/effort profiles and durable task state.
- **Direct command execution** for exact host `argv` when another model turn adds no value.
- **Receipts and provenance** for execution state, stdout/stderr cursors, diffs, artifacts,
  and worker provenance.
- **Local Admin UI** for workspaces, profiles, context policies, runtime state, and history.

ProUse does **not** provide model credentials, bypass subscriptions, or turn a consumer
subscription into a general-purpose API. You bring your own supported model/Codex access
and your own MCP client or transport.

## Architecture

```text
        AI conversation / MCP client
                  |
                  v
             +---------+
             | ProUse  |
             +----+----+
                  |
       +----------+-----------+
       |          |           |
   context      worker      command
 read/write   delegation   execution
       |          |           |
       +----------+-----------+
                  |
         receipt / diff / artifact
```

## Requirements

- macOS or Linux (the installer supplies `uv` and Python **3.13** when needed)
- Python **3.11+** for a manual installation
- Codex CLI / app-server for worker and direct-execution features
- an MCP-capable client
- your own supported model/subscription access

## Everyday commands

`prouse admin` starts the dashboard once; a second invocation reports the existing
instance. Use `prouse start` or `prouse admin --foreground` to keep the server attached
to the terminal instead; Ctrl-C then stops it.

```bash
prouse status --json
prouse logs -f
prouse doctor --json
prouse restart --background
prouse stop
```

On a remote development host, configure LAN access with
`prouse setup --workspace /path/to/project --host 0.0.0.0`. The CLI allows the detected
LAN host and prints its address alongside localhost. Verify the LAN URL before
sharing it with the operator.

For explicit login autostart on macOS or Linux, use `prouse service install`; inspect it
with `prouse service status` and disable/remove it with `prouse service uninstall`.
`prouse stop` stops only the process owned by the selected `PROUSE_HOME`; it does not
disable autostart. Windows user-service integration is not currently supported.

## Connect an MCP client

ProUse uses client-owned stdio transport. Generate the configuration for this exact
installation and verify it:

```bash
prouse mcp config
prouse mcp check --json
```

The generated `mcpServers.prouse` entry includes the executable path and `PROUSE_HOME`;
merge it into your client's existing configuration. The check performs a real
`initialize`, tool discovery, and a policy-scoped workspace directory read, then exits.

Do not detach that process: its stdin/stdout belong to the MCP client. ChatGPT requires
a public HTTPS MCP endpoint or OpenAI's supported Secure MCP Tunnel; follow the current
[connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt/).
Browser sign-in, connection selection, and authorization remain explicit user actions.
The Admin dashboard is a separate operator surface and is never evidence that ChatGPT
is connected. See [Install for agents](docs/install-for-agents.md) for the handoff.

## Recommended first interaction

A client should first call `list_workspaces`, then `resolve_execution_context` for the
selected workspace. From there it can inspect the project with `list_directory`,
`glob_files`, `search_files`, `grep_files`, `read_files`, and `read_file_bytes`.

For mutations, use `edit_file`, `write_file`, or `delete_file` with the SHA-256 observed
from a prior read whenever replacing existing content. Use `exec_command` only when an
actual process must run, and `submit_codex_worker_task` only when an independent model
turn is useful.

See [MCP tools](docs/mcp-tools.md) for the tool surface.

## Safety model

ProUse is privileged developer tooling. Its boundaries are intentionally explicit:

- workspace roots must be registered by the operator;
- context reads and writes are limited by each workspace's context policy;
- traversal, unsafe links/file types, and secret-like paths/content are rejected by the
  context layer;
- direct commands run with the operator's normal host environment and therefore carry
  the operator's local privileges;
- a failed direct command is not silently converted into a delegated model task;
- the Admin UI binds to `127.0.0.1` by default;
- runtime state, credentials, receipts, and live policies belong outside source control.

Read [SECURITY.md](SECURITY.md) and [Security model](docs/security-model.md) before
making any component reachable beyond localhost.

## Agent skill

`SKILL.md` is the agent-facing skill for this repository, with `agents/openai.yaml` as its
metadata. It describes the advisor workflow this project exists for: keep the upper model in the
research and orchestration role, use the context tools for evidence, delegate bounded work to
independent Codex workers, run exact host commands directly when another model turn adds nothing,
and keep each round alive with the wait/wake protocol.

## Documentation

- [Configuration](docs/configuration.md)
- [Install for agents](docs/install-for-agents.md)
- [MCP tools](docs/mcp-tools.md)
- [Direct execution](docs/direct-execution.md)
- [Security model](docs/security-model.md)
- [Roadmap](ROADMAP.md)

## Repository layout

```text
SKILL.md                     agent-facing skill for this repository
agents/openai.yaml           skill metadata
examples/                    registry, policy, and schema examples
docs/                        public operator documentation
scripts/context_server.py    MCP server
scripts/context_store.py     scoped filesystem context implementation
scripts/codex_orchestrator.py delegated worker broker
scripts/direct_execution.py  direct command controller
scripts/admin_server.py      local Admin server
prouse/                      installed command and packaged examples
prouse_assets/admin_ui/      packaged local Admin interface
scripts/test_*.py            regression tests
```

## Tests

```bash
python -m compileall -q prouse scripts
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/release_check.py
```

CI runs the same validation on Python 3.11, 3.12, and 3.13.

## License

MIT. See [LICENSE](LICENSE).
