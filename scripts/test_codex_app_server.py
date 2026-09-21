"""Regression tests for the command-only Codex app-server adapter."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from codex_app_server import ALLOWED_METHODS, AppServerError, CodexAppServer


class _Input:
    def __init__(self):
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        pass


class _Process:
    def __init__(self, stdout=()):
        self.stdin = _Input()
        self.stdout = iter(stdout)
        self.stderr = ()

    def poll(self):
        return None


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = CodexAppServer(Path("/bin/false"), Path(self.tmp.name), Path(self.tmp.name) / "private", [])
        self.server.proc = _Process()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_command_lifecycle_methods_can_be_emitted(self):
        self.assertEqual(ALLOWED_METHODS, {"initialize", "initialized", "command/exec", "command/exec/write", "command/exec/terminate"})
        for method in ("thread/start", "turn/start", "model/list", "review/start", "process/spawn", "shell/exec"):
            with self.subTest(method=method), self.assertRaises(AppServerError):
                self.server.request(method, {})
        self.server.notify("initialized", {})
        self.assertEqual(self.server.sent_methods, ["initialized"])
        self.assertNotIn("thread", " ".join(self.server.sent_methods))

    def test_response_error_and_timeout_remove_pending_requests(self):
        def error_response():
            while not self.server._pending:
                time.sleep(.001)
            self.server._pending[1].put({"id": 1, "error": {"message": "denied"}})

        sender = threading.Thread(target=error_response)
        sender.start()
        with self.assertRaises(AppServerError):
            self.server.request("command/exec", {}, timeout=.5)
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertEqual(self.server._pending, {})

        with self.assertRaises(AppServerError):
            self.server.request("command/exec", {}, timeout=.01)
        self.assertEqual(self.server._pending, {})

    def test_transport_failure_never_blocks_on_full_pending_queue(self):
        # A response may race transport teardown.  Teardown must not deadlock the reader merely
        # because a one-slot waiter already contains that response.
        pending = queue.Queue(maxsize=1)
        pending.put({"id": 1, "result": {}})
        self.server._pending[1] = pending
        thread = threading.Thread(target=self.server._fail_transport, args=("malformed",))
        thread.start()
        thread.join(.5)
        self.assertFalse(thread.is_alive(), "transport teardown blocked on a full pending queue")
        self.assertIsInstance(self.server._transport_error, AppServerError)

    def test_malformed_stdout_and_bad_output_delta_fail_closed_without_pending_leaks(self):
        for line in ("not-json\n", json.dumps({"method": "command/exec/outputDelta", "params": {"processId": "p", "stream": "stdout", "deltaBase64": "%%%"}}) + "\n"):
            with self.subTest(line=line):
                server = CodexAppServer(Path("/bin/false"), Path(self.tmp.name), Path(self.tmp.name) / "private2", [])
                server.proc = _Process([line])
                server._read_stdout()
                self.assertIsInstance(server._transport_error, AppServerError)
                self.assertEqual(server._pending, {})

    def test_binary_output_delta_is_decoded_exactly(self):
        received = []
        payload = b"\x00stdout\xff"
        line = json.dumps({"method": "command/exec/outputDelta", "params": {"processId": "p", "stream": "stderr", "deltaBase64": base64.b64encode(payload).decode(), "capReached": True}}) + "\n"
        self.server.proc = _Process([line])
        self.server._output_handlers["p"] = lambda stream, data, capped: received.append((stream, data, capped))
        self.server._read_stdout()
        self.assertEqual(received, [("stderr", payload, True)])
        # EOF is a transport failure only after the valid notification was delivered.
        self.assertIsInstance(self.server._transport_error, AppServerError)

    def test_exec_uses_native_workspace_write_and_keeps_buffered_result_exact(self):
        result = {"exitCode": 0, "stdout": "final stdout\x00", "stderr": "final stderr"}
        with mock.patch.object(self.server, "request", return_value=result) as request:
            actual = self.server.exec(
                process_id="op-1", command=["/usr/bin/touch", "written.txt"], cwd=self.tmp.name,
                sandbox_policy={"type": "dangerFullAccess"},
                timeout_ms=123, output_cap=456, disable_timeout=False, disable_output_cap=False,
                stream_stdin=False, stream_stdout_stderr=False, tty=False,
            )
        self.assertIs(actual, result)
        method, params = request.call_args.args[:2]
        self.assertEqual(method, "command/exec")
        self.assertEqual(params["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertNotIn("permissionProfile", params)
        self.assertEqual(params["timeoutMs"], 123)
        self.assertEqual(params["outputBytesCap"], 456)
        self.assertFalse(params["streamStdin"])
        self.assertFalse(params["streamStdoutStderr"])

    def test_exec_rejects_legacy_permission_profile_and_non_object_sandbox(self):
        with self.assertRaises(AppServerError):
            self.server.exec(process_id="p", command=["/usr/bin/true"], cwd=self.tmp.name, permission_profile="workspaceWrite")
        with self.assertRaises(AppServerError):
            self.server.exec(process_id="p", command=["/usr/bin/true"], cwd=self.tmp.name, sandbox_policy="workspaceWrite")


if __name__ == "__main__":
    unittest.main()
