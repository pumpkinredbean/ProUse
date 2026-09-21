# Direct Pro execution

Direct execution is the bounded local Codex app-server command path for the current Pro conversation. It is
not a second Codex conversation and does not create a model turn or independent Codex thread.

## Selection and scope

The conversation selects direct execution by calling a direct tool with an approved `workspace_id`. Independent
worker delegation remains selected by the existing worker-submission tool. There is no execution-profile editor,
runtime registry, workspace/global direct default, session/open-close workflow, or browser-managed direct
resource policy. The original workspace registry, context policies, and delegated worker profiles remain in place.

The controller resolves the workspace server-side and applies fixed implementation bounds and network-disabled
app-server permissions. Requests never select a filesystem root, model, permission override, or approval
escalation. A direct operation and a delegated worker share the workspace writer guard.

## Native operation tools

Each direct tool addresses an approved `workspace_id` and caller-supplied `operation_id`:

- `exec_command` starts explicit argv through the local app-server command lifecycle.
- `get_execution` returns the durable operation receipt and bounded base64 stdout/stderr chunks.
- `write_execution_stdin` writes only to the owned active native command when its connection permits it.
- `cancel_execution` records cancellation for the owned operation.
- `workspace_diff` returns the actual bounded manifest-derived workspace change set after an operation.
- `read_artifact` reads a bounded workspace-relative artifact produced by that operation.

The receipt has `executor_kind: "direct"`, `worker_model: null`, and `codex_thread_id: null`. Operation IDs are
idempotent only with identical request bytes. A dropped connection never authorizes replay. File edits use the
context file tools (`write_file`, `edit_file`, `delete_file`) or explicit native commands.

## Admission and availability

Admission pause rejects new direct side effects but preserves receipt/output reads and cancellation. Direct
operations are retained as history; the controller does not automatically replay or roll back an interrupted
operation. Local Admin can inspect direct operation history but has no direct-mode or runtime settings.

Local stdio discovery proves only that the local server source exposes its tools. It does not prove that a
particular ChatGPT Pro conversation has refreshed its connector tool catalogue or identify that conversation's
private model/reasoning state. The current upstream catalogue must be refreshed separately before a conversation
can invoke new direct tools.
