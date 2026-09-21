# MCP tool surface

The public MCP surface is organized around four operations: inspect, mutate, delegate,
and execute.

## Registry and resolution

- `list_workspaces` — list enabled registered workspaces.
- `inspect_workspace` — inspect metadata for one registered workspace.
- `list_worker_profiles` — list allowed delegated-worker profiles.
- `resolve_execution_context` — resolve a workspace/profile pair before execution.
- `registry_health` — inspect registry/runtime readiness.

Use stable `workspace_id` values. Do not infer or pass arbitrary filesystem roots through
the MCP interface.

## Workspace inspection

- `workspace_index` — paginated approved-path discovery with lazy hashing.
- `list_directory` — bounded directory listing inside policy scope.
- `glob_files` — path discovery with glob patterns.
- `search_files` — literal multi-term search with cursor pagination.
- `grep_files` — regex or literal line-oriented search with filters and context lines.
- `read_files` — bounded UTF-8 line-range reads.
- `read_file_bytes` — byte windows for minified/generated/oversized-line content.
- `changed_files` — compare previously observed path/SHA-256 evidence against current state.

Responses carry workspace identity and freshness/hash evidence. Treat a file SHA-256 as
the identity of the exact bytes that were reviewed.

## Workspace mutation

- `edit_file` — ordered exact-string replacements.
- `write_file` — create or fully overwrite one UTF-8 file.
- `delete_file` — remove one approved regular file.

When changing an existing file, pass the SHA-256 observed from a prior read as
`expected_sha256`. If the file changed in between, the write fails instead of overwriting
unseen bytes.

These operations use the same context policy as reads and can be paused by the local
operator.

## Delegated workers

- `submit_codex_worker_task` — start one bounded independent Codex task.
- `get_codex_worker_task` — poll durable task state/output.

Delegated workers retain their configured model and reasoning-effort provenance. Use a
worker when another model inference is actually useful, not as a wrapper around a simple
command.

## Direct execution

- `exec_command` — run one exact host command request.
- `get_execution` — poll status and paginated stdout/stderr.
- `cancel_execution` — explicitly cancel a running operation.
- `write_execution_stdin` — feed stdin to an interactive operation when enabled.
- `workspace_diff` — inspect operation-attributed workspace changes.
- `read_artifact` — read a bounded artifact produced by an operation.

Direct execution is intentionally distinct from delegated inference: it has no worker
model, model turn, or Codex conversation provenance.
