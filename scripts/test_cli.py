import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from prouse import cli
from prouse.configuration import Configuration
from prouse.errors import ExitCode
from prouse.integrations import mcp, services
from prouse.runtime.admin import AdminRuntime
from prouse.state import InstancePaths, atomic_json


ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = os.pathsep.join((str(ROOT), str(ROOT / "scripts")))


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "ProUse home"
        self.workspace = self.base / "project with spaces"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("hello from project\n")
        self.env = mock.patch.dict(os.environ, {"PROUSE_HOME": str(self.home)}, clear=False)
        self.env.start()
        self.paths = InstancePaths.from_environment()
        self.config = Configuration(self.paths)
        self.runtime = AdminRuntime(self.config)

    def tearDown(self):
        record, _ = self.runtime.instance.current()
        if record:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["stop", "--timeout", "3"])
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def setup(self, port=None):
        args = ["setup", "--workspace", str(self.workspace), "--no-input", "--host", "127.0.0.1", "--json"]
        if port:
            args += ["--port", str(port)]
        code, output, _ = self.run_cli(args)
        self.assertEqual(code, 0, output)
        return json.loads(output)

    def process(self, *args):
        env = dict(os.environ, PYTHONPATH=PYTHONPATH, PROUSE_HOME=str(self.home))
        return subprocess.Popen([sys.executable, "-m", "prouse.cli", *args], cwd=self.base,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_ready(self, process, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.runtime.status()
            if result["admin_ready"]:
                return result
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"start exited {process.returncode}: {stdout} {stderr}")
            time.sleep(.05)
        self.fail("Admin did not become ready")

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_setup_twice_preserves_existing_configuration(self):
        first = self.setup()
        registry_path = Path(first["registry"])
        registry = json.loads(registry_path.read_text())
        registry["workspaces"][0]["label"] = "User preserved label"
        atomic_json(registry_path, registry)
        settings = self.config.settings_document()
        settings["user_setting"] = "keep"
        atomic_json(self.paths.settings, settings)

        second = self.setup()
        self.assertFalse(second["workspace_added"])
        after = json.loads(registry_path.read_text())
        self.assertEqual(after["workspaces"][0]["label"], "User preserved label")
        self.assertEqual(self.config.settings_document()["user_setting"], "keep")
        self.assertIn(".state", Path(after["state_dir"]).parts)

    def test_noninteractive_missing_input_and_json_exit_codes(self):
        code, output, _ = self.run_cli(["setup", "--no-input", "--json"])
        self.assertEqual(code, ExitCode.USAGE)
        self.assertEqual(json.loads(output)["exit_code"], ExitCode.USAGE)
        code, output, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, ExitCode.NOT_RUNNING)
        self.assertEqual(json.loads(output)["status"], "stopped")

    def test_setup_bad_path_or_port_does_not_create_configuration(self):
        for extra in (["--workspace", str(self.base / "missing")],
                      ["--workspace", str(self.workspace), "--port", "0"]):
            code, output, _ = self.run_cli(["setup", "--no-input", "--json", *extra])
            self.assertEqual(code, ExitCode.USAGE, output)
            self.assertEqual(json.loads(output)["status"], "error")
            self.assertFalse(self.paths.registry.exists())

    def test_start_survives_launcher_and_restart_defaults_to_background(self):
        self.setup(self.free_port())
        launcher = self.process("start", "--json")
        stdout, stderr = launcher.communicate(timeout=10)
        self.assertEqual(launcher.returncode, 0, stderr)
        first = json.loads(stdout)
        self.assertTrue(first["admin_ready"])
        self.assertNotEqual(first["pid"], launcher.pid)
        self.assertTrue(self.runtime.status()["admin_ready"])
        duplicate = self.process("start", "--json")
        stdout, stderr = duplicate.communicate(timeout=10)
        self.assertEqual(duplicate.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["pid"], first["pid"])

        restarted = self.process("restart", "--json")
        stdout, stderr = restarted.communicate(timeout=10)
        self.assertEqual(restarted.returncode, 0, stderr)
        second = json.loads(stdout)  # Exactly one JSON object, including stop/start.
        self.assertTrue(second["admin_ready"])
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(self.run_cli(["stop"])[0], 0)
        self.assertEqual(self.run_cli(["status", "--json"])[0], ExitCode.NOT_RUNNING)

    def test_background_port_conflict_is_reported_to_launcher(self):
        port = self.free_port()
        self.setup(port)
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", port))
            blocker.listen()
            launcher = self.process("start", "--json")
            stdout, stderr = launcher.communicate(timeout=10)
        self.assertEqual(launcher.returncode, ExitCode.ERROR, stderr)
        self.assertEqual(json.loads(stdout)["status"], "error")
        self.assertIn("Cannot start Admin", self.paths.log.read_text())
        self.assertFalse(self.runtime.status()["running"])

    def test_mcp_config_and_real_check_use_the_selected_instance(self):
        setup = self.setup()
        code, output, _ = self.run_cli(["mcp", "config"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["mcpServers"]["prouse"]["env"],
                         {"PROUSE_HOME": str(self.home)})
        checker = self.process("mcp", "check", "--workspace-id", setup["workspace_id"], "--json")
        stdout, stderr = checker.communicate(timeout=15)
        self.assertEqual(checker.returncode, 0, stderr + stdout)
        result = json.loads(stdout)
        self.assertEqual(result["handshake"], "verified")
        self.assertEqual(result["workspace_read"], "verified")
        self.assertEqual(result["workspace_id"], setup["workspace_id"])
        self.assertEqual(result["client_connection"], "not_verified")
        checker = self.process("mcp", "check", "--workspace-id", "missing", "--json")
        stdout, stderr = checker.communicate(timeout=15)
        self.assertEqual(checker.returncode, ExitCode.ERROR, stderr + stdout)
        self.assertEqual(json.loads(stdout)["status"], "error")
        self.assertIn("no selected workspace", json.loads(stdout)["error"])

    def test_mcp_config_does_not_choose_another_installation_from_path(self):
        other = self.base / "other installation"
        other.mkdir()
        wrong = other / "prouse"
        wrong.write_text("#!/bin/sh\nexit 42\n")
        wrong.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(other) + os.pathsep + os.environ["PATH"]}):
            self.assertNotEqual(mcp.command(self.paths)["command"], str(wrong))

    def test_foreground_run_duplicate_start_restart_and_stop(self):
        self.setup(self.free_port())
        started = self.process("run", "--json")
        status = self.wait_ready(started)
        self.assertEqual(status["pid"], started.pid)
        self.assertEqual(status["local_installation"]["status"], "ready")
        self.assertEqual(self.run_cli(["doctor", "--json"])[0], 0)
        self.assertTrue(self.paths.log.is_file())
        duplicate = subprocess.run([sys.executable, "-m", "prouse.cli", "start", "--json"],
            cwd=self.base, env=dict(os.environ, PYTHONPATH=PYTHONPATH, PROUSE_HOME=str(self.home)),
            capture_output=True, text=True, timeout=5)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout)["status"], "already_running")
        code, output, _ = self.run_cli(["stop", "--json"])
        self.assertEqual(code, 0, output)
        stdout, stderr = started.communicate(timeout=5)
        self.assertEqual(started.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["pid"], started.pid)
        self.assertEqual(self.run_cli(["status", "--json"])[0], ExitCode.NOT_RUNNING)

        restarted = self.process("restart")
        self.wait_ready(restarted)
        self.assertEqual(self.run_cli(["stop"])[0], 0)
        restarted.communicate(timeout=5)

    def test_port_conflict_and_stale_instance(self):
        port = self.free_port()
        self.setup(port)
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", port))
            blocker.listen()
            process = self.process("run")
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, ExitCode.ERROR, stdout + stderr)
        self.assertIn("Cannot start Admin", stderr)
        self.assertFalse(self.paths.instance.exists())

        atomic_json(self.paths.instance, {
            "pid": os.getpid(), "fingerprint": "not-the-current-process", "home": str(self.home)})
        code, output, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, ExitCode.NOT_RUNNING)
        self.assertEqual(json.loads(output)["recovered"], "stale instance state")
        self.assertFalse(self.paths.instance.exists())

    def test_service_templates_use_argv_and_instance_home(self):
        for kind in ("launchd", "systemd"):
            spec = services.definition(self.paths, kind)
            rendered = spec.content.decode()
            self.assertIn(str(self.home), rendered)
            self.assertIn("prouse.cli", rendered)
            self.assertTrue(spec.install)
            self.assertIn("run", rendered)
            self.assertNotIn("start</string>", rendered)
            for action in ("install", "status", "uninstall"):
                code, output, error = self.run_cli(["service", action, "--manager", kind,
                                                    "--dry-run", "--json"])
                self.assertEqual(code, 0, error)
                self.assertIn(json.loads(output)["status"], {"not_run"})

    def test_packaged_admin_assets_resolve_outside_checkout(self):
        from importlib import resources
        old = Path.cwd()
        os.chdir(self.base)
        try:
            page = resources.files("prouse_assets").joinpath("admin_ui", "index.html").read_text()
            schema = json.loads(resources.files("prouse").joinpath(
                "examples", "workspace-registry.schema.json").read_text())
        finally:
            os.chdir(old)
        self.assertIn("ProUse", page)
        self.assertFalse(schema["additionalProperties"])

    def test_agent_documentation_contains_supported_commands(self):
        text = (ROOT / "docs/install-for-agents.md").read_text()
        for command in ("prouse setup", "prouse start", "prouse status --json", "prouse doctor --json",
                        "prouse stop", "prouse restart", '"mcp", "serve"'):
            self.assertIn(command, text)
        self.assertIn("not_verified", text)

    def test_json_argument_errors_and_global_flag(self):
        for arguments in (["start", "--port", "bad"], ["start", "--start-timeout", "nan"],
                          ["restart", "--timeout", "-1"], ["logs", "--lines", "-1"],
                          ["mcp", "check", "--timeout", "inf"], ["service"], ["unknown"]):
            with self.subTest(arguments=arguments):
                code, output, errors = self.run_cli(["--json", *arguments])
                self.assertEqual(code, ExitCode.USAGE, output)
                self.assertEqual(json.loads(output)["exit_code"], ExitCode.USAGE)
                self.assertEqual(errors, "")
        code, output, _ = self.run_cli(["--json", "status"])
        self.assertEqual(code, ExitCode.NOT_RUNNING)
        self.assertEqual(json.loads(output)["status"], "stopped")
        code, output, _ = self.run_cli(["start", "--port", "bad", "--json"])
        self.assertEqual(code, ExitCode.USAGE)
        self.assertEqual(json.loads(output)["status"], "error")

    def test_concurrent_starts_share_one_owned_process(self):
        self.setup(self.free_port())
        launchers = [self.process("start", "--json") for _ in range(2)]
        results = []
        for launcher in launchers:
            stdout, stderr = launcher.communicate(timeout=12)
            self.assertEqual(launcher.returncode, 0, stdout + stderr)
            results.append(json.loads(stdout))
        self.assertEqual(results[0]["pid"], results[1]["pid"])
        self.assertEqual({item["status"] for item in results}, {"running", "already_running"})

    def test_invalid_restart_preserves_running_instance_and_stop_ignores_bad_settings(self):
        self.setup(self.free_port())
        launcher = self.process("start", "--json")
        stdout, stderr = launcher.communicate(timeout=10)
        self.assertEqual(launcher.returncode, 0, stderr)
        pid = json.loads(stdout)["pid"]
        code, output, _ = self.run_cli(["restart", "--port", "0", "--json"])
        self.assertEqual(code, ExitCode.USAGE, output)
        self.assertEqual(self.runtime.status()["pid"], pid)
        self.paths.settings.write_text('{"host": "invalid host", "port": 0}')
        code, output, _ = self.run_cli(["doctor", "--json"])
        self.assertEqual(code, ExitCode.ERROR)
        self.assertEqual(json.loads(output)["status"], "error")
        self.assertEqual(self.run_cli(["stop", "--json"])[0], 0)
        self.assertIsNone(self.runtime.instance.current()[0])

    def test_start_timeout_leaves_no_owned_process(self):
        self.setup(self.free_port())
        launcher = self.process("start", "--start-timeout", "0.001", "--json")
        stdout, stderr = launcher.communicate(timeout=10)
        self.assertEqual(launcher.returncode, ExitCode.ERROR, stderr)
        self.assertIn("did not become ready", json.loads(stdout)["error"])
        self.assertFalse(self.runtime.status()["running"])

    def test_foreign_instance_receipt_is_never_signalled(self):
        from prouse.runtime.process import fingerprint
        self.setup()
        atomic_json(self.paths.instance, {
            "pid": os.getpid(), "fingerprint": fingerprint(os.getpid()),
            "home": str(self.base / "another instance"), "host": "127.0.0.1",
            "port": 8848, "started_at": time.time(),
        })
        with mock.patch("prouse.runtime.admin.os.kill") as kill:
            code, output, _ = self.run_cli(["stop", "--json"])
        self.assertEqual(code, 0, output)
        kill.assert_not_called()
        self.assertEqual(json.loads(output)["status"], "already_stopped")

    def test_repeat_setup_recognizes_relative_workspace_root(self):
        self.setup()
        registry = json.loads(self.paths.registry.read_text())
        registry["workspaces"][0]["root"] = os.path.relpath(self.workspace, self.paths.config)
        atomic_json(self.paths.registry, registry)
        self.assertFalse(self.setup()["workspace_added"])
        self.assertEqual(len(self.config.registry().workspaces), 1)

    def test_setup_bad_settings_is_reported_without_overwriting_registry(self):
        self.setup()
        before = self.paths.registry.read_bytes()
        self.paths.settings.write_text('[]')
        code, output, _ = self.run_cli(["setup", "--workspace", str(self.workspace), "--json"])
        self.assertEqual(code, ExitCode.ERROR, output)
        self.assertEqual(json.loads(output)["status"], "error")
        self.assertEqual(self.paths.registry.read_bytes(), before)

    def test_logs_tail_zero_and_follow_include_recent_lines(self):
        from prouse.runtime.logs import lines
        self.paths.prepare_runtime()
        self.paths.log.write_text("first\nsecond\nlast\n")
        self.assertEqual(self.run_cli(["logs", "--lines", "0"]), (0, "", ""))
        self.assertEqual(self.run_cli(["logs", "--lines", "2"]), (0, "second\nlast\n", ""))
        following = lines(self.paths.log, 1, follow=True)
        self.assertEqual(next(following), "last\n")
        following.close()

    def test_legacy_admin_alias_and_background_flag_remain_compatible(self):
        arguments = cli.parser().parse_args(cli.normalize_legacy_args(["admin", "--foreground"]))
        self.assertEqual(arguments.command, "run")
        arguments = cli.parser().parse_args(cli.normalize_legacy_args(["admin"]))
        self.assertEqual(arguments.command, "start")
        self.assertTrue(cli.parser().parse_args(["start", "--background"]).background)


class InstalledStyleMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_stdio_initialize_list_and_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "temporary project"
            project.mkdir()
            (project / "README.md").write_text("temporary MCP bytes\n")
            env = dict(os.environ, PROUSE_HOME=str(base / "home"), PYTHONPATH=PYTHONPATH)
            installed = os.environ.get("PROUSE_INSTALLED_COMMAND")
            prefix = [installed] if installed else [sys.executable, "-m", "prouse.cli"]
            setup = subprocess.run([*prefix, "setup", "--workspace", str(project),
                                    "--no-input", "--host", "127.0.0.1"], cwd=base, env=env,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(setup.returncode, 0, setup.stderr)
            params = StdioServerParameters(command=prefix[0],
                args=[*prefix[1:], "mcp", "serve"], cwd=str(base), env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "ProUse")
                    tools = await session.list_tools()
                    self.assertIn("read_files", {tool.name for tool in tools.tools})
                    response = await session.call_tool("read_files", {
                        "requests": [{"path": "README.md", "start_line": 1, "end_line": 1}]})
                    self.assertFalse(response.isError, response)
                    self.assertIn("temporary MCP bytes", response.structuredContent["files"][0]["text"])


if __name__ == "__main__":
    unittest.main()
