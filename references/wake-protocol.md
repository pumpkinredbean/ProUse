# Keep the round alive: wait and wake

A round is not finished when the packet leaves the composer. It is finished when the worker has
read the advisor's answer and acted on it. The gap between those two moments is where this
workflow used to die: the worker sent a packet, wrote `waiting` in the state file, ended its
turn, and the session stayed idle while the answer sat in the browser tab. Nothing resumed it
except the user.

The rule that removes that failure is short: **a turn may end with a round outstanding only if
something else will resume it.** Everything below is how to satisfy that rule.

## What counts as an owned round

A round in `sent` or `waiting` is owned when one of these is true:

1. the response was captured and the round moved to `received`;
2. a watcher is armed and verified: its status file exists, `process_alive` is true, and the
   last poll is recent;
3. the user explicitly told the worker to stop and the state file records that.

Anything else is an unowned round. Do not end the turn in that state, and do not describe it as
"waiting for the advisor" in the final message.

## Layer 1 - wait inside the turn (default)

The advisor usually answers in minutes. Stay in the turn and block on the answer instead of
yielding. `scripts/advisor_wait.py wait` polls the conversation read-only through the aside REPL
and returns as soon as the response is complete:

```sh
python3 <skill>/scripts/advisor_wait.py wait \
  --conversation <conversation url or id> \
  --out <round_dir>/response.md \
  --round <round id> \
  --baseline-hash <last assistant hash recorded before sending> \
  --baseline-count <assistant message count recorded before sending> \
  --timeout 900 \
  --status-file <round_dir>/wait.status.json
```

Exit codes: `0` captured, `3` not captured before the deadline, `2` configuration error. The
command prints one JSON line either way, and the status file is rewritten on every poll.

Two habits make this reliable:

- **Record the baseline before sending.** While confirming that the packet left the composer,
  note the last assistant message hash and the assistant message count. That baseline is what
  makes "a new answer arrived" unambiguous. Without it the poller can only compare against its
  own start time and may capture the previous answer.
- **Keep the waits bounded and interleave local work.** A wait of 10-15 minutes with local
  execution between waits keeps the turn responsive and gives the user progress to read. A wait
  that returns `3` is not a reason to end the turn; it is a reason to keep working, wait again,
  or move to layer 2.

A completed capture atomically writes the response verbatim and records a `receipt` in the status
file (conversation id, assistant count, text hash, character and byte lengths, response SHA256,
capture time, polls). Treat that file as the advisor's answer; a draft, a summary, or another
agent's text is never a substitute.

## Layer 2 - arm the watcher before yielding

Sometimes the turn has to end while the advisor is still thinking: the user interrupts, the
context is nearly exhausted, the deadline is far away, or the local work is finished. Arm the
watcher first.

```sh
python3 <skill>/scripts/advisor_wait.py watch --detach \
  --conversation <conversation url or id> \
  --out <round_dir>/response.md \
  --round <round id> \
  --thread "$CODEX_THREAD_ID" \
  --state <ADVISOR_STATE.md> \
  --baseline-hash <hash> --baseline-count <count> \
  --timeout 21600 \
  --status-file <round_dir>/watch.status.json
```

Then verify it before ending the turn:

```sh
python3 <skill>/scripts/advisor_wait.py status --status-file <round_dir>/watch.status.json
```

The detached command's `phase: detached` output is only a launch receipt. The watcher is armed
when a subsequent `status` call shows `phase: polling`, `process_alive: true`,
`ownership.owned: true`, `ownership.owner: watcher`, and a `last_poll_at` within the poll
interval. `starting` is explicitly unowned. Record the pid, the status path, and the wake thread
id in the state file. If the watcher is not armed, the round is unowned and the turn must not end.

The watcher writes the response, then queues one wake message into the worker's own thread. It
never sends anything to the advisor conversation, and it never starts a second model turn of its
own beyond that single queued message.

After capture, ownership moves from `watcher` to `response_capture`; the response remains durable
even after the watcher exits. A failed queue operation is recorded as `wake.ok: false` and still
requires explicit reporting or manual resume. `watch` requires a nonempty `--thread` (or
`CODEX_THREAD_ID`) before it launches, so it cannot knowingly leave a detached capture with no
wake destination.

## The wake channel

```sh
codex queue --thread <worker thread id> --message <wake packet>
```

This is the daemon's per-thread message queue (`thread/queue/add`). A client that owns the thread
starts a turn from the queue when the thread is idle (`thread/queue/start`). The worker thread id
is the `CODEX_THREAD_ID` variable of the session that armed the watcher, so the wake returns to
the same session with its full history.

Verified on 2026-09-21 with codex-cli 0.153.4 on this machine:

- a message queued into an unloaded thread stayed pending while no client was attached, and
  started a turn 37 seconds after a client attached to that thread;
- a message queued into an idle session owned by the desktop app started a turn immediately, with
  no user action: the new turn id matched the queued submission id, and the session resumed with
  its existing history;
- the queued turn answered the queued text, so the wake resumes the round rather than starting a
  fresh conversation.

Confirm the channel once per environment instead of assuming it. Queue a self-describing packet
into the worker's own thread and end the turn:

```sh
codex queue --thread "$CODEX_THREAD_ID" --message "[ProUse wake self-test] ..."
```

If the session resumes by itself, the channel is verified for this host. If the message stays
queued, the wake is late rather than lost, and the final message must say that no client owned the
thread when the packet was queued.

Preconditions to state honestly when they may not hold:

- the thread must be idle; a queued message is deferred while a turn is running;
- some client must own the thread. If the session is closed everywhere, the queued wake stays
  pending and fires the next time the thread is opened. The response is never lost, but the wake
  is late, and the final message should say so.

Do not use `codex exec resume <thread>` as a fallback wake. The daemon holds the writer lock for
a live thread and the command fails with "thread-store conflict: thread ... already has an active
writer". There is no second writer path for a session that is open in the app.

## The wake packet

Keep it short and point at files instead of restating the round:

```text
[ProUse wake] round R007: advisor response captured.
- response: <round_dir>/response.md (sha256 <hash>, captured <time>)
- state: <ADVISOR_STATE.md>
Resume the round from the state file: read the response, adjudicate it, execute the advisor's next
task, and report back. Do not resend the packet.
```

`scripts/advisor_wait.py watch` builds this text itself; pass `--state` so the path is included.

## Resuming after a wake

1. Read the state file, the round record, and the captured response.
2. Check the receipt: the status file's `response_sha256` must match the response file, and the
   capture time must be after the packet was sent.
3. Adjudicate the response and continue the round. Do not resend the packet, do not re-open the
   discussion, and do not treat the wake itself as new evidence.
4. If the capture is missing or ambiguous, inspect the conversation directly before acting, and
   record what the inspection showed.

## Round liveness in the state file

Keep a small block next to the round table so a resumed session can tell whether the loop is
running:

```text
## Round liveness

- Round: R007; phase: waiting
- Baseline before send: last assistant hash 80ac41b5, count 3 (2026-09-21 19:38 KST)
- Watcher: pid 56412, research_artifacts/.../watch.status.json, armed 19:39 KST, timeout 6h
- Wake channel: codex queue --thread 01a0c1c3-..., verified 19:41 KST (receipt in the status file)
- Last verified poll: 19:41 KST (no_new_message)
```

## Failure modes to avoid

- Ending the turn with `waiting` and no watcher, then reporting the round as in progress.
- Sending a second copy of a packet because a poll or a tool call timed out. Inspect the thread
  instead.
- Capturing the previous answer because no baseline was recorded before sending.
- Waking with a long message that re-explains the round; the packet should point at the files.
- Claiming the wake is armed without reading the status file, or leaving a failed wake
  (`wake.ok: false`) unmentioned in the final message.
- Treating the detached launch receipt or `phase: starting` as proof that a watcher owns the round.
- Treating a local draft, a summary, or another agent's text as the advisor's response.
