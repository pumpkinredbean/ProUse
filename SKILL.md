---
name: pro-advisor
description: Use when the user requests a senior advisor, 상급 advisor, Pro 자문, or continuation of an existing Pro research discussion. Continue the user's existing Pro conversation through Aside REPL, let Pro lead the research from direct evidence, execute its concrete requests locally, and preserve resumable work state. Ordinary local reviews do not require this workflow.
---

# Pro advisor

The worker owns workspace access and executes implementation, experiments, and mechanical
verification. The advisor is the actual Pro conversation selected by the user and is the research
orchestrator: it chooses hypotheses, interpretations, next experiments, and stop/success criteria
after reading the evidence directly. The worker reports exact outputs and execution defects; it
does not replace Pro's research judgment with its own. Operational permission still comes only
from the user.

When the user asks to fix private file access or repeated context-transfer cost, solve that
connection task before resuming research. Read the private-context notes in the private repository before changing that transport.
The implemented MCP server can supply bounded source and policy-scoped file tools directly through Secure MCP Tunnel;
record local readiness separately from verified use by the existing Pro model. Small packets
alone do not solve that structural request.

## Bridge requests

When Pro's request arrives through the MCP bridge, validate mechanics only: use the registered
`workspace_id` and an allowlisted `worker_profile_id` from `list_workspaces` and
`list_worker_profiles`, resolved with `resolve_execution_context`. On
`ambiguous_workspace`/`workspace_required`, ask a normal text question; never infer a
filesystem root. Use the same workspace ID for reads, submission and polling. Pro retains
research judgment and the user retains operational authority.

Direct execution is a local command operation that Pro selects by calling a direct tool with an
approved `workspace_id` and `operation_id`; it is not a second Codex conversation. Do not
start a model turn, invent a worker model or thread, silently delegate a failed direct command,
or infer token and cost figures. Poll actual output, inspect the actual diff, and cancel
explicitly.

File edits go through the context file tools — `edit_file` for exact-string replacements,
`write_file` for create/overwrite, `delete_file` for removal — inside the same approved
context policy as reads. Pass the observed `sha256` as `expected_sha256` so a write never
overwrites unseen changes.

For tool schemas, receipts, pause behavior, configuration and migration, read
[MCP tools](docs/mcp-tools.md), [configuration](docs/configuration.md) and
[direct execution](docs/direct-execution.md).

## Preserve the chosen channel

- Use `aside repl` to operate the existing Pro conversation directly. Do not replace it with a
  Codex subagent, `aside exec`, a different model/API, or a new conversation. A similarly named
  local agent is not evidence of contact with Pro.
- Read the installed `aside-browser` skill and run `aside guide`, then `aside guide repl`.
  Follow the current REPL documentation and inspect any matching built-in service skill.
- Read the project's `ADVISOR_STATE.md` first. Inspect existing tabs, attach the matching tab,
  and confirm conversation identity, recent exchange, and the visible model before sending.
  If the tab is closed, reopen the recorded, previously verified conversation URL.
- If no Pro conversation has been selected and recorded yet, ask the user which existing
  conversation to use; do not open or substitute a new one yourself.
- Use fresh snapshots and observed locators. Browser target IDs and REPL variables are ephemeral.
  The durable identity is the conversation URL/id. If an SPA's reported URL is stale, check the
  observed conversation link and visible thread before recording identity.
- A Pro account badge alone does not establish the selected model. Record the visible model
  label exactly; leave unobservable backend identity unknown.
- If the channel cannot be recovered, record the concrete blocker and continue independent
  local work. Do not fabricate an advisor response or silently substitute another channel.

## Give the advisor direct evidence and a bounded orchestration turn

Keep the packet and state formats in the project's own round records.
Keep the mission and stable constraints in a small baseline; send only the changed evidence,
relevant exact source excerpts, competing explanations, and the decision needed this round.
Include disconfirming observations and unknowns so the worker's summary cannot hide the problem.

- Local paths alone are provenance, not remote access. With a verified context connection,
  supply the decision and source identities and let Pro search/read directly. Otherwise include
  the necessary text/table or an actually attached file. A public GitHub repository is never a
  prerequisite for this workflow.
 - Keep the advisor role explicit in every packet: Pro inspects evidence, decides and issues the
   next task while the worker implements. Its context tools are for reading and review; execution
   requests belong to genuine local changes.
- Prefer one consequential orchestration turn per round. State the unresolved objective and new
  evidence without selecting the answer for Pro. Ask Pro to inspect the relevant files directly,
  decide what they establish, and issue one executable next task. Let Pro request additional code
  or rows before making unsupported assumptions.
- Code questions include the relevant function and caller/contract, exact file content hash,
  current behavior, and observed test/failure. Do not send the entire repository by default.
- Reuse stable evidence IDs and identify corrected/superseded claims explicitly.
- Save the exact outgoing packet before sending. Preserve a nonempty composer before replacing
  it, and compare its contents with the thread to avoid resending an already answered message.

## Send, receive, and use the advice

1. Set the local round to `prepared`, with the conversation id and exact packet path/hash.
2. Fill and send through the observed UI. Confirm the packet's round id appears as a user message
   outside the composer in a fresh snapshot before marking `sent`. If submission is ambiguous,
   inspect the thread; do not send another copy just because a tool timed out.
3. While Pro is responding, stay in the turn: block on the answer with
   `scripts/advisor_wait.py wait` and perform independent local work between waits. Keep one
   outstanding question in that conversation. Persist `waiting`, the pre-send baseline (last
   assistant hash and message count), and the last observed UI state.
4. Save the completed response verbatim with its browser evidence and observation time, then
   mark `received`. A local draft or another agent's answer is never a Pro response.
5. Translate Pro's concrete task into an execution record without changing its research intent.
   Check only execution prerequisites, source versions, arithmetic reproduction, clocks, and the
   user authorization boundary. If evidence contradicts the request, report the contradiction to
   Pro instead of silently choosing a different research direction.
6. Apply and validate the authorized work locally, then report exact outputs, diffs, failures, and
   unknowns back to Pro. Treat suggested patches as bound to the source version Pro read.
7. Update `ADVISOR_STATE.md` and the round record. A response receipt is not completion; the loop
   ends only when Pro states its stop/success criterion is met or the user changes the objective.

## Keep the round alive

A round in `sent`/`waiting` is owned only when the response was captured, a verified watcher is
armed, or the user stopped the work. Never end a turn with an unowned round, and never report a
round as in progress when nothing will resume it.

- Default: wait inside the turn with `advisor_wait.py wait`, using bounded waits and local work
  between them. Record the baseline (last assistant hash and count) before sending the packet.
- Before yielding: arm `advisor_wait.py watch --detach --thread "$CODEX_THREAD_ID"`, then verify
  `phase: polling`, `process_alive: true`, and `ownership.owner: watcher` with
  `advisor_wait.py status`. The detached launch receipt and `phase: starting` are not proof of
  ownership. Record the pid, status path, and wake channel in the state file.
- The watcher queues one wake message into this same thread with `codex queue`; a client that
  owns the thread starts a turn from the queue when the session is idle. On wake, resume from the
  state file and the captured response; do not resend the packet.

The wait/wake commands, wake packet format and their preconditions are part of the installed
skill's own scripts.

## Resume after compaction

Read only the state file, current round's request/response/decision, and referenced evidence needed
for the next action. Verify any recorded process and browser state; do not trust a stale PID or
target id. If a round is `sent`/`waiting`, check the watcher status file and inspect that
conversation for its answer before sending; if the watcher is gone and no answer is visible, re-arm
it or wait again instead of yielding. If it is `received`, finish local adjudication. If it is
`applied`, run the recorded next action. Persist state at meaningful boundaries, including before
yielding or changing context. Do not re-read the full transcript or restart the discussion by
default.
