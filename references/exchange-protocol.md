# Worker–advisor exchange

## Division of work

Pro is the research orchestrator. It reads the allowed workspace evidence through the confirmed
connection, decides what the evidence establishes, selects the next discriminating experiment,
and states stop/success criteria. The Codex worker has the live workspace and carries out Pro's
concrete file, computation, and validation requests. It returns exact results and execution
defects; it does not independently choose the research conclusion or next direction.

Routine file access, arithmetic reproduction, and tests are worker execution steps. When results
distinguish hypotheses or invalidate a premise, return them to Pro for the next judgment. The user
alone grants operational permission; Pro orchestration cannot authorize deploys, live orders,
wallet/secret changes, commits, pushes, or other actions outside the user's scope.

Pro's tool surface is evidence-first. The context tools (`workspace_index`, `list_directory`,
`glob_files`, `search_files`, `grep_files`, `read_files`, `read_file_bytes`,
`changed_files`) exist so the advisor can inspect evidence, verify content and review worker
output directly, and the file tools (`write_file`, `edit_file`, `delete_file`) apply exact
edits inside the same approved policy with optional `expected_sha256` preconditions. The
execution tools (`exec_command`, `submit_codex_worker_task` and their receipt/cancellation
companions) are for concrete tasks that genuinely require a local run or an independent worker;
they are not for exploration, and a read never requires them.

## Small durable state

`ADVISOR_STATE.md` is the entry point, normally kept within a few pages:

- Mission, success criterion, current scope/authorization, and applicable contracts.
- Exact channel (`aside repl`), conversation URL/id/title, observed model and verification time.
- Current round id, phase, request/response/decision paths, packet hash and send evidence.
- Round liveness: phase, pre-send baseline, watcher pid/status path, wake channel and its receipt,
  last verified poll.
- Accepted observations, rejected/superseded claims, unknowns with evidence links.
- Next executable action, its input/output/acceptance condition, and any real blockers/processes.

Round folders contain `request.md`, `response.md` (only after receipt), `decision.md`, and small
evidence files as needed. Keep browser evidence separately. Do not repeatedly paste old rounds
into new requests. A phase is one of `prepared`, `sent`, `waiting`, `received`, `applied`, `blocked`;
these describe the round, not completion of the user's broader task.

A round in `sent`/`waiting` must be owned: the response was captured, a verified watcher is
armed, or the user stopped the work. Waiting inside the turn and arming the watcher are described
in [round liveness](wake-protocol.md); that file also records the verified wake semantics, the
wake packet format, and the resume procedure.

## Outgoing packet

```text
Round: <stable id>; responds to: <last verified advisor answer>
Role: you advise; inspect the evidence, decide, and issue the next worker task. The worker implements.
Decision needed: <one concrete choice or falsifiable question>
Goal/constraints: <short baseline or changed constraints>
Unresolved objective: <what Pro must decide; do not preselect the answer>
Delta since last exchange: <observations, corrections, failed tests>
Evidence: <ids; exact tables/code; population, units, clocks, version>
Unknowns/counterevidence: <limitations affecting the decision>
Requested answer: judgment + decisive objection + next experiment + stop/success criterion.
If material is insufficient, request named evidence instead of filling gaps.
```

Aim for roughly 1–2 pages of narrative. This is a sizing heuristic, not a requirement to omit
essential context. Keep large evidence in a separately attached, bounded file and confirm it is
present. Send a relevant function with its contract/caller rather than a whole codebase or a
summary that hides executable details. Source hashes identify supplied bytes, not their truth.

The optional `scripts/build_packet.py` builds a reviewable packet from an explicit local source
list. It does not read unspecified files, execute commands, send messages, or apply code. Example:

```json
{
  "round_id": "R007",
  "request": "research_artifacts/YYYY-MM-DD/topic/request.md",
  "sources": [
    {"id": "E1", "path": "src/example.rs", "start": 25, "end": 95},
    {"id": "E2", "path": "research_artifacts/YYYY-MM-DD/topic/result.csv"}
  ]
}
```

```sh
python3 .agents/skills/pro-advisor/scripts/build_packet.py \
  --root . --spec <spec.json> --output <packet.md>
```

Read the produced file before sending. The default character budget is 24,000; an oversized
packet fails without truncation. Narrow the evidence or explicitly raise the budget when needed.

## Inbound task execution

Preserve the response first, then record a compact table:

| Pro directive | Execution state | Exact worker evidence | Reported result / blocker |
|---|---|---|---|

Separate (a) an observation in supplied data, (b) Pro's interpretation, (c) Pro's requested
experiment, and (d) permission to operate. Only the user grants the last. For a requested code
change, bind it to the source hash Pro read; if the file changed, report that fact and rebase only
the mechanics without changing the research intent. Tests check behavior; Pro decides what the
result establishes after receiving the independent data checks.

## Reducing the GitHub and conversation bottlenecks

For the user's 2026-09-20 private-access/cost request, the selected structure is **local
bounded context and policy-scoped file tools → Secure MCP Tunnel → existing Pro conversation**. The implementation,
boundaries, current connection status and runbook are in [private-context.md](private-context.md).
The worker supplies the unresolved objective and changed evidence; Pro searches and batches file
reads itself, then issues a concrete task. Follow-ups check previously read hashes and fetch
worker results. Deterministic retrieval makes no intermediary model calls. Pro's own context
usage remains; cached reasoning or zero total cost is not promised.

The local MCP implementation, tunnel authentication and actual 6 Pro source reads were verified
by R008b on 2026-09-20; the receipt records the observed source hashes. On a new connection,
local validation and real Pro use remain separate completion criteria. Do not report connected
until both work. Do not restart
the research instead of resolving this task, publish source, repurpose another project's tunnel,
or substitute another model. GitHub and manual packets are not prerequisites for this route.
Keep the bounded packet builder for a genuinely chosen attachment/text workflow, not as a claim
that repeated model-to-model file delivery has been structurally removed.

## Verified REPL usage

Run the current Aside guides first. Start a persistent `aside repl`, call `listBrowserTabs()`,
then `attachBrowserTab(observedId)`. Read with `snapshot(page, {interactive:true})` and save
fresh snapshots after UI actions. Use `openTab(recordedConversationUrl)` only when the relevant
tab is absent. Short-lived REPL variables/locator refs are never the resume key.

Waiting for a response is not a snapshot loop. Use `scripts/advisor_wait.py wait` inside the turn
and `scripts/advisor_wait.py watch --detach` before yielding; both read the conversation through
the REPL without changing it. A detached launch is not yet an armed watcher: verify `phase:
polling`, `process_alive: true`, and `ownership.owner: watcher` with the status command. Completed
capture atomically preserves the response and a receipt in the status file.

Use a scoped snapshot for the active UI when possible, for example `selector:'form'` for the
conversation composer or `selector:'[role="dialog"]'` for a settings dialog. Inspecting an
unchanged full research transcript on every UI action adds unnecessary context. Capture the
actual completed response when needed; do not truncate a snapshot into an assumed result.

When transferring local text through the shell, prefer a subprocess argument list or correctly
quoted literal content. Never interpolate JSON-stringified user/code text into a shell command:
backticks and dollar substitutions can execute. A request file is not a browser attachment until
the UI confirms the attachment. Do not use cookie APIs or hidden page state as a substitute for
the requested browser interaction.

Skill placement and implicit invocation follow the [official Codex customization documentation](https://developers.openai.com/ko-KR/docs/customization/overview).

## Workspace-aware independent execution (v2)

Pro selects the concrete task and supplies `workspace_id`, optional `worker_profile_id`, stable
`orchestrator_task_id`, objective, relative `allowed_paths`, deliverables and validation.
Discover approved IDs first. The bridge uses explicit ID → configured default → unique enabled
workspace; ambiguity returns candidates for a normal user question. It does not choose research
hypotheses, infer project roots, or change Pro's intent. Profile selection resolves only an
admin-allowlisted model and effort. Operational permission still belongs to the user.

All context evidence carries workspace ID, relative path and SHA256; `view_id` is a workspace-level
freshness token that is computed when a caller requests it. Keep those identities with the request.
`changed_files` reports added, modified and deleted paths, so a worker's new files are never missed.
Poll the same workspace/task pair and return exact
changes, unexpected changes, validation, blockers and runtime-observed provenance to Pro. A
nonzero exit, missing independent thread, model/effort mismatch or malformed structured final
result cannot be accepted as success. Reusing an ID with changed request bytes fails; exact
retries reuse durable history. Historical v1 receipts are explicitly marked during migration.
See [backend contracts and migration](multi-workspace-orchestration.md) for the maintained API.
