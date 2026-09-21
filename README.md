# ProUse

**Use Pro models across your local workspaces.**

ProUse is an open-source local MCP control layer for giving an AI conversation
explicit, inspectable access to approved project workspaces. It keeps source
inspection, bounded file mutation, delegated Codex work, and direct host command
execution behind one workspace registry with durable receipts.

ProUse is for developers who want a strong interactive model to remain the
research and orchestration layer while local tools perform mechanical work.

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

- Python **3.11+**
- Codex CLI / app-server for worker and direct-execution features
- an MCP-capable client
- your own supported model/subscription access

## Quick start

```bash
git clone https://github.com/pumpkinredbean/ProUse.git
cd ProUse

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p .state
cp examples/workspace-registry.example.json .state/workspace-registry.json
cp examples/context-policy.example.json .state/context-policy.example.json
```

Edit `.state/workspace-registry.json` and replace the example workspace `root`
with the absolute path of a real project. The example `context_policy` is resolved
relative to the registry file, so the two copied example files work together.

Validate configuration before starting the server:

```bash
python scripts/context_server.py \
  --registry .state/workspace-registry.json \
  --check-config
```

Start the MCP server over stdio:

```bash
python scripts/context_server.py --registry .state/workspace-registry.json
```

Start the optional local Admin UI in another terminal:

```bash
python scripts/admin_server.py \
  --registry .state/workspace-registry.json \
  --host 127.0.0.1 \
  --port 8848
```

Then open `http://127.0.0.1:8848/`.

## Connect an MCP client

ProUse uses stdio transport. MCP client configuration differs by client, but the
process to launch is simply your virtualenv Python plus `context_server.py` and the
registry path. A generic configuration looks like:

```json
{
  "command": "/absolute/path/to/ProUse/.venv/bin/python",
  "args": [
    "/absolute/path/to/ProUse/scripts/context_server.py",
    "--registry",
    "/absolute/path/to/ProUse/.state/workspace-registry.json"
  ]
}
```

If your client requires a remote HTTPS MCP endpoint, put the stdio server behind an
authenticated transport you control. The Admin UI is a separate local operator surface
and is not the MCP endpoint.

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

`SKILL.md» is the agent-facing skill for this repository, with `agents/openai.yaml» as its
metadata. It describes the advisor workflow this project exists for: keep the upper model in the
research and orchestration role, use the context tools for evidence, delegate bounded work to
independent Codex workers, run exact host commands directly when another model turn adds nothing,
and keep each round alive with the wait/wake protocol.

## Documentation

- [Configuration](docs/configuration.md)
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
scripts/admin_ui/            local Admin interface
scripts/test_*.py            regression tests
```

## Tests

```bash
python -m compileall -q scripts
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/release_check.py
```

CI runs the same validation on Python 3.11, 3.12, and 3.13.

## License

MIT. See [LICENSE](LICENSE).
