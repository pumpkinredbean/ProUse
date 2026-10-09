import os
import shutil
import subprocess
import unittest
from unittest import mock

from support import WorkspaceCase

from prouse.tools import ToolError
from prouse.tools.search import _glob_regex, find, grep, ripgrep


class SearchCases:
    """Run against ripgrep and against the built-in fallback."""
    use_rg = True

    def setUp(self):
        super().setUp()
        environment = {} if self.use_rg else {"PROUSE_RG": str(self.base / "no-rg")}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        if self.use_rg and ripgrep() is None:
            self.skipTest("ripgrep is not installed")
        if not self.use_rg:
            self.assertIsNone(ripgrep())
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)

    def grep(self, pattern, **options):
        return grep(self.workspaces, pattern, options.pop("path", None), options.pop("glob", None),
                    options.pop("ignore_case", False), options.pop("literal", False),
                    options.pop("context", None), options.pop("limit", None)).text

    def test_grep_shows_file_and_line(self):
        self.write("example.txt", "first line\nmatch line\nlast line")
        self.assertEqual(self.grep("match", path="example.txt"), "example.txt:2: match line")
        self.write("src/a.py", "needle = 1\n")
        self.assertIn("src/a.py:1: needle = 1", self.grep("needle"))
        self.assertEqual(self.grep("absent"), "No matches found")

    def test_grep_limit_and_context(self):
        self.write("context.txt", "before\nmatch one\nafter\nmiddle\nmatch two\nafter two")
        output = self.grep("match", path="context.txt", limit=1, context=1)
        self.assertIn("context.txt-1- before", output)
        self.assertIn("context.txt:2: match one", output)
        self.assertIn("context.txt-3- after", output)
        self.assertIn("[1 matches limit reached. Use limit=2 for more, or refine pattern]", output)
        self.assertNotIn("match two", output)

    def test_grep_options(self):
        self.write("a.ts", "Value = a.b\n")
        self.write("b.py", "value = axb\n")
        self.assertEqual(self.grep("value", glob="*.py"), "b.py:1: value = axb")
        self.assertIn("a.ts:1: Value = a.b", self.grep("VALUE", ignore_case=True))
        self.assertEqual(self.grep("a.b", literal=True), "a.ts:1: Value = a.b")

    def test_grep_treats_flag_like_patterns_as_text(self):
        marker = self.base / "marker"
        payload = self.write("payload.sh", f"#!/bin/sh\necho executed > '{marker}'\ncat \"$1\"\n")
        payload.chmod(0o755)
        self.assertEqual(self.grep(f"--pre={payload}"), "No matches found")
        self.assertFalse(marker.exists())

    def test_grep_respects_gitignore_and_skips_git_metadata(self):
        self.write(".gitignore", "ignored.txt\n")
        self.write("ignored.txt", "secret needle\n")
        self.write("kept.txt", "needle\n")
        self.assertEqual(self.grep("needle"), "kept.txt:1: needle")
        self.assertEqual(self.grep("needle", glob="*.txt"), "kept.txt:1: needle")
        self.assertEqual(self.grep("repositoryformatversion"), "No matches found")

    def test_grep_truncates_long_lines(self):
        self.write("long.txt", "needle" + "x" * 1000 + "\n")
        output = self.grep("needle")
        self.assertIn("... [truncated]", output)
        self.assertIn("Some lines truncated to 500 chars", output)

    def test_grep_rejects_bad_regex_and_missing_paths(self):
        self.write("a.txt", "x\n")
        with self.assertRaises(ToolError):
            self.grep("(unclosed")
        with self.assertRaisesRegex(ToolError, "Path not found: nope"):
            self.grep("x", path="nope")

    def test_find_includes_hidden_files_and_respects_gitignore(self):
        self.write(".secret/hidden.txt", "")
        self.write("visible.txt", "")
        self.write(".gitignore", "ignored.txt\n")
        self.write("ignored.txt", "")
        lines = find(self.workspaces, "**/*.txt").text.split("\n")
        self.assertIn("visible.txt", lines)
        self.assertIn(".secret/hidden.txt", lines)
        self.assertNotIn("ignored.txt", lines)

    def test_find_patterns_with_a_slash_match_at_any_depth(self):
        self.write("pkg/src/a.ts", "")
        self.write("src/b.ts", "")
        self.write("src/c.js", "")
        self.assertEqual(find(self.workspaces, "src/*.ts").text.split("\n"), ["pkg/src/a.ts", "src/b.ts"])
        self.assertEqual(find(self.workspaces, "*.js").text, "src/c.js")

    def test_find_limit_and_flag_like_patterns(self):
        for name in "abc":
            self.write(f"{name}.md", "")
        self.assertEqual(find(self.workspaces, "*.md", limit=2).text, "a.md\nb.md\n\n[2 results limit reached]")
        self.assertEqual(find(self.workspaces, "--help").text, "No files found matching pattern")
        with self.assertRaisesRegex(ToolError, "Not a directory"):
            find(self.workspaces, "*", path="a.md")


@unittest.skipUnless(shutil.which("git"), "git is required for ignore rules")
class RipgrepSearchTests(SearchCases, WorkspaceCase):
    use_rg = True


@unittest.skipUnless(shutil.which("git"), "git is required for ignore rules")
class FallbackSearchTests(SearchCases, WorkspaceCase):
    use_rg = False


class GlobTests(unittest.TestCase):
    def test_glob_translation(self):
        cases = [("*.py", "a.py", True), ("*.py", "a/b.py", False), ("**/*.py", "a/b.py", True),
                 ("**/*.py", "b.py", True), ("src/**", "src/a/b", True), ("?.md", "ab.md", False),
                 ("*.{js,ts}", "a.ts", True), ("[ab].txt", "c.txt", False), ("[!ab].txt", "c.txt", True)]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(bool(_glob_regex(pattern).match(path)), expected)


if __name__ == "__main__":
    unittest.main()
