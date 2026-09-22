"""Exercise the shell bootstrap with isolated executable paths and no downloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

UV_STUB = r'''
import json, os, pathlib, sys
with open(os.environ["TEST_UV_CALLS"], "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["tool", "dir"]:
    print(os.environ["UV_TOOL_BIN_DIR"])
elif sys.argv[1:3] == ["tool", "install"]:
    if os.environ.get("TEST_INSTALL_FAIL"):
        sys.exit(7)
    target = pathlib.Path(os.environ["UV_TOOL_BIN_DIR"]) / "prouse"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nprintf 'ProUse 0.2.0\\n'\n")
    target.chmod(0o755)
'''

CURL_STUB = r'''
import os, pathlib, sys
if os.environ.get("TEST_DOWNLOAD_FAIL"):
    sys.exit(22)
assert "https://astral.sh/uv/install.sh" in sys.argv
target = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
target.write_text('#!/bin/sh\nmkdir -p "$UV_INSTALL_DIR"\ncp "$TEST_UV_STUB" "$UV_INSTALL_DIR/uv"\n')
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="prouse installer ")
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.calls = self.base / "calls.jsonl"
        self.env = dict(os.environ, PATH=str(self.bin) + ":/usr/bin:/bin",
                        UV_INSTALL_DIR=str(self.base / "uv install"),
                        UV_TOOL_BIN_DIR=str(self.base / "tool bin"),
                        TEST_UV_CALLS=str(self.calls),
                        TEST_UV_STUB=str(self.base / "stub-uv"),
                        PROUSE_HOME=str(self.base / "settings"))
        self.stub(self.base / "stub-uv", UV_STUB)
        self.stub(self.bin / "curl", CURL_STUB)
        self.settings = Path(self.env["PROUSE_HOME"]) / "config/settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text('{"preserve": true}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, path, code):
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)

    def install(self, *args):
        return subprocess.run(["/bin/sh", str(ROOT / "install.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def uv_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_missing_uv_bootstraps_then_installs_and_keeps_configuration(self):
        result = self.install("--source", str(ROOT), "--no-modify-path")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(self.env["UV_INSTALL_DIR"]) / "uv").is_file())
        self.assertIn("ProUse 0.2.0", result.stdout)
        self.assertIn(self.env["UV_TOOL_BIN_DIR"], result.stdout)
        self.assertEqual(self.settings.read_text(), '{"preserve": true}\n')
        calls = self.uv_calls()
        self.assertIn("--reinstall", calls[0])
        self.assertIn(str(ROOT), calls[0])
        self.assertNotIn(["tool", "update-shell"], calls)

    def test_existing_uv_installs_selected_ref_and_updates_shell(self):
        self.stub(self.bin / "uv", UV_STUB)
        result = self.install("--ref", "codex/cli-onboarding")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.uv_calls()
        self.assertIn("git+https://github.com/pumpkinredbean/ProUse.git@codex/cli-onboarding", calls[0])
        self.assertIn(["tool", "update-shell"], calls)

    def test_failed_download_never_runs_installation(self):
        self.env["TEST_DOWNLOAD_FAIL"] = "1"
        result = self.install()
        self.assertEqual(result.returncode, 22)
        self.assertFalse(self.calls.exists())
        self.assertNotIn("Installed:", result.stdout)

    def test_failed_installation_does_not_print_success(self):
        self.stub(self.bin / "uv", UV_STUB)
        self.env["TEST_INSTALL_FAIL"] = "1"
        result = self.install("--source", str(ROOT))
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("Installed:", result.stdout)
        self.assertEqual(self.settings.read_text(), '{"preserve": true}\n')


if __name__ == "__main__":
    unittest.main()
