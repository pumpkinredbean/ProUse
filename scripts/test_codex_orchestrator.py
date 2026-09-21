import json
import os
from pathlib import Path
import tempfile
import textwrap
import time
import unittest

from codex_orchestrator import OrchestratorError, TaskBroker




class CodexOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "allowed").mkdir()
        (self.root / "allowed/input.md").write_text("input\n")
        self.fake = self.root / "fake-codex"
        self.fake.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, pathlib, sys
            args = sys.argv[1:]
            assert args[args.index('--model') + 1] == 'gpt-5.6-sol'
            assert 'default_permissions="broker-task"' in args
            assert 'permissions.broker-task.network.enabled=false' in args
            assert args[-1] == '-'
            prompt = sys.stdin.read()
            output = pathlib.Path(args[args.index('--output-last-message') + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            (output.parent / 'captured-prompt.txt').write_text(prompt)
            pathlib.Path('allowed/output.md').write_text('worker result\\n')
            output.write_text(json.dumps({
                'summary': 'completed fake task',
                'deliverables': ['allowed/output.md'],
                'validations': ['fake validation exit 0'],
                'blockers': []
            }))
            print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-gpt56sol-test'}))
            print(json.dumps({'type': 'turn_context', 'payload': {'model': 'gpt-5.6-sol', 'effort': 'xhigh'}}))
            print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}))
        """))
        self.fake.chmod(0o755)
        self.broker = TaskBroker(self.root, self.root / ".state/orchestrator", self.fake)
        self.request = {
            "orchestrator_task_id": "R011-test",
            "objective": "Create the requested bounded worker output.",
            "deliverables": ["allowed/output.md"],
            "validation": ["Report fake validation."],
            "allowed_paths": ["allowed"],
            "write_mode": "workspace_write",
            "max_seconds": 60,
            "source_refs": ["allowed/input.md"],
        }

    def tearDown(self):
        if self.broker._paths("R011-test")[2].exists():
            self.broker.get("R011-test", wait_seconds=10)
        self.tmp.cleanup()

    def test_independent_fixed_model_task_and_receipt(self):
        submitted = self.broker.submit(self.request)
        self.assertEqual(submitted["worker_model"], "gpt-5.6-sol")
        deadline = time.monotonic() + 10
        while True:
            result = self.broker.get("R011-test", wait_seconds=1)
            if result["status"] in {"succeeded", "failed", "timed_out", "scope_violation"}:
                break
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["codex_thread_id"], "thread-gpt56sol-test")
        self.assertEqual(result["changed_paths"], ["allowed/output.md"])
        self.assertEqual(result["unexpected_changed_paths"], [])
        self.assertEqual(result["summary"], "completed fake task")
        captured = next((self.root / ".state/orchestrator/tasks").glob("*/captured-prompt.txt")).read_text()
        self.assertIn("upper orchestrator owns research judgment", captured)
        self.assertIn('"worker_model": "gpt-5.6-sol"', captured)

    def test_retry_is_idempotent_and_changed_request_is_rejected(self):
        self.broker.submit(self.request)
        second = self.broker.submit(self.request)
        self.assertTrue(second["duplicate_submission"])
        changed = dict(self.request, objective="different task bytes")
        with self.assertRaises(OrchestratorError):
            self.broker.submit(changed)

    def test_boundaries_are_validated_before_launch(self):
        for path in ("../outside", "/etc", ".state/private", "allowed/../outside", "."):
            with self.subTest(path=path):
                request = dict(self.request, orchestrator_task_id=f"bad-{abs(hash(path))}", allowed_paths=[path])
                with self.assertRaises(OrchestratorError):
                    self.broker.submit(request)


if __name__ == "__main__":
    unittest.main()
