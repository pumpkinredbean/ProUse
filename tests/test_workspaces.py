import json
import os
from pathlib import Path
import unittest

from support import WorkspaceCase

from prouse.tools import ToolError
from prouse.workspaces import Workspaces


class ResolveTests(WorkspaceCase):
    def test_relative_absolute_and_at_prefixed_paths(self):
        self.write("src/app.py", "print()\n")
        target = self.project / "src" / "app.py"
        self.assertEqual(self.workspaces.resolve("src/app.py"), target)
        self.assertEqual(self.workspaces.resolve(str(target)), target)
        self.assertEqual(self.workspaces.resolve("@src/app.py"), target)
        self.assertEqual(self.workspaces.resolve(None), self.project)
        self.assertEqual(self.workspaces.resolve("missing/new.txt"), self.project / "missing" / "new.txt")

    def test_paths_outside_every_workspace_are_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.project / "escape").symlink_to(outside)
        for path in ("../outside", str(outside), "escape/file.txt", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaisesRegex(ToolError, "outside the registered workspaces"):
                self.workspaces.resolve(path)

    def test_symlinks_inside_a_workspace_are_followed(self):
        self.write("real/file.txt", "x")
        (self.project / "link").symlink_to(self.project / "real")
        self.assertEqual(self.workspaces.resolve("link/file.txt"), self.project / "real" / "file.txt")

    def test_bash_output_is_readable_but_not_writable(self):
        self.output.mkdir(parents=True)
        log = self.output / "bash-1-1.log"
        self.assertEqual(self.workspaces.resolve(str(log), readable_extra=True), log)
        with self.assertRaises(ToolError):
            self.workspaces.resolve(str(log))

    def test_relative_paths_need_a_default_when_several_workspaces_exist(self):
        other = self.base / "other"
        other.mkdir()
        self.register({"id": "project", "root": str(self.project)}, {"id": "other", "root": str(other)},
                      default=None)
        with self.assertRaisesRegex(ToolError, "ambiguous"):
            self.workspaces.resolve("README.md")
        self.assertEqual(self.workspaces.resolve(str(other / "a.txt")), other / "a.txt")

    def test_nested_workspaces_pick_the_innermost(self):
        inner = self.project / "packages" / "inner"
        inner.mkdir(parents=True)
        self.register({"id": "project", "root": str(self.project)}, {"id": "inner", "root": str(inner)})
        self.assertEqual(self.workspaces.containing(inner / "x.py").id, "inner")
        self.assertEqual(self.workspaces.find(str(inner)).id, "inner")

    def test_display_is_relative_to_the_default_workspace(self):
        self.assertEqual(self.workspaces.display(self.project / "a" / "b.txt"), "a/b.txt")
        self.assertEqual(self.workspaces.display(self.project), ".")
        self.assertEqual(self.workspaces.display(self.base / "elsewhere"), str(self.base / "elsewhere"))


class RegistryTests(WorkspaceCase):
    def test_missing_registry_explains_how_to_register(self):
        workspaces = Workspaces(self.base / "missing.json")
        with self.assertRaisesRegex(ToolError, "prouse setup --workspace"):
            workspaces.resolve(".")

    def test_version_1_registry_from_earlier_releases(self):
        disabled = self.base / "disabled"
        disabled.mkdir()
        self.registry.write_text(json.dumps({
            "version": 1, "server_name": "ProUse", "default_worker_profile": "standard",
            "worker_profiles": [{"id": "standard", "model": "gpt-5-codex"}],
            "workspaces": [
                {"id": "project", "label": "Project", "root": str(self.project), "enabled": True,
                 "default_worker_profile": "standard", "context_policy": "policies/project.json"},
                {"id": "disabled", "root": str(disabled), "enabled": False},
            ],
        }))
        workspaces = Workspaces(self.registry)
        self.assertEqual([item.id for item in workspaces.all()], ["project"])
        self.assertEqual(workspaces.default().id, "project")

    def test_missing_folders_and_the_home_folder_are_reported_unavailable(self):
        self.register({"id": "project", "root": str(self.project)},
                      {"id": "gone", "root": str(self.base / "gone")},
                      {"id": "home", "root": str(Path.home())})
        reasons = {item.id: item.reason for item in self.workspaces.unavailable()}
        self.assertEqual(reasons, {"gone": "folder not found", "home": "home or filesystem root is not allowed"})
        self.assertEqual([item.id for item in self.workspaces.all()], ["project"])

    def test_registry_changes_apply_without_restarting(self):
        other = self.base / "other"
        other.mkdir()
        self.assertEqual(len(self.workspaces.all()), 1)
        replacement = self.registry.with_suffix(".tmp")
        replacement.write_text(json.dumps({"version": 2, "default_workspace_id": "project", "workspaces": [
            {"id": "project", "root": str(self.project)}, {"id": "other", "root": str(other)}]}))
        os.replace(replacement, self.registry)
        self.assertEqual([item.id for item in self.workspaces.all()], ["project", "other"])

    def test_invalid_registry_is_reported_to_the_model(self):
        self.registry.write_text("{not json")
        with self.assertRaisesRegex(ToolError, "Cannot read the ProUse workspace registry"):
            Workspaces(self.registry).all()


class DescribeTests(WorkspaceCase):
    def test_lists_workspaces_and_asks_for_a_name_when_several_exist(self):
        other = self.base / "other"
        other.mkdir()
        self.register({"id": "project", "label": "My project", "root": str(self.project)},
                      {"id": "other", "root": str(other)})
        text = self.workspaces.describe()
        self.assertIn(f"- project (default, My project): {self.project}", text)
        self.assertIn(f"- other: {other}", text)
        self.assertIn("Call workspaces with name=<id>", text)

    def test_loads_project_instructions_outermost_first(self):
        (self.base / "AGENTS.md").write_text("parent rules\n")
        self.write("AGENTS.md", "project rules\n")
        self.write("CLAUDE.md", "ignored because AGENTS.md exists\n")
        text = self.workspaces.describe()  # The only workspace is selected automatically.
        self.assertIn("project rules", text)
        self.assertNotIn("ignored because", text)
        self.assertLess(text.index("parent rules"), text.index("project rules"))
        self.assertIn(f'<project_instructions path="{self.project / "AGENTS.md"}">', text)

    def test_claude_md_is_used_without_agents_md(self):
        self.write("CLAUDE.md", "claude rules\n")
        self.assertIn("claude rules", self.workspaces.describe("project"))

    def test_reports_when_there_are_no_instructions(self):
        self.assertIn("project has no AGENTS.md or CLAUDE.md", self.workspaces.describe("project"))

    def test_unknown_workspace_names_list_the_registered_ones(self):
        with self.assertRaisesRegex(ToolError, "Unknown workspace 'nope'. Registered: project"):
            self.workspaces.describe("nope")


if __name__ == "__main__":
    unittest.main()
