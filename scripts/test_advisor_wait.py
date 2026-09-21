import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock

from advisor_wait import (WaitError, conversation_id, evaluate, main, parse_probe, queue_wake,
                          set_ownership, wake_message)


FAKE_ASIDE = """\
    #!/usr/bin/env python3
    import json, pathlib, sys
    root = pathlib.Path(__file__).resolve().parent
    sequence = json.loads((root / "probe_sequence.json").read_text())
    counter = root / "probe_counter"
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    entry = sequence[min(index, len(sequence) - 1)]
    print(json.dumps(entry))
    print("[ok | 12ms]")
    """

FAKE_CODEX = """\
    #!/usr/bin/env python3
    import json, pathlib, sys
    root = pathlib.Path(__file__).resolve().parent
    (root / "codex_argv.json").write_text(json.dumps(sys.argv[1:]))
    if (root / "codex_fail").exists():
        print("Error: failed to queue session message: thread/queue/add failed", file=sys.stderr)
        raise SystemExit(1)
    thread = sys.argv[sys.argv.index("--thread") + 1]
    print("Queued message 01a0c000-0000-7000-8000-000000000000 for thread " + thread + ".")
    """


def probe_entry(text_hash, count=2, length=12, stop=False, text="advisor response body"):
    return {"ok": True, "url": "https://chatgpt.com/c/6ab0b48e-d320-83ee-a78b-f0f750a03799",
            "assistantCount": count, "lastLen": length, "lastHash": text_hash,
            "stopButton": stop, "composerText": "", "text": text}


class AdvisorWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aside = self.root / "fake-aside"
        self.codex = self.root / "fake-codex"
        self.aside.write_text(textwrap.dedent(FAKE_ASIDE))
        self.codex.write_text(textwrap.dedent(FAKE_CODEX))
        self.aside.chmod(0o755)
        self.codex.chmod(0o755)
        self.sequence = self.root / "probe_sequence.json"
        self.out = self.root / "response.md"
        self.status = self.root / "status.json"

    def tearDown(self):
        self.tmp.cleanup()

    def set_sequence(self, entries):
        self.sequence.write_text(json.dumps(entries))

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def test_conversation_id_accepts_url_and_bare_id(self):
        self.assertEqual(conversation_id("https://chatgpt.com/c/6ab0b48e-d320-83ee-a78b-f0f750a03799"),
                         "6ab0b48e-d320-83ee-a78b-f0f750a03799")
        self.assertEqual(conversation_id(" 6ab0b48e-d320-83ee-a78b-f0f750a03799 "),
                         "6ab0b48e-d320-83ee-a78b-f0f750a03799")
        with self.assertRaises(WaitError):
            conversation_id("not a conversation")
        with self.assertRaises(WaitError):
            conversation_id("6ab0b48e-d320-83ee-a78b-f0f750a03799-not-an-id!")

    def test_parse_probe_ignores_repl_timing_line(self):
        stdout = '{"ok": true, "lastHash": "aa"}\n[ok | 12ms]\n'
        self.assertEqual(parse_probe(stdout)["lastHash"], "aa")
        with self.assertRaises(WaitError):
            parse_probe("[ok | 12ms]\n")

    def test_evaluate_waits_for_streaming_and_stability(self):
        done, reason = evaluate(probe_entry("aa", stop=True), None, None, "aa", 3, 2)
        self.assertFalse(done)
        self.assertEqual(reason, "streaming")
        done, reason = evaluate(probe_entry("aa"), "bb", 1, "aa", 1, 2)
        self.assertFalse(done)
        self.assertEqual(reason, "stabilizing")
        done, reason = evaluate(probe_entry("aa"), "bb", 1, "aa", 2, 2)
        self.assertTrue(done)
        self.assertEqual(reason, "captured")

    def test_evaluate_does_not_capture_the_already_read_message(self):
        done, reason = evaluate(probe_entry("aa"), "aa", 2, "aa", 5, 2)
        self.assertFalse(done)
        self.assertEqual(reason, "no_new_message")
        done, reason = evaluate(probe_entry("aa", count=3), "aa", 2, "aa", 5, 2)
        self.assertTrue(done)

    def test_evaluate_reports_missing_tab(self):
        done, reason = evaluate({"ok": False, "error": "tab_not_found"}, "aa", 1, "aa", 2, 2)
        self.assertFalse(done)
        self.assertEqual(reason, "tab_not_found")

    def test_evaluate_rejects_malformed_probe_counts(self):
        result = probe_entry("aa")
        result["assistantCount"] = "not-a-number"
        done, reason = evaluate(result, "bb", 1, "aa", 2, 2)
        self.assertFalse(done)
        self.assertEqual(reason, "invalid_probe_result")

    def test_wake_message_names_round_response_and_state(self):
        message = wake_message("R007", Path("/tmp/round/response.md"), "abc123",
                               Path("/tmp/ADVISOR_STATE.md"), "2026-09-21T19:00:00+09:00")
        self.assertIn("R007", message)
        self.assertIn("/tmp/round/response.md", message)
        self.assertIn("abc123", message)
        self.assertIn("/tmp/ADVISOR_STATE.md", message)
        self.assertIn("Do not resend the packet", message)

    def test_queue_wake_passes_argv_without_a_shell(self):
        receipt = queue_wake("01a0c1c3-7c29-7d10-8a2d-e09d930e81ae", "wake text",
                             codex_bin=str(self.codex))
        self.assertTrue(receipt["ok"])
        argv = json.loads((self.root / "codex_argv.json").read_text())
        self.assertEqual(argv[:2], ["queue", "--thread"])
        self.assertEqual(argv[2], "01a0c1c3-7c29-7d10-8a2d-e09d930e81ae")
        self.assertEqual(argv[3:5], ["--message", "wake text"])

    def test_queue_wake_reports_failure(self):
        (self.root / "codex_fail").write_text("1")
        receipt = queue_wake("thread-id", "wake text", codex_bin=str(self.codex))
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIn("thread/queue/add failed", receipt["stderr"])

    def test_ownership_requires_a_capture_or_live_polling_watcher(self):
        status = {"phase": "starting", "response_sha256": None}
        set_ownership(status, process_alive=True)
        self.assertFalse(status["ownership"]["owned"])
        status["phase"] = "polling"
        set_ownership(status, process_alive=True)
        self.assertEqual(status["ownership"]["owner"], "watcher")
        set_ownership(status, process_alive=False)
        self.assertFalse(status["ownership"]["owned"])
        status.update(phase="captured", response_sha256="abc123")
        set_ownership(status, process_alive=False)
        self.assertEqual(status["ownership"]["owner"], "response_capture")

    def test_wait_captures_the_response_after_it_stabilizes(self):
        # Poll 1 still shows the previous answer, poll 2 starts the new one, poll 3 confirms it settled.
        self.set_sequence([probe_entry("aa"), probe_entry("cc", text="final answer")])
        code = self.run_cli(["wait", "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                             "--out", str(self.out), "--round", "R001", "--timeout", "30",
                             "--poll-interval", "0", "--stable-polls", "2",
                             "--baseline-hash", "aa", "--baseline-count", "1",
                             "--status-file", str(self.status), "--aside-bin", str(self.aside)])
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(), "final answer")
        status = json.loads(self.status.read_text())
        self.assertEqual(status["phase"], "captured")
        self.assertEqual(status["polls"], 3)
        self.assertEqual(status["receipt"]["response_bytes"], len(b"final answer"))
        self.assertEqual(status["receipt"]["assistant_text_hash"], "cc")
        self.assertEqual(status["ownership"]["owner"], "response_capture")

    def test_wait_times_out_without_a_new_message(self):
        self.set_sequence([probe_entry("aa")])
        code = self.run_cli(["wait", "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                             "--out", str(self.out), "--round", "R001", "--timeout", "0",
                             "--poll-interval", "0", "--baseline-hash", "aa", "--baseline-count", "2",
                             "--status-file", str(self.status), "--aside-bin", str(self.aside)])
        self.assertEqual(code, 3)
        self.assertFalse(self.out.exists())
        self.assertEqual(json.loads(self.status.read_text())["phase"], "timeout")

    def test_wait_rejects_a_shared_response_and_status_path(self):
        code = self.run_cli([
            "wait", "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
            "--out", str(self.out), "--round", "R001",
            "--status-file", str(self.out),
        ])
        self.assertEqual(code, 2)
        self.assertFalse(self.out.exists())

    def test_watch_writes_the_response_and_queues_the_wake(self):
        self.set_sequence([probe_entry("bb", text="round answer")])
        code = self.run_cli(["watch", "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                             "--out", str(self.out), "--round", "R002", "--timeout", "30",
                             "--poll-interval", "0", "--stable-polls", "1",
                             "--baseline-hash", "aa", "--baseline-count", "1",
                             "--thread", "01a0c1c3-7c29-7d10-8a2d-e09d930e81ae",
                             "--state", str(self.root / "ADVISOR_STATE.md"),
                             "--status-file", str(self.status), "--aside-bin", str(self.aside),
                             "--codex-bin", str(self.codex)])
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(), "round answer")
        status = json.loads(self.status.read_text())
        self.assertTrue(status["wake"]["ok"])
        self.assertEqual(status["wake"]["thread_id"], "01a0c1c3-7c29-7d10-8a2d-e09d930e81ae")
        argv = json.loads((self.root / "codex_argv.json").read_text())
        self.assertIn("R002", argv[argv.index("--message") + 1])
        self.assertIn(str(self.out), argv[argv.index("--message") + 1])

    def test_watch_records_a_failed_wake_instead_of_losing_the_response(self):
        (self.root / "codex_fail").write_text("1")
        self.set_sequence([probe_entry("bb", text="round answer")])
        code = self.run_cli(["watch", "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                             "--out", str(self.out), "--round", "R002", "--timeout", "30",
                             "--poll-interval", "0", "--stable-polls", "1",
                             "--baseline-hash", "aa", "--baseline-count", "1",
                             "--thread", "01a0c1c3-7c29-7d10-8a2d-e09d930e81ae",
                             "--status-file", str(self.status), "--aside-bin", str(self.aside),
                             "--codex-bin", str(self.codex)])
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_text(), "round answer")
        status = json.loads(self.status.read_text())
        self.assertFalse(status["wake"]["ok"])
        self.assertEqual(status["phase"], "captured")

    def test_detach_does_not_overwrite_child_polling_status(self):
        polling = {"phase": "polling", "pid": 43210, "response_sha256": None}

        def start_child(*args, **kwargs):
            self.status.write_text(json.dumps(polling))
            return mock.Mock(pid=43210)

        with mock.patch("advisor_wait.subprocess.Popen", side_effect=start_child):
            code = self.run_cli([
                "watch", "--detach",
                "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                "--out", str(self.out), "--round", "R003",
                "--thread", "01a0c1c3-7c29-7d10-8a2d-e09d930e81ae",
                "--status-file", str(self.status),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.status.read_text())["phase"], "polling")

    def test_watch_requires_thread_before_starting_detached_process(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("advisor_wait.subprocess.Popen") as popen:
            code = self.run_cli([
                "watch", "--detach",
                "--conversation", "6ab0b48e-d320-83ee-a78b-f0f750a03799",
                "--out", str(self.out), "--round", "R003",
                "--status-file", str(self.status),
            ])
        self.assertEqual(code, 2)
        popen.assert_not_called()

    def test_status_reports_liveness(self):
        self.status.write_text(json.dumps({"phase": "polling", "pid": os.getpid()}))
        self.assertEqual(self.run_cli(["status", "--status-file", str(self.status)]), 0)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["status", "--status-file", str(self.status)]), 0)
        reported = json.loads(output.getvalue())
        self.assertTrue(reported["ownership"]["owned"])
        self.assertEqual(reported["ownership"]["owner"], "watcher")
        missing = self.root / "absent.json"
        self.assertEqual(self.run_cli(["status", "--status-file", str(missing)]), 3)


if __name__ == "__main__":
    unittest.main()
