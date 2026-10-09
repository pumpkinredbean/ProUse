from pathlib import Path
import re
import time
import unittest
from unittest import mock

from support import WorkspaceCase

from prouse.tools import ToolError, shell


class ShellTests(WorkspaceCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.jobs = shell.Jobs(self.output)
        self.addCleanup(self.jobs.shutdown)

    async def bash(self, command, cwd=None, timeout=None):
        return await shell.bash(self.workspaces, self.jobs, command, cwd, timeout)

    async def test_runs_in_the_default_workspace(self):
        result = await self.bash("echo 'test output'; pwd")
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, f"test output\n{self.project}")
        (self.project / "sub").mkdir()
        self.assertEqual((await self.bash("pwd", cwd="sub")).text, str(self.project / "sub"))

    async def test_exit_codes_and_empty_output(self):
        result = await self.bash("echo out; exit 3")
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "out\n\nCommand exited with code 3")
        self.assertEqual((await self.bash("true")).text, "(no output)")
        self.assertEqual((await self.bash("kill -9 $$")).text, "Command exited with code 137")

    async def test_stdin_is_closed(self):
        self.assertEqual((await self.bash("cat; echo done")).text, "done")

    async def test_timeout(self):
        started = time.monotonic()
        result = await self.bash("echo begin; sleep 10", timeout=0.3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result.is_error)
        self.assertEqual(result.text, "begin\n\nCommand timed out after 0.3 seconds")

    async def test_long_output_keeps_the_tail_and_saves_the_rest(self):
        result = await self.bash("seq 1 4000")
        self.assertTrue(result.text.startswith("2001\n"))
        match = re.search(r"\[Showing the last 2000 of 4000 lines\. Full output: (.+)\]$", result.text)
        self.assertIsNotNone(match, result.text[-200:])
        self.assertEqual(Path(match.group(1)).read_text(), "".join(f"{n}\n" for n in range(1, 4001)))

    async def test_short_output_logs_are_removed(self):
        await self.bash("echo hi")
        self.assertEqual(list(self.output.glob("bash-*.log")), [])

    async def test_slow_commands_continue_as_jobs(self):
        with mock.patch.object(shell, "YIELD_SECONDS", 0.3):
            first = await self.bash("echo start; sleep 1; echo end")
            match = re.search(r"Still running after \d+s as job (\d+)\. Call bash_job with job=\1", first.text)
            self.assertIsNotNone(match, first.text)
            self.assertTrue(first.text.startswith("start\n\n"))
            job = int(match.group(1))
            for _ in range(20):
                result = await shell.bash_job(self.jobs, job, wait=1)
                if "finished" in result.text:
                    break
            self.assertRegex(result.text, rf"^end\n\n\[Job {job} finished after \d+s with exit code 0\]$")
            with self.assertRaisesRegex(ToolError, f"No bash job {job}"):
                await shell.bash_job(self.jobs, job)

    async def test_jobs_can_be_stopped(self):
        with mock.patch.object(shell, "YIELD_SECONDS", 0.2):
            first = await self.bash("sleep 30")
            job = int(re.search(r"as job (\d+)", first.text).group(1))
            started = time.monotonic()
            result = await shell.bash_job(self.jobs, job, kill=True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn(f"[Job {job} finished after", result.text)
        self.assertIn("Command was stopped", result.text)

    async def test_running_output_is_never_split_inside_a_character(self):
        with mock.patch.object(shell, "YIELD_SECONDS", 0.4):
            first = await self.bash(r"printf 'a\342\202'; sleep 1; printf '\254b\n'")
            job = int(re.search(r"as job (\d+)", first.text).group(1))
            self.assertTrue(first.text.startswith("a\n\n"), first.text)
            for _ in range(20):
                result = await shell.bash_job(self.jobs, job, wait=1)
                if "finished" in result.text:
                    break
        self.assertTrue(result.text.startswith("\u20acb\n"), result.text)

    async def test_shutdown_stops_running_jobs(self):
        with mock.patch.object(shell, "YIELD_SECONDS", 0.1):
            first = await self.bash("sleep 30")
        job = self.jobs.get(int(re.search(r"as job (\d+)", first.text).group(1)))
        self.jobs.shutdown()
        self.assertTrue(job.done.is_set())

    async def test_invalid_requests(self):
        with self.assertRaisesRegex(ToolError, "command must not be empty"):
            await self.bash("  ")
        with self.assertRaisesRegex(ToolError, "Invalid timeout"):
            await self.bash("true", timeout=0)
        with self.assertRaisesRegex(ToolError, "Working directory does not exist"):
            await self.bash("true", cwd="missing")
        with self.assertRaisesRegex(ToolError, "outside the registered workspaces"):
            await self.bash("true", cwd=str(self.base))
        with self.assertRaisesRegex(ToolError, "No bash job 99. Running jobs: none."):
            await shell.bash_job(self.jobs, 99)


if __name__ == "__main__":
    unittest.main()
