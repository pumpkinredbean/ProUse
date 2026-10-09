"""edit: exact text replacement with pi's whitespace/Unicode-tolerant fallback.

Every edit is matched against the original file, not incrementally. Matching tries the
exact text first; if any edit only matches after normalization (trailing whitespace,
smart quotes, Unicode dashes and spaces, NFKC), the touched lines are rewritten from the
normalized text and every other line keeps its original bytes. CRLF line endings and a
UTF-8 BOM are preserved.
"""
from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any

from . import Result, ToolError
from .files import file_lock

if TYPE_CHECKING:
    from ..workspaces import Workspaces

_SINGLE_QUOTES = re.compile("[\u2018\u2019\u201a\u201b]")
_DOUBLE_QUOTES = re.compile("[\u201c\u201d\u201e\u201f]")
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPACES = re.compile("[\xa0\u2002-\u200a\u202f\u205f\u3000]")
_LINES_WITH_ENDINGS = re.compile(r"[^\n]*\n|[^\n]+")


def detect_line_ending(content: str) -> str:
    crlf = content.find("\r\n")
    lf = content.find("\n")
    if lf == -1 or crlf == -1:
        return "\n"
    return "\r\n" if crlf < lf else "\n"


def normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def normalize_for_fuzzy_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _SINGLE_QUOTES.sub("'", text)
    text = _DOUBLE_QUOTES.sub('"', text)
    text = _DASHES.sub("-", text)
    return _SPACES.sub(" ", text)


def _lines_with_endings(content: str) -> list[str]:
    return _LINES_WITH_ENDINGS.findall(content)


def _line_spans(content: str) -> list[tuple[int, int]]:
    spans, offset = [], 0
    for line in _lines_with_endings(content):
        spans.append((offset, offset + len(line)))
        offset += len(line)
    return spans


def _apply(content: str, replacements: list[tuple[int, int, str]], offset: int = 0) -> str:
    for index, length, new_text in sorted(replacements, reverse=True):
        start = index - offset
        content = content[:start] + new_text + content[start + length:]
    return content


def _replacement_lines(spans: list[tuple[int, int]], index: int, length: int) -> tuple[int, int]:
    start_line = next((number for number, (start, end) in enumerate(spans) if start <= index < end), -1)
    if start_line == -1:
        raise ToolError("Replacement range is outside the base content.")
    end_line = start_line
    while end_line < len(spans) and spans[end_line][1] < index + length:
        end_line += 1
    if end_line >= len(spans):
        raise ToolError("Replacement range is outside the base content.")
    return start_line, end_line + 1


def _apply_preserving_unchanged_lines(original: str, base: str,
                                      replacements: list[tuple[int, int, str]]) -> str:
    original_lines = _lines_with_endings(original)
    spans = _line_spans(base)
    if len(original_lines) != len(spans):
        raise ToolError("Cannot preserve unchanged lines because the normalized text has a different line count.")
    groups: list[list[Any]] = []
    for replacement in sorted(replacements):
        start_line, end_line = _replacement_lines(spans, replacement[0], replacement[1])
        if groups and start_line < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end_line)
            groups[-1][2].append(replacement)
        else:
            groups.append([start_line, end_line, [replacement]])
    result, position = [], 0
    for start_line, end_line, members in groups:
        result.append("".join(original_lines[position:start_line]))
        group_start, group_end = spans[start_line][0], spans[end_line - 1][1]
        result.append(_apply(base[group_start:group_end], members, group_start))
        position = end_line
    result.append("".join(original_lines[position:]))
    return "".join(result)


def _find(content: str, old_text: str) -> tuple[int, int, bool]:
    """Exact match first, then a match in normalized space. Returns (index, length, fuzzy)."""
    index = content.find(old_text)
    if index != -1:
        return index, len(old_text), False
    fuzzy_content = normalize_for_fuzzy_match(content)
    fuzzy_old = normalize_for_fuzzy_match(old_text)
    index = fuzzy_content.find(fuzzy_old)
    return (index, len(fuzzy_old), True) if index != -1 else (-1, 0, False)


def _occurrences(content: str, old_text: str) -> int:
    return normalize_for_fuzzy_match(content).count(normalize_for_fuzzy_match(old_text))


def apply_edits(content: str, edits: list[dict[str, str]], path: str) -> str:
    """Apply replacements to LF-normalized content and return the new LF-normalized content."""
    total = len(edits)
    normalized = [(normalize_to_lf(edit["oldText"]), normalize_to_lf(edit["newText"])) for edit in edits]
    for number, (old_text, _) in enumerate(normalized):
        if not old_text:
            raise ToolError(f"oldText must not be empty in {path}." if total == 1
                            else f"edits[{number}].oldText must not be empty in {path}.")
    fuzzy = any(_find(content, old_text)[2] for old_text, _ in normalized)
    base = normalize_for_fuzzy_match(content) if fuzzy else content
    matched: list[tuple[int, int, str, int]] = []
    for number, (old_text, new_text) in enumerate(normalized):
        index, length, _ = _find(base, old_text)
        if index == -1:
            raise ToolError(
                f"Could not find the exact text in {path}. The old text must match exactly including all "
                f"whitespace and newlines." if total == 1 else
                f"Could not find edits[{number}] in {path}. The oldText must match exactly including all "
                f"whitespace and newlines.")
        count = _occurrences(base, old_text)
        if count > 1:
            raise ToolError(
                f"Found {count} occurrences of the text in {path}. The text must be unique. Please provide more "
                f"context to make it unique." if total == 1 else
                f"Found {count} occurrences of edits[{number}] in {path}. Each oldText must be unique. Please "
                f"provide more context to make it unique.")
        matched.append((index, length, new_text, number))
    matched.sort()
    for previous, current in zip(matched, matched[1:]):
        if previous[0] + previous[1] > current[0]:
            raise ToolError(f"edits[{previous[3]}] and edits[{current[3]}] overlap in {path}. Merge them into "
                            f"one edit or target disjoint regions.")
    replacements = [(index, length, new_text) for index, length, new_text, _ in matched]
    result = (_apply_preserving_unchanged_lines(content, base, replacements) if fuzzy
              else _apply(base, replacements))
    if result == content:
        raise ToolError(
            f"No changes made to {path}. The replacement produced identical content. This might indicate an "
            f"issue with special characters or the text not existing as expected." if total == 1 else
            f"No changes made to {path}. The replacements produced identical content.")
    return result


def first_changed_line(before: str, after: str) -> int:
    old_lines, new_lines = before.split("\n"), after.split("\n")
    for number, (old, new) in enumerate(zip(old_lines, new_lines), start=1):
        if old != new:
            return number
    return min(len(old_lines), len(new_lines)) + 1


def edit(workspaces: Workspaces, path: str, edits: list[dict[str, str]]) -> Result:
    if not edits:
        raise ToolError("Edit tool input is invalid. edits must contain at least one replacement.")
    target = workspaces.resolve(path)
    with file_lock(target):
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            raise ToolError(f"Could not edit file: {path}. File not found.") from None
        except OSError as exc:
            raise ToolError(f"Could not edit file: {path}. {exc.strerror or exc}.") from None
        if b"\0" in raw[:8192]:
            raise ToolError(f"Could not edit file: {path}. It is a binary file.")
        text = raw.decode("utf-8", errors="surrogateescape")
        bom = "\ufeff" if text.startswith("\ufeff") else ""
        content = text[len(bom):]
        ending = detect_line_ending(content)
        before = normalize_to_lf(content)
        after = apply_edits(before, edits, path)
        target.write_bytes((bom + restore_line_endings(after, ending)).encode("utf-8", errors="surrogateescape"))
    line = first_changed_line(before, after)
    return Result(f"Successfully replaced {len(edits)} block(s) in {path}. First change at line {line}.")
