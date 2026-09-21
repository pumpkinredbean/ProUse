# ProUse Admin

The ProUse local Admin console manages the existing workspace registry and task broker. It is a separate,
local operator surface; its configuration mutations are **not exposed as MCP tools**. The upper model still
owns substantive judgment and task selection. Workers execute concrete tasks. The Admin manages
approved execution context and operational state, without granting commit/push/deploy/credential authority.

## What the operator manages

- **Overview:** live tunnel health/readiness, enabled workspaces, profiles, active tasks and failed receipts.
  Pause/resume new mutations without interrupting existing work, context reads, receipt reads, or cancellation.
- **Workspaces:** create approved projects, edit labels/default profiles, enable/disable, choose the default.
  The registration form lists recent local Codex working directories from `thread/list` and provides a
  bounded directory picker rooted at known project-parent locations, so operators normally do not type an
  absolute path. Manual paste remains available as a fallback. Discovery never starts a model turn.
  Equal, aliased, and nested roots may be registered under separate IDs; overlapping writers share one
  lock. Workspace IDs with history cannot be removed or rebound to another directory. Disabling preserves
  all receipts. Saving a workspace applies it immediately.
- **Worker profiles:** select a model, supported effort and access mode (`full_access` or
  `workspace_sandbox`), manage profile
  IDs/labels and the global default. State-directory and executable paths remain local startup settings.
- **Direct operations:** inspect durable operation receipts, bounded stdout/stderr and actual diffs, then
  cancel an active operation. Direct execution is selected by a conversational tool call for an approved
  workspace and operation ID, never by an Admin profile, workspace default, runtime registry, or browser
  resource policy. Direct receipts identify `executor_kind: direct` and set `worker_model` and
  `codex_thread_id` to `null`; delegated receipts retain independent model/thread provenance.
- **Context policies:** edit explicit relative file lists and directory/extension rules. Preview actual
  permitted files and hashes before applying. The same paths bound MCP reads and file mutations;
  secret/runtime/traversal/symlink/hardlink restrictions remain fixed. File mutations use the same
  overlapping-workspace writer lock as delegated and direct execution.
- **Tasks:** filter by workspace/task/status; inspect and download public receipts including exact model,
  effort, thread, exit, validation, blockers and scope changes. Cancellation is a durable request which the
  owning runner consumes, terminates its own worker process group and then writes a terminal receipt.
  Queued tasks can be cancelled without waiting for the preceding task. A missing runner can be reconciled
  only after acquiring its run lock and establishing that no recorded process exists. Recovery never
  retries a task or rolls back files; inspect the workspace before assigning a new task ID.
- **Connection:** actual MCP stdio discovery/health probe using the same source and registry as the tunnel;
  a fixed independent read-only worker health probe; health/readiness and dedicated tunnel restart.
  Worker probes consume model usage. They never accept a browser-provided prompt or raw model string.
  Local MCP diagnostics do not prove the state of a remote ChatGPT conversation/tool cache.
- **History:** versioned configuration/policy snapshots, reviewable restoration and an operational audit
  log. Historical task results and workspace files are never rolled back with configuration.

## Configuration transaction and effective state

Each workspace, profile, or policy form saves and applies its change immediately. There is no separate
draft, review banner, validation step, or apply button. The save request includes `expected_revision`,
hashing both registry configuration and policy bytes. A stale editor receives
HTTP 409 rather than overwriting a newer revision. Application and worker submission share an OS file
lock. Active or corrupt delegated task state or direct operation blocks
configuration changes and tunnel restarts. Terminal direct history also prevents workspace-ID/root rebinding.
Policies are saved
as immutable, content-addressed files under the private administration state and the registry is
atomically replaced. Original project policy files remain untouched. Revision snapshots preserve
configuration plus policy bytes so restoration does not depend on an external file still having old bytes.

The MCP server reloads and validates the registry under the same lock on every tool call. Invalid external
edits fail closed. New settings take effect on the next call, without restarting for ordinary changes.
`mcp-status.json` records the registry hash and time observed by the most recent local MCP call, including
short-lived diagnostic sessions; it is not a process liveness assertion. Changes to the pinned state path
or executable require local startup reconfiguration. Already issued task requests retain their resolved
profile, model, root and request hash.

## Start

Use the Python environment from `scripts/requirements-context.txt`:

```sh
python scripts/context_server.py --registry /path/to/.state/workspace-registry.json
python scripts/admin_server.py \
  --registry /path/to/.state/workspace-registry.json \
  --runtime-config /path/to/.state/admin-runtime.json \
  --host 127.0.0.1 --port 8848
```

`--runtime-config` is optional. When absent, registry, policy and task management plus local MCP
probes work; tunnel restart is unavailable. The local JSON is trusted operator configuration and is
never editable through browser input. The shipped adapter uses the installed tunnel-client `run`
command and `tmux`; it matches the full fixed process command before stopping it and refuses duplicate
matching runtimes. It does not stop services by a fuzzy name, run arbitrary commands, or manage other tunnels.

Example non-secret adapter configuration (replace addresses/paths locally):

```json
{
  "binary": "/opt/tunnel-client",
  "profile_dir": "/opt/orchestrator/.state/profiles",
  "profile": "workspace-context",
  "session_name": "workspace-context-tunnel",
  "health_url": "http://127.0.0.1:8847",
  "operator_url": "http://127.0.0.1:8847/ui"
}
```

Keep the existing tunnel profile and authentication reference; this console does not edit credentials,
create remote tunnels, or publish applications. Runtime adapters for other supervisors should preserve
the same fixed target and active-task safeguards.

## Local access

The Admin opens directly without a login, access code, account, or expiring operator session.
This is the user's local management surface. It binds `127.0.0.1` by default and accepts loopback
plus explicitly supplied LAN Host headers. Browser requests automatically bootstrap an in-memory
CSRF value; it is request protection, requires no operator input, is not written as a credential,
and is not a login gate. Cross-origin writes and unapproved Host headers remain blocked.
CSP denies third-party scripts and framing. Registry paths and operational data are available
on this configured local/LAN surface. This is a single trusted operator/LAN administration mode.
Raw worker logs and credentials are not served. Receipt text renders as text, not HTML.
The former generated `admin/access.json` is removed at startup and no replacement code is created.

## HTTP contracts

GETs require no login. POSTs require the automatically fetched browser CSRF value and same origin. No CORS is enabled.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/session` | Automatic request-protection bootstrap; no operator authentication |
| `GET /api/state` | Current config/policies, revision, worker tasks, direct executions/diagnostics, and runtime |
| `GET /api/schema` | Registry and strict context-policy JSON schemas |
| `GET /api/workspace-candidates` | Registered roots and recent unique Codex thread working directories |
| `POST /api/workspace/browse` | Browse direct child directories within discovered project-parent roots |
| `POST /api/config/apply` | Apply bundle with `expected_revision` |
| `POST /api/config/rollback` | Restore `revision` with `expected_revision` |
| `POST /api/policy/preview` | Actual bounded context index for the saved `bundle`, `workspace_id` |
| `POST /api/admission` | Set boolean `paused` |
| `POST /api/tasks/cancel` | Request cancellation with `workspace_id`, `task_id` |
| `POST /api/tasks/reconcile` | Recover an orphan's terminal history; never signals arbitrary PIDs |
| `POST /api/runtime/probe` | Actual stdio MCP initialize/discovery/health |
| `POST /api/runtime/worker-probe` | Fixed no-write health task for `workspace_id`, optional `worker_profile_id` |
| `POST /api/runtime/direct-diagnostics` | Local direct app-server/backend readiness data; no model call |
| `POST /api/direct/get` | Poll an execution/operation with bounded base64 stdout/stderr and cursors |
| `POST /api/direct/diff` | Retrieve the actual bounded workspace diff for an execution/operation |
| `POST /api/direct/cancel` | Request termination of the owned direct operation; paused admission does not block it |
| `POST /api/runtime/restart` | Restart only the pinned dedicated tunnel after checking active tasks |
| `GET /api/history`, `GET /api/revisions/<sha256>` | Audit/revision metadata and reviewable saved bundle |

Bodies are bounded at 512 KB. Responses use no-store. Workspace root paths are available only to this
local operator; the remote MCP surface continues to return workspace IDs and relative paths.
Audit records capture operation metadata, not arbitrary worker log payloads. Runtime data
lives under the existing gitignored `.state` boundary with restrictive file/directory modes.

## Direct execution safeguards and usage

The conversation selects direct execution by calling a native tool with an approved workspace and operation ID.
Admin does not configure execution profiles, registered runtimes, direct/default workspace mode, session
lifecycle, or custom command timeout/output/concurrency policy. The controller owns fixed bounded implementation
limits, resolves the workspace server-side, uses network-disabled app-server permissions, and applies the shared
writer guard, manifest change evidence, and scope checks. While admission is paused, new direct commands are
rejected; receipt/output reads and cancellation remain available.

The native direct tools are command execution, output retrieval, stdin, cancellation, workspace diff, and artifact
read. File edits use the context file tools or explicit native commands. The controller never replays or
rolls back a dropped or interrupted operation automatically.

Direct execution proves only local command execution. It has no independent Codex worker model or thread, and the
Admin does not derive token counts or dollar costs from commands. A delegated worker remains a separate inference
path with its own runtime model/effort/thread fields. MCP metadata also cannot prove the exact upstream ChatGPT
model or private reasoning state. Local MCP discovery validates the installed server source; it does not prove that
a particular ChatGPT conversation has refreshed its tool catalogue.

## Validation

Run `python -m unittest discover -s scripts -p 'test_*.py'` in the context environment. Coverage includes
MCP hot reload and fail-closed malformed edits, admission control, transactions/conflicts/rollback,
context policy boundaries, active-task guards, running and queued cancellation, orphan reconciliation,
fixed health probes, direct local access without login, Host/Origin/CSRF checks and private state permissions.
Use the Admin's MCP diagnostic and independent worker check for actual installation verification.
