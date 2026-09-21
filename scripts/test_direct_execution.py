"""Native direct-controller regressions without profile, runtime, or session setup."""
from __future__ import annotations

import base64
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import direct_execution
from direct_execution import DirectExecutionController, DirectExecutionError, _Control, _read, _sha, run_operation
from workspace_registry import Registry


class _Proc:
    pid = 424242


class _Server:
    def __init__(self, *_):
        self.sent_methods = []
        self.result = {"exitCode": 0}

    def start(self):
        self.sent_methods.extend(["initialize", "initialized"])

    def exec(self, *, on_output, **_):
        self.sent_methods.append("command/exec")
        on_output("stdout", b"\x00native-out\xff", False)
        on_output("stderr", b"native-err", False)
        return self.result

    def stop(self):
        pass


class NativeDirectControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "alpha"; self.root.mkdir()
        (self.root / "input.txt").write_text("input\n")
        (self.base / "policy.json").write_text(json.dumps({"version": 1, "files": ["input.txt"]}))
        codex = self.base / "codex"; codex.write_text("#!/bin/sh\n"); codex.chmod(0o755)
        self.registry = Registry({
            "version": 1, "default_workspace_id": "alpha", "default_worker_profile": "worker",
            "state_dir": ".state/orchestrator", "codex_bin": str(codex),
            "model_capabilities": {"test-model": ["high"]},
            "worker_profiles": [{"id": "worker", "model": "test-model", "reasoning_effort": "high"}],
            "workspaces": [{"id": "alpha", "label": "Alpha", "root": "alpha", "enabled": True, "context_policy": "policy.json"}],
        }, self.base)
        self.controller = DirectExecutionController(self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, operation_id="operation", command=None):
        command = command or ["/usr/bin/printf", "native"]
        with mock.patch.object(direct_execution.subprocess, "Popen", return_value=_Proc()):
            return self.controller.exec_command("alpha", operation_id, command)

    def test_direct_selection_has_no_profile_runtime_or_session_provenance(self):
        receipt = self.start()
        self.assertEqual(receipt["executor_kind"], "direct")
        self.assertIsNone(receipt["worker_model"])
        self.assertIsNone(receipt["codex_thread_id"])
        for field in ("execution_id", "execution_profile_id", "runtime_id", "worker_profile_id", "write_mode"):
            self.assertNotIn(field, receipt)

    def test_exact_retry_is_idempotent_and_changed_bytes_conflict_without_extra_runner(self):
        with mock.patch.object(direct_execution.subprocess, "Popen", return_value=_Proc()) as popen:
            first = self.controller.exec_command("alpha", "same", ["/usr/bin/printf", "one"])
            duplicate = self.controller.exec_command("alpha", "same", ["/usr/bin/printf", "one"])
            self.assertTrue(duplicate["duplicate_submission"])
            self.assertEqual(first["request_sha256"], duplicate["request_sha256"])
            self.assertEqual(popen.call_count, 1)
            with self.assertRaises(DirectExecutionError) as caught:
                self.controller.exec_command("alpha", "same", ["/usr/bin/printf", "two"])
        self.assertEqual(caught.exception.result["error"]["code"], "idempotency_conflict")

    def test_runner_retains_exact_binary_streams_and_only_command_rpc_methods(self):
        self.start("binary")
        directory = self.controller._dir("alpha", "binary")
        with mock.patch.object(direct_execution, "CodexAppServer", _Server):
            run_operation(directory, self.registry.codex_bin)
        receipt = self.controller.get_execution("alpha", "binary")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(base64.b64decode(receipt["stdout_base64"]), b"\x00native-out\xff")
        self.assertEqual(base64.b64decode(receipt["stderr_base64"]), b"native-err")
        self.assertEqual(receipt["output_encoding"], "base64")
        raw = _read(directory / "receipt.json")
        self.assertEqual(raw["native_result"], {"exitCode": 0})
        self.assertFalse(Path(raw["control_socket"]).exists(), "operation control socket leaked after completion")
        with self.assertRaises(DirectExecutionError) as caught:
            self.controller.read_artifact("alpha", "binary", "input.txt")
        self.assertEqual(caught.exception.result["error"]["code"], "scope_violation")

    def test_non_integer_or_nonzero_native_exit_never_succeeds(self):
        for value in (False, 0.0, "0", 7, None):
            with self.subTest(value=value):
                operation_id = "exit" + str(len(str(value))) + _sha(repr(value).encode())[:4]
                self.start(operation_id)
                directory = self.controller._dir("alpha", operation_id)
                server = _Server(); server.result = {} if value is None else {"exitCode": value}
                with mock.patch.object(direct_execution, "CodexAppServer", return_value=server):
                    run_operation(directory, self.registry.codex_bin)
                self.assertEqual(self.controller.get_execution("alpha", operation_id)["status"], "failed")

    def test_output_stream_cursors_do_not_skip_shorter_stream_late_bytes(self):
        self.start("cursors")
        directory = self.controller._dir("alpha", "cursors")
        (directory / "stdout.bin").write_bytes(b"stdout-8")
        (directory / "stderr.bin").write_bytes(b"e" * 110)
        first = self.controller.get_execution("alpha", "cursors")
        self.assertEqual(base64.b64decode(first["stdout_base64"]), b"stdout-8")
        self.assertEqual(base64.b64decode(first["stderr_base64"]), b"e" * 110)
        with (directory / "stdout.bin").open("ab") as stdout:
            stdout.write(b"-late")
        second = self.controller.get_execution(
            "alpha", "cursors", stdout_offset=first["stdout_next_offset"],
            stderr_offset=first["stderr_next_offset"],
        )
        self.assertEqual(base64.b64decode(second["stdout_base64"]), b"-late")
        self.assertEqual(base64.b64decode(second["stderr_base64"]), b"")

    def test_server_cleanup_and_terminal_receipt_hold_workspace_lease(self):
        self.start("lease")
        directory = self.controller._dir("alpha", "lease")
        observed = {}

        class LockProbeServer(_Server):
            def stop(self):
                observed["status_during_stop"] = _read(directory / "receipt.json")["status"]
                with Path(_read(directory / "receipt.json")["writer_lock"]).open("a+b") as contender:
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        observed["lease_held"] = True
                    else:
                        observed["lease_held"] = False
                        fcntl.flock(contender, fcntl.LOCK_UN)
                super().stop()

        with mock.patch.object(direct_execution, "CodexAppServer", LockProbeServer):
            run_operation(directory, self.registry.codex_bin)
        self.assertTrue(observed["lease_held"])
        self.assertNotIn(observed["status_during_stop"], direct_execution.TERMINAL)
        self.assertEqual(self.controller.get_execution("alpha", "lease")["status"], "succeeded")

    def test_queued_stdin_drains_in_submission_order(self):
        self.start("stdin-order")
        first = base64.b64encode(b"first\x00").decode()
        second = base64.b64encode(b"second\xff").decode()
        self.controller.write_stdin("alpha", "stdin-order", "first", first, False)
        self.controller.write_stdin("alpha", "stdin-order", "second", second, True)
        directory = self.controller._dir("alpha", "stdin-order")

        class Receiver:
            def __init__(self):
                self.writes = []
            def write(self, process_id, data, close_stdin):
                self.writes.append((process_id, data, close_stdin))
                return {}

        receiver = Receiver()
        socket_path = _read(directory / "receipt.json")["control_socket"]
        control = _Control(directory, receiver, "direct-test", socket_path)
        control.sent.set()
        try:
            drained = control.drain()
        finally:
            control.close()
        self.assertEqual([entry[1:] for entry in receiver.writes], [(b"first\x00", False), (b"second\xff", True)])
        self.assertEqual(list(drained), ["first", "second"])

    def test_stdin_and_cancel_are_durable_native_operations(self):
        # This is deliberately public-controller level: a future in-memory shortcut would lose
        # idempotency after process restart and would not satisfy the MCP contract.
        self.start("stdin", ["/usr/bin/python3", "-c", "import sys;print(sys.stdin.read(),end='')"])
        encoded = base64.b64encode(b"native-stdin\n").decode()
        first = self.controller.write_stdin("alpha", "stdin", "chunk", encoded, True)
        duplicate = self.controller.write_stdin("alpha", "stdin", "chunk", encoded, True)
        self.assertTrue(duplicate["duplicate_submission"])
        self.assertEqual(first["request_sha256"], duplicate["request_sha256"])
        with self.assertRaises(DirectExecutionError) as caught:
            self.controller.write_stdin("alpha", "stdin", "chunk", base64.b64encode(b"changed").decode(), True)
        self.assertEqual(caught.exception.result["error"]["code"], "idempotency_conflict")
        cancelled = self.controller.cancel_execution("alpha", "stdin")
        self.assertEqual(cancelled["status"], "cancel_requested")


if __name__ == "__main__":
    unittest.main()
