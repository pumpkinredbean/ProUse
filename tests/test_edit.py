"""Behaviour ported from pi's edit tool tests (packages/coding-agent/test/tools.test.ts)."""
import unittest

from support import WorkspaceCase

from prouse.tools import ToolError
from prouse.tools.edit import edit


class EditTests(WorkspaceCase):
    def edit(self, name, *pairs):
        return edit(self.workspaces, name, [{"oldText": old, "newText": new} for old, new in pairs])

    def test_replaces_text(self):
        self.write("edit.txt", "Hello, world!")
        result = self.edit("edit.txt", ("world", "testing"))
        self.assertEqual(result.text, "Successfully replaced 1 block(s) in edit.txt. First change at line 1.")
        self.assertEqual(self.text("edit.txt"), "Hello, testing!")

    def test_reports_the_first_changed_line(self):
        self.write("lines.txt", "a\nb\nc\nd\n")
        self.assertIn("First change at line 3.", self.edit("lines.txt", ("c\n", "C\n")).text)

    def test_fails_when_text_is_missing_or_not_unique(self):
        self.write("edit.txt", "foo foo foo")
        with self.assertRaisesRegex(ToolError, "Could not find the exact text"):
            self.edit("edit.txt", ("nonexistent", "x"))
        with self.assertRaisesRegex(ToolError, "Found 3 occurrences"):
            self.edit("edit.txt", ("foo", "bar"))
        with self.assertRaisesRegex(ToolError, "Could not edit file: missing.txt. File not found."):
            self.edit("missing.txt", ("a", "b"))

    def test_multiple_disjoint_edits_match_the_original_file(self):
        self.write("multi.txt", "alpha\nbeta\ngamma\ndelta\n")
        result = self.edit("multi.txt", ("alpha\n", "ALPHA\n"), ("gamma\n", "GAMMA\n"))
        self.assertIn("Successfully replaced 2 block(s)", result.text)
        self.assertEqual(self.text("multi.txt"), "ALPHA\nbeta\nGAMMA\ndelta\n")
        self.write("original.txt", "foo\nbar\nbaz\n")
        self.edit("original.txt", ("foo\n", "foo bar\n"), ("bar\n", "BAR\n"))
        self.assertEqual(self.text("original.txt"), "foo bar\nBAR\nbaz\n")

    def test_rejects_empty_overlapping_and_partial_edits(self):
        self.write("edit.txt", "one\ntwo\nthree\n")
        with self.assertRaisesRegex(ToolError, "edits must contain at least one replacement"):
            edit(self.workspaces, "edit.txt", [])
        with self.assertRaisesRegex(ToolError, "oldText must not be empty"):
            self.edit("edit.txt", ("", "x"))
        with self.assertRaisesRegex(ToolError, "overlap"):
            self.edit("edit.txt", ("one\ntwo\n", "ONE\nTWO\n"), ("two\nthree\n", "TWO\nTHREE\n"))
        with self.assertRaisesRegex(ToolError, "Could not find edits\\[1\\]"):
            self.edit("edit.txt", ("one\n", "ONE\n"), ("missing\n", "MISSING\n"))
        with self.assertRaisesRegex(ToolError, "No changes made"):
            self.edit("edit.txt", ("one", "one"))
        self.assertEqual(self.text("edit.txt"), "one\ntwo\nthree\n")

    def test_refuses_binary_files_and_paths_outside_workspaces(self):
        self.write("blob.bin", b"\x00\x01text")
        with self.assertRaisesRegex(ToolError, "binary file"):
            self.edit("blob.bin", ("text", "x"))
        with self.assertRaisesRegex(ToolError, "outside the registered workspaces"):
            self.edit(str(self.base / "outside.txt"), ("a", "b"))


class FuzzyMatchTests(WorkspaceCase):
    def edit(self, name, *pairs):
        return edit(self.workspaces, name, [{"oldText": old, "newText": new} for old, new in pairs])

    def test_trailing_whitespace(self):
        self.write("ws.txt", "line one   \nline two  \nline three\n")
        self.edit("ws.txt", ("line one\nline two\n", "replaced\n"))
        self.assertEqual(self.text("ws.txt"), "replaced\nline three\n")

    def test_fullwidth_punctuation(self):
        self.write("zh.txt", "\u4f60\u597d\uff0c\u4e16\u754c\n\u4f60\u597d\uff08\u4e16\u754c\uff09\n")
        self.edit("zh.txt", ("\u4f60\u597d,\u4e16\u754c\n\u4f60\u597d(\u4e16\u754c)\n",
                             "\u4f60\u597d\uff0cpi\n\u4f60\u597d(pi)\n"))
        self.assertEqual(self.text("zh.txt"), "\u4f60\u597d\uff0cpi\n\u4f60\u597d(pi)\n")

    def test_compatibility_equivalent_forms(self):
        self.write("nfkc.txt", "\uff21\uff22\uff23\uff11\uff12\uff13\ncafe\u0301\n")
        self.edit("nfkc.txt", ("ABC123\ncaf\xe9\n", "XYZ789\ncoffee\n"))
        self.assertEqual(self.text("nfkc.txt"), "XYZ789\ncoffee\n")

    def test_smart_quotes_dashes_and_special_spaces(self):
        self.write("quotes.txt", "console.log(\u2018hello\u2019);\nconst msg = \u201cHello\u201d;\n")
        self.edit("quotes.txt", ("console.log('hello');", "console.log('world');"),
                  ('const msg = "Hello";', 'const msg = "Bye";'))
        self.assertEqual(self.text("quotes.txt"), "console.log('world');\nconst msg = \"Bye\";\n")
        self.write("dash.txt", "range: 1\u20135\nbreak\u2014here\nhello\xa0world\n")
        self.edit("dash.txt", ("range: 1-5\nbreak-here\nhello world", "range: 10-50\nbreak--here\nhi"))
        self.assertEqual(self.text("dash.txt"), "range: 10-50\nbreak--here\nhi\n")

    def test_exact_match_is_preferred(self):
        self.write("exact.txt", "const x = 'exact';\nconst y = 'other';\n")
        self.edit("exact.txt", ("const x = 'exact';", "const x = 'changed';"))
        self.assertEqual(self.text("exact.txt"), "const x = 'changed';\nconst y = 'other';\n")

    def test_still_fails_without_a_match_and_detects_fuzzy_duplicates(self):
        self.write("dup.txt", "hello world   \nhello world\n")
        with self.assertRaisesRegex(ToolError, "Could not find the exact text"):
            self.edit("dup.txt", ("this does not exist", "x"))
        with self.assertRaisesRegex(ToolError, "Found 2 occurrences"):
            self.edit("dup.txt", ("hello world", "replaced"))

    def test_fuzzy_multi_edit(self):
        self.write("multi.txt", "console.log(\u2018hello\u2019);\nhello\xa0world\n")
        self.edit("multi.txt", ("console.log('hello');\n", "console.log('world');\n"),
                  ("hello world\n", "hello universe\n"))
        self.assertEqual(self.text("multi.txt"), "console.log('world');\nhello universe\n")

    def test_fuzzy_replacement_keeps_the_right_occurrence(self):
        self.write("near.txt", "replace me   \nafter   \n")
        self.edit("near.txt", ("replace me\n", "after\n"))
        self.assertEqual(self.text("near.txt"), "after\nafter   \n")

    def test_fuzzy_edits_preserve_untouched_lines(self):
        self.write("keep.txt", "keep before  \nfirst target  \nfirst after\nkeep middle   \n"
                               "second target  \nsecond after\nkeep after  \n")
        self.edit("keep.txt", ("first target\nfirst after", "FIRST\nFIRST2"),
                  ("second target\nsecond after", "SECOND\nSECOND2"))
        self.assertEqual(self.text("keep.txt"), "keep before  \nFIRST\nFIRST2\nkeep middle   \n"
                                                "SECOND\nSECOND2\nkeep after  \n")


class LineEndingTests(WorkspaceCase):
    def edit(self, name, *pairs):
        return edit(self.workspaces, name, [{"oldText": old, "newText": new} for old, new in pairs])

    def test_crlf_files_keep_crlf(self):
        self.write("crlf.txt", "first\r\nsecond\r\nthird\r\n")
        self.edit("crlf.txt", ("second\n", "REPLACED\n"))
        self.assertEqual(self.text("crlf.txt"), "first\r\nREPLACED\r\nthird\r\n")

    def test_lf_files_keep_lf(self):
        self.write("lf.txt", "first\nsecond\nthird\n")
        self.edit("lf.txt", ("second\n", "REPLACED\n"))
        self.assertEqual(self.text("lf.txt"), "first\nREPLACED\nthird\n")

    def test_duplicates_across_line_ending_variants(self):
        self.write("mixed.txt", "hello\r\nworld\r\n---\r\nhello\nworld\n")
        with self.assertRaisesRegex(ToolError, "Found 2 occurrences"):
            self.edit("mixed.txt", ("hello\nworld\n", "replaced\n"))

    def test_bom_and_crlf_survive_multi_edits(self):
        self.write("bom.txt", "\ufefffirst\r\nsecond\r\nthird\r\nfourth\r\n")
        self.edit("bom.txt", ("second\n", "SECOND\n"), ("fourth\n", "FOURTH\n"))
        self.assertEqual(self.text("bom.txt"), "\ufefffirst\r\nSECOND\r\nthird\r\nFOURTH\r\n")

    def test_invalid_utf8_bytes_elsewhere_are_kept(self):
        self.write("latin1.txt", b"caf\xe9\nkeep\n")
        self.edit("latin1.txt", ("keep", "KEEP"))
        self.assertEqual((self.project / "latin1.txt").read_bytes(), b"caf\xe9\nKEEP\n")


if __name__ == "__main__":
    unittest.main()
