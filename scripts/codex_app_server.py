"""Narrow client for the installed Codex app-server command execution API.

This module intentionally has no thread, turn, model, review, shell-command, or process/spawn
entry point.  The only RPC methods it can emit after initialization are the documented
``command/exec`` lifecycle methods.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Callable


class AppServerError(RuntimeError):
    pass


ALLOWED_METHODS = {"initialize", "initialized", "command/exec", "command/exec/write", "command/exec/terminate"}


class CodexAppServer:
    """One private, persistent stdio connection.  Process IDs are connection scoped."""

    def __init__(self, codex_bin: Path, cwd: Path, private_dir: Path, permission_settings: list[str], *, expected_version="0.153.4"):
        self.codex_bin = Path(codex_bin)
        self.cwd = Path(cwd)
        self.private_dir = Path(private_dir)
        self.permission_settings = list(permission_settings)
        self.expected_version = expected_version
        self.proc: subprocess.Popen | None = None
        self._messages: queue.Queue = queue.Queue()
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._output_handlers: dict[str, Callable[[str, bytes, bool], None]] = {}
        self.sent_methods: list[str] = []
        self._threads: list[threading.Thread] = []
        self._transport_error: AppServerError | None = None

    def start(self):
        if self.proc is not None:
            return
        # Keep user config, plugins, hooks, integrations and inherited shell variables out of this executor.
        # app-server 0.153.4 has no --ignore-user-config flag.  An isolated CODEX_HOME is the
        # supported way to prevent ambient config, plugins and hooks from entering this process.
        argv = [str(self.codex_bin), "app-server", "--listen", "stdio://", "--strict-config"]
        for setting in self.permission_settings + ['approval_policy="never"', 'shell_environment_policy.inherit="all"',
                                                   'web_search="disabled"', 'features.multi_agent_v2=false']:
            argv += ["-c", setting]
        # Command is a privileged host-command path. Preserve normal operator
        # PATH, HOME, and CLI credentials; isolate only Codex config.
        codex_home = self.private_dir / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        try:
            self.proc = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True, close_fds=True)
        except OSError as exc:
            raise AppServerError("Codex app-server could not start") from exc
        try:
            self._threads = [threading.Thread(target=self._read_stdout, daemon=True), threading.Thread(target=self._drain_stderr, daemon=True)]
            for thread in self._threads: thread.start()
            # Pin the known experimental surface: fail closed instead of guessing a newer protocol.
            if self._version() != self.expected_version:
                raise AppServerError("Installed Codex app-server version is not the configured compatible version")
            self.request("initialize", {"clientInfo": {"name": "direct-execution-controller", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
            self.notify("initialized", {})
        except Exception:
            self.stop()
            raise

    def _version(self):
        try:
            text = subprocess.check_output([str(self.codex_bin), "--version"], text=True, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AppServerError("Cannot inspect Codex app-server version") from exc
        import re
        found = re.search(r"(\d+\.\d+\.\d+)", text)
        return found.group(1) if found else ""

    def _drain_stderr(self):
        if self.proc and self.proc.stderr:
            log = self.private_dir / "app-server.stderr.log"
            with log.open("ab", buffering=0) as handle:
                for line in self.proc.stderr:
                    data = line.encode("utf-8", "replace")[:4096]
                    if handle.tell() < 16384:
                        handle.write(data[:16384 - handle.tell()])

    def _read_stdout(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
                if not isinstance(message, dict): raise ValueError()
            except ValueError:
                self._fail_transport("Malformed app-server response")
                return
            if "id" in message and "method" in message:
                # We never permit server requests, including approval / user-input requests.
                self._fail_transport("Unsolicited app-server request")
                return
            if "id" in message:
                pending = self._pending.get(message["id"])
                if pending:
                    pending.put(message)
                continue
            if message.get("method") == "command/exec/outputDelta":
                params = message.get("params", {})
                handler = self._output_handlers.get(params.get("processId"))
                if handler:
                    try:
                        handler(params["stream"], base64.b64decode(params["deltaBase64"], validate=True), bool(params.get("capReached")))
                    except (KeyError, ValueError, TypeError):
                        self._fail_transport("Malformed app-server output notification")
                        return
            elif message.get("method") == "remoteControl/status/changed":
                # 0.153.4 emits this connection metadata during startup.  It carries no command
                # result and is intentionally discarded; remote control itself remains disabled.
                continue
            elif "method" in message:
                self._fail_transport("Forbidden unsolicited app-server notification")
                return
        self._fail_transport("App-server connection closed")

    def _fail_transport(self, message):
        if self._transport_error is None:
            self._transport_error = AppServerError(message)
        for pending in list(self._pending.values()):
            try:
                pending.put_nowait({"error": {"message": message}})
            except queue.Full:
                pass

    def _send(self, message):
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise AppServerError("Codex app-server is unavailable")
        if self._transport_error:
            raise self._transport_error
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        self.sent_methods.append(message["method"])

    def request(self, method, params, timeout=30, on_sent=None):
        if method not in ALLOWED_METHODS or method == "initialized":
            raise AppServerError("Forbidden app-server RPC method")
        with self._lock:
            request_id = self._next_id; self._next_id += 1
            pending = queue.Queue(maxsize=1); self._pending[request_id] = pending
            self._send({"id": request_id, "method": method, "params": params})
            if on_sent:
                on_sent()
        try:
            message = pending.get(timeout=timeout)
        except queue.Empty as exc:
            raise AppServerError("Timed out waiting for app-server RPC response") from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in message:
            error = message.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise AppServerError("Codex app-server rejected command request" + (" (" + str(code) + ")" if code is not None else ""))
        return message.get("result", {})

    def notify(self, method, params):
        if method != "initialized":
            raise AppServerError("Forbidden app-server notification")
        self._send({"method": method, "params": params})

    def exec(self, *, process_id: str, command: list[str], cwd: str, timeout_ms: int | None = None, output_cap: int | None = None,
             permission_profile: str | None = None, sandbox_policy: dict | None = None,
             on_output: Callable[[str, bytes, bool], None] = lambda *_: None,
             on_request_sent=None, stream_stdin=True, stream_stdout_stderr=True, tty=False, disable_timeout=None, disable_output_cap=None):
        if permission_profile is not None:
            raise AppServerError("Named app-server permission profiles are not used by direct execution")
        if sandbox_policy is not None and not isinstance(sandbox_policy, dict):
            raise AppServerError("sandbox_policy must be a native app-server sandbox policy object")
        self._output_handlers[process_id] = on_output
        try:
            params={"command":command,"cwd":cwd,"processId":process_id,"streamStdin":stream_stdin,"streamStdoutStderr":stream_stdout_stderr,"tty":tty}
            if timeout_ms is not None: params["timeoutMs"]=timeout_ms
            if output_cap is not None: params["outputBytesCap"]=output_cap
            if disable_timeout is not None: params["disableTimeout"]=disable_timeout
            if disable_output_cap is not None: params["disableOutputCap"]=disable_output_cap
            if sandbox_policy is not None: params["sandboxPolicy"]=sandbox_policy
            # Native no-timeout semantics should not become a broker timeout.
            return self.request("command/exec",params,timeout=((timeout_ms / 1000) + 30) if timeout_ms is not None else None,on_sent=on_request_sent)
        finally:
            self._output_handlers.pop(process_id, None)

    def write(self, process_id: str, data: bytes, close_stdin=False):
        params = {"processId": process_id, "closeStdin": bool(close_stdin)}
        if data:
            params["deltaBase64"] = base64.b64encode(data).decode("ascii")
        return self.request("command/exec/write", params)

    def terminate(self, process_id: str):
        return self.request("command/exec/terminate", {"processId": process_id})

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try: stream.close()
            except OSError: pass
        for thread in self._threads: thread.join(timeout=1)
        self.proc = None
