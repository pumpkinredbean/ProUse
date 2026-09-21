# Direct execution

Direct execution exists for work where another model inference would add no value: run a
test, invoke a formatter, inspect Git state, execute a deterministic build, or perform
another exact local command selected by the orchestrator/operator.

## Contract

`exec_command` receives an explicit workspace, operation ID, argv, working directory,
timeout/output bounds, and optional interactive settings. The command is executed using
the operator's host environment rather than a restricted source-reading sandbox.

That distinction is important: **direct execution is privileged local process execution**.
The workspace/context policy controls the MCP filesystem tools; it is not a shell sandbox
for arbitrary child processes.

## No implicit fallback

A failed direct command remains a failed direct command. ProUse does not silently replace
it with a delegated worker or invent a model/thread receipt. If a model worker is desired,
submit one explicitly.

## Receipts

Operations expose durable state including status, exit code, stdout/stderr cursors and,
where applicable, changed paths, workspace diffs, and artifacts. Poll actual operation
state rather than assuming success from submission.

## Operational guidance

- Prefer argv arrays over shell interpolation when possible.
- Use bounded timeouts and output caps.
- Inspect the resulting diff before accepting code-changing commands.
- Do not treat MCP connectivity itself as authorization to publish, deploy, move funds,
  rotate credentials, or perform other external side effects.
- Keep runtime receipts and command artifacts in private state, not in the repository.
