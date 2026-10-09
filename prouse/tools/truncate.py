"""Output limits shared by every tool, ported from pi's truncate.ts.

Two independent limits apply and whichever is hit first wins: 2000 lines or 50KB.
Head truncation keeps the start (file reads); tail truncation keeps the end (command
output, where errors and final results usually are). Neither returns a partial line,
except the tail edge case where the last line alone exceeds the byte limit.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(frozen=True)
class Truncation:
    content: str
    truncated: bool
    truncated_by: str | None  # "lines", "bytes" or None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool = False
    first_line_exceeds_limit: bool = False


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def split_lines(content: str) -> list[str]:
    """Lines for counting: a trailing newline does not start another line."""
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def truncate_head(content: str, max_lines: int = DEFAULT_MAX_LINES,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> Truncation:
    total_bytes = _size(content)
    lines = split_lines(content)
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return Truncation(content, False, None, len(lines), total_bytes, len(lines), total_bytes)
    if _size(lines[0]) > max_bytes:
        return Truncation("", True, "bytes", len(lines), total_bytes, 0, 0, first_line_exceeds_limit=True)
    kept: list[str] = []
    used = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[:max_lines]):
        cost = _size(line) + (1 if index else 0)
        if used + cost > max_bytes:
            truncated_by = "bytes"
            break
        kept.append(line)
        used += cost
    output = "\n".join(kept)
    return Truncation(output, True, truncated_by, len(lines), total_bytes, len(kept), _size(output))


def _tail_bytes(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    start = len(data) - max_bytes
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:].decode("utf-8")


def truncate_tail(content: str, max_lines: int = DEFAULT_MAX_LINES,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> Truncation:
    total_bytes = _size(content)
    lines = split_lines(content)
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return Truncation(content, False, None, len(lines), total_bytes, len(lines), total_bytes)
    kept: list[str] = []
    used = 0
    truncated_by = "lines"
    partial = False
    for line in reversed(lines):
        if len(kept) >= max_lines:
            break
        cost = _size(line) + (1 if kept else 0)
        if used + cost > max_bytes:
            truncated_by = "bytes"
            if not kept:
                kept.insert(0, _tail_bytes(line, max_bytes))
                partial = True
            break
        kept.insert(0, line)
        used += cost
    output = "\n".join(kept)
    return Truncation(output, True, truncated_by, len(lines), total_bytes, len(kept), _size(output),
                      last_line_partial=partial)


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars] + "... [truncated]", True
