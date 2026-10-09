import base64
import unittest

from support import WorkspaceCase

from prouse.tools import ToolError
from prouse.tools.files import ls, read, write

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


class ReadTests(WorkspaceCase):
    def test_reads_a_small_file_as_is(self):
        self.write("test.txt", "Hello, world!\nLine 2\nLine 3")
        result = read(self.workspaces, "test.txt")
        self.assertEqual(result.text, "Hello, world!\nLine 2\nLine 3")
        self.assertFalse(result.is_error)

    def test_missing_file_and_directory(self):
        with self.assertRaisesRegex(ToolError, "File not found: nope.txt"):
            read(self.workspaces, "nope.txt")
        with self.assertRaisesRegex(ToolError, "is a directory"):
            read(self.workspaces, ".")

    def test_truncates_at_2000_lines(self):
        self.write("large.txt", "\n".join(f"Line {number}" for number in range(1, 2501)))
        text = read(self.workspaces, "large.txt").text
        self.assertIn("Line 2000", text)
        self.assertNotIn("Line 2001", text)
        self.assertIn("[Showing lines 1-2000 of 2500. Use offset=2001 to continue.]", text)

    def test_truncates_at_50kb(self):
        self.write("wide.txt", "\n".join(f"Line {number}: {'x' * 200}" for number in range(1, 501)))
        text = read(self.workspaces, "wide.txt").text
        self.assertRegex(text, r"\[Showing lines 1-\d+ of 500 \(50.0KB limit\)\. Use offset=\d+ to continue\.\]")

    def test_offset_and_limit(self):
        self.write("numbers.txt", "\n".join(f"Line {number}" for number in range(1, 101)))
        text = read(self.workspaces, "numbers.txt", offset=41, limit=20).text
        self.assertNotIn("Line 40\n", text)
        self.assertIn("Line 41", text)
        self.assertIn("Line 60", text)
        self.assertNotIn("Line 61", text)
        self.assertIn("[40 more lines in file. Use offset=61 to continue.]", text)
        tail = read(self.workspaces, "numbers.txt", offset=51).text
        self.assertTrue(tail.startswith("Line 51\n") and tail.endswith("Line 100"))
        with self.assertRaisesRegex(ToolError, r"Offset 101 is beyond end of file \(100 lines total\)"):
            read(self.workspaces, "numbers.txt", offset=101)

    def test_one_line_over_the_limit_points_to_bash(self):
        self.write("minified.js", "x" * (60 * 1024))
        self.assertIn("exceeds 50.0KB limit. Use bash: sed -n '1p' minified.js", read(self.workspaces, "minified.js").text)

    def test_images_are_attached_by_content_not_extension(self):
        self.write("pixel.dat", PNG)
        result = read(self.workspaces, "pixel.dat")
        self.assertEqual(result.text, "Read image file [image/png]")
        self.assertEqual(result.images, [(base64.b64encode(PNG).decode(), "image/png")])
        self.write("fake.png", "just text\n")
        self.assertEqual(read(self.workspaces, "fake.png").text, "just text\n")

    def test_binary_files_are_not_dumped(self):
        self.write("blob.bin", b"\x00\x01\x02binary")
        with self.assertRaisesRegex(ToolError, "binary file"):
            read(self.workspaces, "blob.bin")

    def test_bom_is_dropped_and_empty_files_are_reported(self):
        self.write("bom.txt", "\ufeffhello\n")
        self.assertEqual(read(self.workspaces, "bom.txt").text, "hello\n")
        self.write("empty.txt", "")
        self.assertEqual(read(self.workspaces, "empty.txt").text, "(empty file)")


class WriteTests(WorkspaceCase):
    def test_creates_parent_directories_and_overwrites(self):
        result = write(self.workspaces, "deep/nested/file.txt", "one\n")
        self.assertEqual(result.text, "Successfully created deep/nested/file.txt (4 bytes)")
        result = write(self.workspaces, "deep/nested/file.txt", "\u20ac\r\n")
        self.assertEqual(result.text, "Successfully overwrote deep/nested/file.txt (5 bytes)")
        self.assertEqual((self.project / "deep/nested/file.txt").read_bytes(), "\u20ac\r\n".encode())

    def test_refuses_directories_and_paths_outside_workspaces(self):
        (self.project / "folder").mkdir()
        with self.assertRaisesRegex(ToolError, "is a directory"):
            write(self.workspaces, "folder", "x")
        with self.assertRaises(ToolError):
            write(self.workspaces, str(self.base / "outside.txt"), "x")
        self.assertFalse((self.base / "outside.txt").exists())


class ListTests(WorkspaceCase):
    def test_lists_dotfiles_and_marks_directories(self):
        self.write(".hidden-file", "secret")
        (self.project / ".hidden-dir").mkdir()
        self.write("B.txt", "b")
        self.write("a.txt", "a")
        self.assertEqual(ls(self.workspaces).text, ".hidden-dir/\n.hidden-file\na.txt\nB.txt")

    def test_limit_and_errors(self):
        for number in range(5):
            self.write(f"f{number}.txt", "")
        self.assertEqual(ls(self.workspaces, ".", limit=2).text,
                         "f0.txt\nf1.txt\n\n[2 entries limit reached. Use limit=4 for more]")
        (self.project / "empty").mkdir()
        self.assertEqual(ls(self.workspaces, "empty").text, "(empty directory)")
        with self.assertRaisesRegex(ToolError, "Not a directory: f0.txt"):
            ls(self.workspaces, "f0.txt")
        with self.assertRaisesRegex(ToolError, "Path not found: nope"):
            ls(self.workspaces, "nope")


if __name__ == "__main__":
    unittest.main()
