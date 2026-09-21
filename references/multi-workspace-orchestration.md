# Approved workspace orchestration

This is the reusable backend used by the existing context server, not a separate service.
The upper conversational orchestrator chooses the substantive objective, hypotheses,
interpretations and next task. The independent Codex worker performs concrete local operations
and reports exact results. The bridge resolves identities, enforces mechanics, and records
provenance; it makes no research decisions and never expands human authorization.

## Architecture and administration boundary

`context_server.py` exposes MCP stdio tools. `workspace_registry.py` loads one administrator-owned
JSON configuration, validates the entire registry before serving, canonicalizes roots, and
resolves workspace/profile IDs. Each workspace has its own `ContextStore`. `TaskBroker` launches
a detached `codex_orchestrator.py run` process with a durable request. Registrations whose canonical
roots are equal or nested share one writer lock; independent roots can run concurrently.

The JSON format is shared with the local administration UI through
[workspace-registry.schema.json](workspace-registry.schema.json). The server also supports
`--print-schema`, `--check-config`, and read-only `registry_health`, `list_workspaces`,
`inspect_workspace`, and `list_worker_profiles`. There are no remotely writable registry tools.
The local Admin validates and atomically replaces configuration under the administration lock;
active delegated tasks or direct operations block conflicting changes. The MCP server reloads
the registry and policies on every call, while pinned state/executable changes require a restart.
In-flight receipts remain bound to their original resolution.

## Registry

Start from [workspace-registry.example.json](workspace-registry.example.json) and
[context-policy.example.json](context-policy.example.json). Roots and local policy references
can be absolute or relative to the registry directory. Environment variables and `~` are not
expanded. A workspace contains:

- `id`: stable lower-case identifier, unique in this registry;
- `label`: human-readable label;
- `root`: an existing explicit project directory, canonicalized locally;
- `enabled`: required boolean;
- optional `default_worker_profile` and `context_policy` reference.

Duplicate IDs or JSON keys, unknown fields, missing/non-directory roots (including disabled
entries), and invalid policies/defaults fail the whole configuration. Equal, aliased, and nested
workspace roots are allowed under distinct IDs; their writers share a lock so overlapping paths are
not mutated concurrently. `/` and the home directory cannot be registered. A root symlink is resolved
once; changing that alias cannot redirect an existing loaded registry. Replacing the canonical root is
detected before context access/execution. Registries contain no secrets or authentication fields.
Private roots and configuration filenames are omitted from MCP results.

To add a workspace, approve its root locally, create its bounded context policy, add a unique enabled
entry and optional default profile, and save it through Admin (or run `--check-config` after an
operator-owned edit). Ordinary registry and policy changes apply on the next MCP call.
A workspace without `context_policy` may execute tasks but its context tools return
`context_unavailable`. This never expands reads to the entire root.

## Worker profiles and deterministic resolution

`worker_profiles` is an array of unique `{id, model, reasoning_effort, label?}` entries.
`model_capabilities` is the administrator's local allowlist of model IDs and supported efforts.
Profiles must match it. Verify capabilities against the installed CLI/catalog before changing
this allowlist; the bridge does not guess model support. A global `default_worker_profile` is
required and every workspace default must reference an existing profile. To add a profile,
verify local support, update the allowlist/profile, validate, and restart. Tasks cannot provide
raw model strings, effort strings, filesystem roots, or operational authorization.

Workspace precedence is explicit ID, configured `default_workspace_id`, then exactly one enabled
workspace. Unknown/disabled explicit IDs are errors, never fallbacks. Multiple eligible workspaces
return `ambiguous_workspace`; zero eligible workspaces return `workspace_required`. Both contain
only candidate IDs and labels. There is no fuzzy matching, conversation inference, or dynamic UI.
The upper model asks the user a normal text question if clarification is needed.

Profile precedence is explicit profile ID, workspace default, global default. Invalid explicit
profiles fail. The compatibility profile remains `gpt-5.6-sol` with an explicit `xhigh` effort,
matching the previously observed installed CLI run. These values are configuration in registry
mode, not model-controlled task options. The legacy `--root` adapter retains these defaults.

## MCP contracts

All tools return structured content with a short status text. Errors include
`status: error` and `error: {code, message}`; resolution errors may include `candidates`.
Use explicit workspace IDs even when only one is registered.

The read tools are the advisor's default surface: use them to inspect context, verify content
and review worker output. The file tools apply exact edits inside the same approved policy and
acquire the workspace's shared writer lock before mutating. The
execution tools run or delegate concrete authorized tasks; they are not for exploration.

| Tool | Contract |
|---|---|
| `list_workspaces` | Approved IDs, labels, enabled state and policy metadata; no roots |
| `inspect_workspace` | One registered workspace's public policy/default metadata |
| `list_worker_profiles` | Allowed profile IDs with model/effort and global default |
| `resolve_execution_context` | Deterministic resolution or actionable structured error |
| `registry_health` | Registry hash/counts, root availability and worker executable readiness |
| `workspace_index` | One workspace's approved relative paths; SHA256 is computed lazily for the returned page |
| `list_directory` | Approved directories and readable files below one approved directory (LS) |
| `glob_files` | Glob discovery such as `src/**/*.py` or `**/package.json`; page hashes are computed lazily |
| `search_files` | Literal multi-term search with glob filters, context lines and a resumable cursor |
| `grep_files` | Regex or literal search with glob filters, context lines and files/count output modes |
| `read_files` | Bounded exact line ranges; files larger than the fast path receive whole-file safety/hash verification and are then streamed |
| `read_file_bytes` | Exact byte window for minified, generated or single-line content after whole-file safety/hash verification (up to 32 MiB) |
| `changed_files` | Compare that workspace's path/hash map; pages over the known/current path union and reports added, modified and deleted paths |
| `write_file` | Create or fully overwrite one approved file with exact UTF-8 content; `expected_sha256` guards unseen changes |
| `edit_file` | Ordered exact-string replacements; each `old` matches once unless `replace_all` |
| `delete_file` | Remove one approved regular file and return its last content hash |
| `submit_codex_worker_task` | Validated, idempotent independent execution request |
| `get_codex_worker_task` | Durable receipt for `(workspace_id, orchestrator_task_id)`, optional wait 0–20s |

View IDs include workspace identity, canonical root identity, policy hash, and the discovered
path/device/inode/size/mtime/ctime set. Equal bytes in two workspaces share their content hash but never their view
ID. A stale or foreign `expected_view_id` returns `changed_view`, not silently substituted text.
Per-file SHA256 is the exact evidence identity: `read_files` and `read_file_bytes` compute a view
only when `expected_view_id` is supplied, so an ordinary read does not walk the workspace.
Discovery is stat-only and hashes are computed per file on demand and cached against the file
identity, so no call reads or hashes the whole workspace. Every context result has `workspace_id`;
all file items use workspace-relative paths. A bare legacy path/hash map has no source-workspace
identity, so new callers should always include `known_view_id`. Existing context output limits,
secret-content checks, descriptor-based no-symlink traversal, regular-file/hardlink checks and
allowlists remain in place.

Example submit payload:

```json
{
  "request": {
    "workspace_id": "example",
    "worker_profile_id": "standard",
    "orchestrator_task_id": "R012-T1",
    "objective": "Create the requested local report and validate its exact bytes.",
    "allowed_paths": ["reports/task-one"],
    "deliverables": ["reports/task-one/result.md"],
    "validation": ["Read back the result and report validation exit status."],
    "write_mode": "workspace_write",
    "max_seconds": 300
  }
}
```

Allowances are relative files or directories; their parents must already exist or an appropriate
ancestor must be explicitly allowed. The model cannot register a root through a task.

## Receipts, retries and concurrency

Local state is `<state_dir>/workspaces/<workspace_id>/tasks/task_<task-id-hash>/`.
The canonical request, state receipt, before/after manifests, runtime JSONL, final JSON, schema,
and local logs are private (directories 0700, files 0600). Put `state_dir` inside a gitignored
`.state` directory. Legacy originals are retained at their old location during migration.

Receipts record the orchestrator task ID, workspace ID, canonical root locally, resolved profile,
model and effort, runtime-observed model and effort, independent Codex thread ID, normalized
request SHA256, terminal state, changed/unexpected paths, validation, blockers, exit code and UTC
timestamps. Public receipts exclude local roots, PIDs, log filenames and raw event streams.
Runtime-observed model/effort are read from Codex's own persisted turn context, not a worker's
self-description. This attests the Codex runtime selection, not an independently measured backend
model implementation. Queued receipts have null actual-model fields until execution is verified.

A task lock prevents concurrent duplicate launches. Exact normalized retries return the existing
receipt, including failed tasks. Reusing the same ID with changed objective bytes, allowed paths,
profile/default resolution or validation fails. JSON key ordering and omitted defaults are
normalized; meaningful text bytes and array ordering remain significant. Use a new ID only for
an intentionally new task. A workspace lock serializes workers to avoid conflicting manifests.
A dead launcher is reported as failed; the bridge never automatically reruns it.

Success requires exit 0, one nonempty runtime thread ID, a completed clean turn, matching runtime
model/effort, exactly the required typed final JSON fields, no blockers and no unexpected writes.
Malformed JSON is never turned into successful free text. Scope violations retain the evidence;
the bridge does not reset files or destroy pre-existing changes.

## Threat model and execution restrictions

The MCP client, task text and file contents are untrusted inputs. Registry files and the local
administrator/OS account are trusted. Authentication and remote connection authorization remain
with the existing secure tunnel. This is not multi-tenant isolation against a hostile process
already running as the same local OS user, nor a transactional filesystem.

The bridge rejects absolute paths, traversal, denied internal/runtime paths, secret-like paths,
symlinks/hardlinks in allowed paths and links below allowed directories. It passes task text on
stdin using an argv array, never through shell interpolation. The worker profile's `access` field
selects the boundary. `full_access` (the default) adds no sandbox settings: the worker inherits the
operator's environment, network and filesystem, so it can run the project's interpreters, package
managers, git and local services. `workspace_sandbox` keeps the restricted profile: reads in the
chosen root, writes only to allowed paths, minimal OS tools, no network, and explicit denial of
credentials/runtime trees/git metadata. Other workspaces are outside its filesystem permissions.
`read_only` has no write allowance in either mode. Under `workspace_sandbox` shell environments do
not inherit credentials; user integrations, MCP tools, user execution rules and broad sandbox
settings are never inherited, and only local CLI model routing/catalog settings are preserved.

This v1 depends on the installed Codex CLI's named filesystem permission profiles and POSIX locks
(macOS/Linux). It does not silently fall back to a broad sandbox on incompatible runtimes.
`check_worker_sandbox.py` tests the installed OS boundary with synthetic files and no model calls.
Under `workspace_sandbox`, minimal OS tools do not include arbitrary SDKs or interpreters
installed in the home directory; if a requested validation lacks an accessible tool, report the
blocker rather than broadening permissions, or switch that profile to `full_access` deliberately.
Administrators must validate a new platform/runtime before use.

Before/after manifests cover normal workspace files, symlink identities and mode/link-count
changes. Files over 5 MiB use size/mtime/ctime identity to avoid hashing bulk datasets. Runtime,
build, dependency and worktree trees are excluded and are denied to worker commands by the
permission profile. External concurrent edits can produce a scope violation; no automatic rollback
is attempted. Human-authorized service restarts are an administration action, not worker authority.

Commit/push/publication/deployment, unrelated services/PM2, live orders/trading APIs, wallets,
secret/approval changes and credential access remain prohibited through this worker API. A future
operational workflow needs separate human authority and a separately reviewed boundary; profile
selection or task prose never grants that authority.

## Running and migrating

Install the pinned `scripts/requirements-context.txt` into a local virtual environment.
Use the directory containing this reference's sibling `scripts/` directory as the package base.
No public publishing or repository creation is needed.

```sh
python scripts/context_server.py --registry /path/to/admin/registry.json --check-config
python scripts/context_server.py --registry /path/to/admin/registry.json
python -m unittest discover -s scripts -p 'test_*.py' -v
python scripts/check_worker_sandbox.py --codex-bin /path/to/codex --scratch /path/to/local/scratch
```

The old six tool names remain available, with optional `workspace_id` parameters and profile
selection added to submissions. Omitting the workspace is deprecated but follows the same
resolver, so an existing configured single-workspace connection remains usable. `--root ROOT
--policy POLICY [--workspace-id legacy]` is a deprecated administrator-only singleton adapter.
It can read and idempotently retry original v1 receipts without relaunching them.

For registry migration, wait for old tasks to finish; create a validated registry containing the
same root and unchanged context policy. Then run:

```sh
python scripts/migrate_orchestrator_state.py --registry /path/to/admin/registry.json \
  --workspace-id example --source-state /path/to/old/.state/orchestrator
```

Migration copies terminal tasks without launching workers, preserves original bytes, records the
old request hash, and marks `legacy_receipt: true`. Historical terminal states are retained as
historical evidence; they are not retroactively certified by the stronger v2 validator. Repeated
migration is idempotent and conflicting history fails.

For the existing Secure MCP Tunnel, retain its identity/authentication and change only its stdio
command to the same Python/server plus `--registry /path/to/admin/registry.json`. Use the installed
tunnel client's actual `runtimes connect/stop/status` interface or its supported profile runner;
do not invent admin APIs or repurpose unrelated tunnels. Check `/healthz`, `/readyz`, successful
control-plane polling, stdio `initialize`/`tools/list`, context reads and a tiny real independent
worker. If the operator UI is exposed on this remote host, bind it to `0.0.0.0`, allow the LAN host,
and verify its actual LAN URL. The MCP source surface itself remains private stdio/outbound tunnel.
A ChatGPT client may cache tool discovery; refresh that connection's tool list only if its UI
still shows the old surface. Local protocol proof is distinct from a new Pro conversation turn.

Codex filesystem profiles were checked against the installed CLI and the
[official permissions documentation](https://developers.openai.com/de-DE/docs/permissions).

## Local administration console

The implementation now includes a local Admin without a login requirement for workspace/profile/context-policy
management, atomic revisions and restoration, task receipts/cancellation, admission control, and
fixed runtime diagnostics/restart. Configuration reloads on each MCP tool call. See
[Admin console](admin-console.md) for startup, HTTP contracts, local access, transaction semantics,
and operational boundaries. Administration mutations remain separate from the remote MCP API.
