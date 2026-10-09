"""Shared test fixture: a scratch project registered as the default workspace."""
import json
import os
from pathlib import Path
import tempfile
import unittest

# Keep tests independent of the developer's login-shell profile.
os.environ.setdefault("PROUSE_SHELL_SNAPSHOT", "0")

from prouse.workspaces import Workspaces  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="prouse test ")
        self.base = Path(os.path.realpath(self._tmp.name))
        self.project = self.base / "project"
        self.project.mkdir()
        self.home = self.base / "home"
        self.registry = self.home / "config" / "workspace-registry.json"
        self.output = self.home / "output"
        self.register({"id": "project", "root": str(self.project)})

    def tearDown(self):
        self._tmp.cleanup()

    def register(self, *entries, default="project"):
        document = {"version": 2, "default_workspace_id": default, "workspaces": list(entries)}
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps(document))
        self.workspaces = Workspaces(self.registry, extra_readable=(self.output,))

    def write(self, relative, content, mode="w"):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            with path.open(mode, encoding="utf-8", newline="") as handle:
                handle.write(content)
        return path

    def text(self, relative):
        return (self.project / relative).read_bytes().decode("utf-8")
