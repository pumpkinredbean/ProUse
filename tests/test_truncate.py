import unittest

from prouse.tools.truncate import (DEFAULT_MAX_BYTES, format_size, split_lines, truncate_head, truncate_line,
                                   truncate_tail)


class TruncateTests(unittest.TestCase):
    def test_content_within_limits_is_unchanged(self):
        result = truncate_head("a\nb\n")
        self.assertFalse(result.truncated)
        self.assertEqual(result.content, "a\nb\n")
        self.assertEqual(result.total_lines, 2)

    def test_trailing_newline_does_not_count_as_a_line(self):
        self.assertEqual(split_lines("a\nb\n"), ["a", "b"])
        self.assertEqual(split_lines(""), [])

    def test_head_keeps_the_first_lines(self):
        content = "\n".join(f"line {number}" for number in range(1, 2501))
        result = truncate_head(content)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "lines")
        self.assertEqual(result.output_lines, 2000)
        self.assertTrue(result.content.endswith("line 2000"))

    def test_head_stops_on_bytes_without_partial_lines(self):
        content = "\n".join("x" * 1000 for _ in range(100))
        result = truncate_head(content)
        self.assertEqual(result.truncated_by, "bytes")
        self.assertLessEqual(result.output_bytes, DEFAULT_MAX_BYTES)
        self.assertTrue(all(len(line) == 1000 for line in result.content.split("\n")))

    def test_head_reports_a_first_line_over_the_limit(self):
        result = truncate_head("x" * (DEFAULT_MAX_BYTES + 1) + "\nshort")
        self.assertTrue(result.first_line_exceeds_limit)
        self.assertEqual(result.content, "")

    def test_tail_keeps_the_last_lines(self):
        content = "\n".join(f"line {number}" for number in range(1, 4001)) + "\n"
        result = truncate_tail(content)
        self.assertEqual(result.total_lines, 4000)
        self.assertEqual(result.output_lines, 2000)
        self.assertTrue(result.content.startswith("line 2001\n"))

    def test_tail_keeps_the_end_of_one_huge_line_on_a_character_boundary(self):
        result = truncate_tail("\u20ac" * DEFAULT_MAX_BYTES)
        self.assertTrue(result.last_line_partial)
        self.assertLessEqual(result.output_bytes, DEFAULT_MAX_BYTES)
        self.assertEqual(set(result.content), {"\u20ac"})

    def test_line_and_size_helpers(self):
        self.assertEqual(truncate_line("abc", 5), ("abc", False))
        self.assertEqual(truncate_line("abcdef", 3), ("abc... [truncated]", True))
        self.assertEqual(format_size(512), "512B")
        self.assertEqual(format_size(50 * 1024), "50.0KB")
        self.assertEqual(format_size(3 * 1024 * 1024), "3.0MB")


if __name__ == "__main__":
    unittest.main()
