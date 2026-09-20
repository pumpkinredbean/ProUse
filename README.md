# ProUse

**Use Pro models across your local workspaces.**

ProUse is a local MCP control layer that connects a Pro-capable AI conversation
to approved workspaces on your machine.

It provides:
- scoped workspace context over MCP;
- multiple registered local workspaces;
- delegation to independent Codex workers;
- exact host command execution without creating another model turn;
- durable execution receipts, stdout/stderr cursors, diffs, and artifact reads;
- a local Admin UI for workspace and runtime management.

The context and worker paths are bounded by workspace configuration. The command
path is intentionally privileged local command execution and uses the operator's
normal host environment.

## Requirements
- Python 3.11+
- Codex CLI / app-server for execution features
- an MCP-capable client
- your own supported model/subscription access

## Install

    git clone https://github.com/pumpkinredbean/ProUse.git
    cd ProUse
    python3.11 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

## Configure

    mkdir -p .state
    cp examples/workspace-registry.example.json .state/workspace-registry.json
    cp examples/context-policy.example.json .state/context-policy.json

Edit the registry so the workspace root points to a real project directory.

## Run

    python scripts/context_server.py --registry .state/workspace-registry.json
    python scripts/admin_server.py --registry .state/workspace-registry.json --host 127.0.0.1 --port 8848

The MCP server uses stdio transport. The Admin UI binds to localhost by default.

## Security

ProUse can execute local commands. Treat access to it like access to a privileged
developer tool. Do not expose the Admin UI directly to the public Internet, and
do not commit runtime state or credentials.
