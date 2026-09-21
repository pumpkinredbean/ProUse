#!/usr/bin/env python3
"""Wait for, capture, and wake on a Pro advisor response.

The worker owns the round loop. Two entry points share one read-only polling core:

  wait   block inside the current turn until the advisor response is captured
  watch  run detached, capture the response, then queue a wake message into the
         worker's own Codex thread so the round resumes without the user

The poller reads the conversation through the aside REPL (read-only). It never
sends, edits, or clears anything in the advisor conversation, and it never
starts a model turn by itself: the wake step only queues one message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_POLL_INTERVAL = 20.0
DEFAULT_STABLE_POLLS = 2
DEFAULT_WAIT_TIMEOUT = 1800.0
DEFAULT_WATCH_TIMEOUT = 21600.0
CONVERSATION_ID = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,}")


class WaitError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def conversation_id(value: str) -> str:
    """Accept a full conversation URL or a bare conversation id."""
    text = (value or "").strip()
    if not text:
        raise WaitError("conversation URL or id is required")
    match = re.search(r"/c/([0-9a-fA-F][0-9a-fA-F-]{7,})(?:[/?#]|$)", text)
    if match:
        return match.group(1)
    if CONVERSATION_ID.fullmatch(text):
        return text
    raise WaitError(f"cannot read a conversation id from {value!r}")


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


PROBE_TEMPLATE = """const tabs = await listBrowserTabs();
const target = tabs.find((t) => (t.url || '').includes(__CONVERSATION_ID__));
if (!target) {
  console.log(JSON.stringify({ok: false, error: 'tab_not_found'}));
} else {
  const p = await attachBrowserTab(target.targetId);
  const info = await p.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-message-author-role]'));
    const assistants = nodes.filter((e) => e.getAttribute('data-message-author-role') === 'assistant');
    const last = assistants[assistants.length - 1];
    const text = last ? (last.innerText || '') : '';
    let hash = 2166136261 >>> 0;
    for (let i = 0; i < text.length; i++) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619) >>> 0;
    }
    const stop = document.querySelector('[data-testid="stop-button"]');
    const composer = document.querySelector('#prompt-textarea');
    return {
      url: location.href,
      assistantCount: assistants.length,
      lastLen: text.length,
      lastHash: hash.toString(16),
      stopButton: Boolean(stop),
      composerText: composer ? (composer.innerText || '') : '',
      text: text
    };
  });
  console.log(JSON.stringify({ok: true, ...info}));
}
"""


def probe_program(conversation: str) -> str:
    """The REPL program that reads the advisor conversation without changing it."""
    return PROBE_TEMPLATE.replace("__CONVERSATION_ID__", json.dumps(conversation))


def parse_probe(stdout: str) -> dict:
    """Read the probe result from aside REPL output.

    The REPL prints the console output first and a timing line such as
    "[ok | 37ms]" last, so scan for the last line that parses as our object.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "ok" in payload:
            return payload
    raise WaitError("aside REPL returned no probe result")


def probe(conversation: str, aside_bin: str = "aside", account: str | None = None,
          timeout: float = 120.0) -> dict:
    argv = [aside_bin, "repl"]
    if account:
        argv += ["--account", account]
    argv.append(probe_program(conversation))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise WaitError(f"aside CLI not found: {aside_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WaitError("aside REPL probe timed out") from exc
    if result.returncode != 0 and not result.stdout.strip():
        raise WaitError(f"aside REPL failed: {(result.stderr or '').strip()[:300]}")
    return parse_probe(result.stdout)


def evaluate(probe_result: dict, baseline_hash: str | None, baseline_count: int | None,
             stable_hash: str | None, stable_count: int, required_stable: int) -> tuple[bool, str]:
    """Decide whether the advisor response is complete.

    A response is complete when the advisor is not streaming, the last assistant
    message differs from the recorded baseline, and its text hash has been stable
    for the required number of consecutive polls.
    """
    if not probe_result.get("ok"):
        return False, str(probe_result.get("error") or "probe_failed")
    if probe_result.get("stopButton"):
        return False, "streaming"
    text_hash = str(probe_result.get("lastHash") or "")
    try:
        count = int(probe_result.get("assistantCount") or 0)
        length = int(probe_result.get("lastLen") or 0)
    except (TypeError, ValueError):
        return False, "invalid_probe_result"
    if length == 0 or not text_hash:
        return False, "no_assistant_message"
    if baseline_hash is None and baseline_count is None:
        changed = True
    else:
        changed = ((baseline_hash is not None and text_hash != baseline_hash)
                   or (baseline_count is not None and count > baseline_count))
    if not changed:
        return False, "no_new_message"
    if text_hash != stable_hash:
        return False, "changing"
    if stable_count < required_stable:
        return False, "stabilizing"
    return True, "captured"


def write_response(path: Path, text: str) -> tuple[str, int]:
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest(), len(data)


def write_status(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + chr(10))
    tmp.replace(path)


def set_ownership(payload: dict, process_alive: bool) -> None:
    """Record whether the round has a durable owner at this observation."""
    phase = payload.get("phase")
    if phase == "captured" and payload.get("response_sha256"):
        owned, owner, reason = True, "response_capture", "response_captured"
    elif phase == "polling" and process_alive:
        owner = "in_turn_wait" if payload.get("mode") == "wait" else "watcher"
        owned, reason = True, f"{owner}_polling"
    else:
        owned, owner = False, None
        reason = "watcher_not_verified" if phase in {"starting", "polling"} else str(phase or "unknown")
    payload["ownership"] = {"owned": owned, "owner": owner, "reason": reason}


def wake_message(round_id: str, response_path: Path, digest: str, state_path: Path | None,
                 captured_at: str) -> str:
    """The queued message that resumes the worker session."""
    lines = [
        f"[ProUse wake] round {round_id}: advisor response captured.",
        f"- response: {response_path} (sha256 {digest}, captured {captured_at})",
    ]
    if state_path is not None:
        lines.append(f"- state: {state_path}")
    lines.append("Resume the round from the state file: read the response, adjudicate it, "
                 "execute the advisor's next task, and report back. Do not resend the packet.")
    return chr(10).join(lines)


def queue_wake(thread_id: str, message: str, codex_bin: str = "codex",
               timeout: float = 120.0) -> dict:
    """Queue the wake message into an existing Codex thread (no shell, no new thread)."""
    thread_id = thread_id.strip()
    if not thread_id:
        raise WaitError("a Codex thread id is required to wake the session")
    argv = [codex_bin, "queue", "--thread", thread_id, "--message", message]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise WaitError(f"codex CLI not found: {codex_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WaitError("codex queue timed out") from exc
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    ok = result.returncode == 0 and "Queued message" in stdout
    return {
        "channel": "codex queue",
        "thread_id": thread_id,
        "queued_at": now_iso(),
        "ok": ok,
        "exit_code": result.returncode,
        "stdout": stdout[:500],
        "stderr": stderr[:500],
    }


def poll_loop(conversation: str, response_path: Path, *, round_id: str, timeout: float,
              poll_interval: float, required_stable: int, baseline_hash: str | None,
              baseline_count: int | None, status_path: Path | None, aside_bin: str,
              account: str | None, mode: str, on_captured=None) -> dict:
    """Poll until the response is captured or the deadline passes."""
    started = time.monotonic()
    deadline = started + timeout
    status = {
        "round_id": round_id,
        "conversation_id": conversation,
        "mode": mode,
        "phase": "polling",
        "pid": os.getpid(),
        "started_at": now_iso(),
        "timeout_seconds": timeout,
        "poll_interval_seconds": poll_interval,
        "baseline_hash": baseline_hash,
        "baseline_count": baseline_count,
        "polls": 0,
        "last_reason": None,
        "last_poll_at": None,
        "response_path": str(response_path),
        "response_sha256": None,
        "captured_at": None,
        "receipt": None,
        "wake": None,
        "error": None,
    }
    set_ownership(status, process_alive=True)
    write_status(status_path, status)
    stable_hash = None
    stable_count = 0
    while True:
        try:
            result = probe(conversation, aside_bin=aside_bin, account=account)
        except WaitError as exc:
            result = {"ok": False, "error": str(exc)}
        status["polls"] += 1
        status["last_poll_at"] = now_iso()
        current_hash = str(result.get("lastHash") or "")
        if current_hash and current_hash == stable_hash:
            stable_count += 1
        else:
            stable_hash = current_hash or None
            stable_count = 1 if current_hash else 0
        done, reason = evaluate(result, baseline_hash, baseline_count, stable_hash, stable_count,
                                required_stable)
        status["last_reason"] = reason
        status["last"] = {
            "ok": bool(result.get("ok")),
            "assistant_count": result.get("assistantCount"),
            "last_len": result.get("lastLen"),
            "last_hash": current_hash or None,
            "stop_button": bool(result.get("stopButton")),
            "composer_chars": len(result.get("composerText") or ""),
        }
        if done:
            digest, response_bytes = write_response(response_path, str(result.get("text") or ""))
            status["phase"] = "captured"
            status["response_sha256"] = digest
            status["captured_at"] = now_iso()
            status["elapsed_seconds"] = round(time.monotonic() - started, 1)
            status["receipt"] = {
                "conversation_id": conversation,
                "assistant_count": result.get("assistantCount"),
                "assistant_text_hash": current_hash,
                "assistant_text_chars": result.get("lastLen"),
                "response_bytes": response_bytes,
                "response_sha256": digest,
                "captured_at": status["captured_at"],
                "polls": status["polls"],
            }
            set_ownership(status, process_alive=True)
            write_status(status_path, status)
            if on_captured is not None:
                on_captured(status)
            return status
        if time.monotonic() >= deadline:
            status["phase"] = "timeout"
            status["elapsed_seconds"] = round(time.monotonic() - started, 1)
            set_ownership(status, process_alive=True)
            write_status(status_path, status)
            return status
        write_status(status_path, status)
        time.sleep(poll_interval)


def run_wait(args) -> int:
    conversation = conversation_id(args.conversation)
    response_path = Path(args.out)
    status_path = Path(args.status_file) if args.status_file else None
    if status_path is not None and response_path.absolute() == status_path.absolute():
        raise WaitError("response and status paths must be different")
    status = poll_loop(
        conversation,
        response_path,
        round_id=args.round,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        required_stable=args.stable_polls,
        baseline_hash=args.baseline_hash,
        baseline_count=args.baseline_count,
        status_path=status_path,
        aside_bin=args.aside_bin,
        account=args.account,
        mode="wait",
    )
    print(json.dumps({
        "phase": status["phase"],
        "round": status["round_id"],
        "response": status["response_path"] if status["phase"] == "captured" else None,
        "sha256": status["response_sha256"],
        "polls": status["polls"],
        "elapsed_seconds": status["elapsed_seconds"],
        "reason": status["last_reason"],
    }, ensure_ascii=False))
    return 0 if status["phase"] == "captured" else 3


def run_watch(args) -> int:
    conversation = conversation_id(args.conversation)
    response_path = Path(args.out)
    status_path = Path(args.status_file)
    if response_path.absolute() == status_path.absolute():
        raise WaitError("response and status paths must be different")
    thread_id = (args.thread or os.environ.get("CODEX_THREAD_ID", "")).strip()
    if not thread_id:
        raise WaitError("a Codex thread id is required to wake the session")
    if args.detach:
        argv = [sys.executable, str(Path(__file__).resolve()), "watch",
                "--conversation", conversation, "--out", str(response_path),
                "--status-file", str(status_path), "--round", args.round,
                "--timeout", str(args.timeout), "--poll-interval", str(args.poll_interval),
                "--stable-polls", str(args.stable_polls), "--thread", thread_id,
                "--codex-bin", args.codex_bin, "--aside-bin", args.aside_bin]
        if args.account:
            argv += ["--account", args.account]
        if args.baseline_hash:
            argv += ["--baseline-hash", args.baseline_hash]
        if args.baseline_count is not None:
            argv += ["--baseline-count", str(args.baseline_count)]
        if args.state:
            argv += ["--state", args.state]
        log_path = status_path.with_suffix(".log")
        if response_path.absolute() == log_path.absolute():
            raise WaitError("response and watcher log paths must be different")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        starting = {
            "round_id": args.round,
            "conversation_id": conversation,
            "mode": "watch",
            "phase": "starting",
            "pid": None,
            "started_at": now_iso(),
            "response_path": str(response_path),
            "log_path": str(log_path),
            "thread_id": thread_id,
            "response_sha256": None,
            "wake": None,
            "error": None,
        }
        set_ownership(starting, process_alive=False)
        write_status(status_path, starting)
        try:
            with log_path.open("ab") as log:
                process = subprocess.Popen(argv, stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL,
                                           start_new_session=True, cwd=os.getcwd())
        except OSError as exc:
            starting["phase"] = "error"
            starting["error"] = f"cannot start watcher: {exc}"
            set_ownership(starting, process_alive=False)
            write_status(status_path, starting)
            raise WaitError(starting["error"]) from exc
        print(json.dumps({"phase": "detached", "pid": process.pid,
                          "status_file": str(status_path), "log": str(log_path)},
                         ensure_ascii=False))
        return 0

    state_path = Path(args.state) if args.state else None

    def on_captured(status):
        message = wake_message(args.round, response_path, status["response_sha256"], state_path,
                               status["captured_at"])
        try:
            status["wake"] = queue_wake(thread_id, message, codex_bin=args.codex_bin)
        except WaitError as exc:
            status["wake"] = {"channel": "codex queue", "thread_id": thread_id, "ok": False,
                              "error": str(exc), "queued_at": now_iso()}

    status = poll_loop(
        conversation,
        response_path,
        round_id=args.round,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        required_stable=args.stable_polls,
        baseline_hash=args.baseline_hash,
        baseline_count=args.baseline_count,
        status_path=status_path,
        aside_bin=args.aside_bin,
        account=args.account,
        mode="watch",
        on_captured=on_captured,
    )
    write_status(status_path, status)
    print(json.dumps({"phase": status["phase"], "round": status["round_id"],
                      "response": status["response_path"] if status["phase"] == "captured" else None,
                      "sha256": status["response_sha256"], "wake": status["wake"],
                      "polls": status["polls"]}, ensure_ascii=False))
    return 0 if status["phase"] == "captured" else 3


def run_status(args) -> int:
    path = Path(args.status_file)
    if not path.exists():
        payload = {"phase": "missing", "status_file": str(path)}
        set_ownership(payload, process_alive=False)
        print(json.dumps(payload, ensure_ascii=False))
        return 3
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WaitError(f"cannot read status file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WaitError(f"status file {path} does not contain a JSON object")
    pid = payload.get("pid")
    alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    payload["process_alive"] = alive
    set_ownership(payload, process_alive=alive)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wait for, capture, and wake on a Pro advisor "
                                                 "response.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--conversation", required=True,
                         help="Advisor conversation URL or id recorded in the state file")
        sub.add_argument("--out", required=True, help="Path that receives the verbatim response")
        sub.add_argument("--round", required=True, help="Round id used in the wake message")
        sub.add_argument("--timeout", type=nonnegative_float, default=None,
                         help="Seconds to wait before giving up")
        sub.add_argument("--poll-interval", type=nonnegative_float,
                         default=DEFAULT_POLL_INTERVAL)
        sub.add_argument("--stable-polls", type=positive_int, default=DEFAULT_STABLE_POLLS,
                         help="Consecutive identical polls required before capture")
        sub.add_argument("--baseline-hash", default=None,
                         help="Last assistant hash recorded before the packet was sent")
        sub.add_argument("--baseline-count", type=nonnegative_int, default=None,
                         help="Assistant message count recorded before the packet was sent")
        sub.add_argument("--status-file", default=None,
                         help="JSON status file; written every poll")
        sub.add_argument("--aside-bin", default="aside")
        sub.add_argument("--account", default=None, help="Aside account id such as u0")

    wait = subparsers.add_parser("wait", help="Block until the advisor response is captured")
    add_common(wait)
    wait.set_defaults(handler=run_wait, timeout=DEFAULT_WAIT_TIMEOUT)

    watch = subparsers.add_parser("watch", help="Capture the response and queue a wake message")
    add_common(watch)
    watch.add_argument("--thread", default=None,
                       help="Codex thread id to wake; defaults to the CODEX_THREAD_ID variable")
    watch.add_argument("--state", default=None, help="ADVISOR_STATE.md path for the wake message")
    watch.add_argument("--codex-bin", default="codex")
    watch.add_argument("--detach", action="store_true",
                       help="Start the watcher in its own session and return immediately")
    watch.set_defaults(handler=run_watch, timeout=DEFAULT_WATCH_TIMEOUT)

    status = subparsers.add_parser("status", help="Report a watcher status file")
    status.add_argument("--status-file", required=True)
    status.set_defaults(handler=run_status)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "status_file", None) is None and args.command in {"wait", "watch"}:
        args.status_file = str(Path(args.out).with_suffix(Path(args.out).suffix + ".status.json"))
    try:
        return args.handler(args)
    except WaitError as exc:
        print(json.dumps({"phase": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
