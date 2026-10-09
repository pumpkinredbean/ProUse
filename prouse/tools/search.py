"""grep and find. Both use ripgrep when it is installed (respecting .gitignore) and fall
back to a pure-Python walk that uses `git ls-files` for ignore rules when possible."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Iterator

from . import Result, ToolError
from .shell import shell_environment
from .truncate import DEFAULT_MAX_BYTES, GREP_MAX_LINE_LENGTH, format_size, truncate_head, truncate_line

if TYPE_CHECKING:
    from ..workspaces import Workspaces

GREP_DEFAULT_LIMIT = 100
FIND_DEFAULT_LIMIT = 1000
FALLBACK_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
FALLBACK_MAX_FILE_BYTES = 4 * 1024 * 1024
SEARCH_TIMEOUT_SECONDS = 40


def ripgrep() -> str | None:
    configured = os.environ.get("PROUSE_RG")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    path = shell_environment().get("PATH", os.environ.get("PATH", ""))
    return shutil.which("rg", path=path) or shutil.which("rg")


def _notices(result: str, notes: list[str]) -> str:
    return result + (f"\n\n[{'. '.join(notes)}]" if notes else "")


# ------------------------------------------------------------------ fallback walk

def _git_files(directory: Path) -> list[Path] | None:
    git = shutil.which("git", path=shell_environment().get("PATH")) or shutil.which("git")
    if git is None:
        return None
    try:
        output = subprocess.run([git, "-C", str(directory), "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=SEARCH_TIMEOUT_SECONDS, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted(directory / name for name in output.decode("utf-8", "surrogateescape").split("\0") if name)


def _walk(directory: Path) -> Iterator[Path]:
    files = _git_files(directory)
    if files is not None:
        yield from (path for path in files if path.is_file())
        return
    for current, dirs, names in os.walk(directory):
        dirs[:] = sorted(name for name in dirs if name not in FALLBACK_SKIP_DIRS)
        for name in sorted(names):
            yield Path(current) / name


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob with ** support. Patterns without '/' match the file name."""
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[index + 1:end].replace("\\", "\\\\")
                out.append("[^" + body[1:] + "]" if body.startswith("!") else "[" + body + "]")
                index = end
        elif char == "{":
            end = pattern.find("}", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                out.append("(?:" + "|".join(re.escape(part) for part in pattern[index + 1:end].split(",")) + ")")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def _glob_matcher(pattern: str):
    regex = _glob_regex(pattern.lstrip("/"))
    if "/" in pattern:
        return lambda relative: bool(regex.match(relative))
    return lambda relative: bool(regex.match(relative.rsplit("/", 1)[-1]))


# ------------------------------------------------------------------------- grep

def grep(workspaces: Workspaces, pattern: str, path: str | None = None, glob: str | None = None,
         ignore_case: bool = False, literal: bool = False, context: int | None = None,
         limit: int | None = None) -> Result:
    target = workspaces.resolve(path)
    if not target.exists():
        raise ToolError(f"Path not found: {path or workspaces.display(target)}")
    effective = max(1, limit or GREP_DEFAULT_LIMIT)
    around = max(0, context or 0)
    rg = ripgrep()
    if rg:
        matches, limit_reached = _grep_rg(rg, target, pattern, glob, ignore_case, literal, effective)
    else:
        matches, limit_reached = _grep_python(target, pattern, glob, ignore_case, literal, effective)
    if not matches:
        return Result("No matches found")
    cache: dict[Path, list[str]] = {}
    lines_truncated = False
    output: list[str] = []
    for file, number, text in matches:
        shown = workspaces.display(file)
        if around == 0:
            line, cut = truncate_line(text.replace("\r", "").rstrip("\n"))
            lines_truncated |= cut
            output.append(f"{shown}:{number}: {line}")
            continue
        if file not in cache:
            try:
                cache[file] = normalize_lines(file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                cache[file] = []
        lines = cache[file]
        if not lines:
            output.append(f"{shown}:{number}: (unable to read file)")
            continue
        for current in range(max(1, number - around), min(len(lines), number + around) + 1):
            line, cut = truncate_line(lines[current - 1])
            lines_truncated |= cut
            separator = ":" if current == number else "-"
            output.append(f"{shown}{separator}{current}{separator} {line}")
    truncation = truncate_head("\n".join(output), max_lines=10 ** 9)
    notes = []
    if limit_reached:
        notes.append(f"{effective} matches limit reached. Use limit={effective * 2} for more, or refine pattern")
    if truncation.truncated:
        notes.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
    if lines_truncated:
        notes.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read to see full lines")
    return Result(_notices(truncation.content, notes))


def normalize_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _grep_rg(rg: str, target: Path, pattern: str, glob: str | None, ignore_case: bool, literal: bool,
             limit: int) -> tuple[list[tuple[Path, int, str]], bool]:
    # rg's own --glob would override .gitignore, so the glob filter is applied here instead.
    args = [rg, "--json", "--line-number", "--color=never", "--hidden", "--glob", "!.git"]
    if ignore_case:
        args.append("--ignore-case")
    if literal:
        args.append("--fixed-strings")
    args += ["--", pattern, str(target)]
    matcher = _glob_matcher(glob) if glob and target.is_dir() else None
    matches: list[tuple[Path, int, str]] = []
    limit_reached = False
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=target if target.is_dir() else target.parent) as process:
        assert process.stdout is not None and process.stderr is not None
        for raw in process.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            name = data.get("path", {}).get("text")
            number = data.get("line_number")
            text = data.get("lines", {}).get("text")
            if text is None and "bytes" in data.get("lines", {}):
                text = "(binary or non-UTF-8 line)"
            if not name or not isinstance(number, int):
                continue
            file = Path(name)
            if matcher and not matcher(file.relative_to(target).as_posix()):
                continue
            matches.append((file, number, text or ""))
            if len(matches) >= limit:
                limit_reached = True
                process.kill()
                break
        stderr = process.stderr.read().decode("utf-8", "replace")
    if not limit_reached and process.returncode not in (0, 1):
        raise ToolError(stderr.strip() or f"ripgrep exited with code {process.returncode}")
    return matches, limit_reached


def _grep_python(target: Path, pattern: str, glob: str | None, ignore_case: bool, literal: bool,
                 limit: int) -> tuple[list[tuple[Path, int, str]], bool]:
    try:
        regex = re.compile(re.escape(pattern) if literal else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression: {exc}") from None
    matcher = _glob_matcher(glob) if glob else None
    files = [target] if target.is_file() else _walk(target)
    matches: list[tuple[Path, int, str]] = []
    for file in files:
        if matcher and target.is_dir() and not matcher(file.relative_to(target).as_posix()):
            continue
        try:
            if file.stat().st_size > FALLBACK_MAX_FILE_BYTES:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for number, line in enumerate(normalize_lines(data.decode("utf-8", "replace")), start=1):
            if regex.search(line):
                matches.append((file, number, line))
                if len(matches) >= limit:
                    return matches, True
    return matches, False


# ------------------------------------------------------------------------- find

def find(workspaces: Workspaces, pattern: str, path: str | None = None, limit: int | None = None) -> Result:
    target = workspaces.resolve(path)
    if not target.is_dir():
        raise ToolError(f"Not a directory: {path or workspaces.display(target)}")
    effective = max(1, limit or FIND_DEFAULT_LIMIT)
    rg = ripgrep()
    found = _find_rg(rg, target, pattern, effective) if rg else _find_python(target, pattern, effective)
    if not found:
        return Result("No files found matching pattern")
    truncation = truncate_head("\n".join(workspaces.display(item) for item in found), max_lines=10 ** 9)
    notes = []
    if len(found) >= effective:
        notes.append(f"{effective} results limit reached")
    if truncation.truncated:
        notes.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
    return Result(_notices(truncation.content, notes))


def _anchored(pattern: str) -> str:
    """pi's rule: a pattern with '/' matches anywhere below the search root."""
    if "/" in pattern and not pattern.startswith(("/", "**/")) and pattern != "**":
        return "**/" + pattern
    return pattern


def _find_rg(rg: str, target: Path, pattern: str, limit: int) -> list[Path]:
    # List every file rg does not ignore and match the glob here: rg's --glob would override .gitignore.
    args = [rg, "--files", "--hidden", "--color=never", "--glob", "!.git", str(target)]
    try:
        process = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=SEARCH_TIMEOUT_SECONDS, cwd=target)
    except subprocess.TimeoutExpired:
        raise ToolError("find timed out; narrow the path or pattern") from None
    if process.returncode not in (0, 1):
        raise ToolError(process.stderr.decode("utf-8", "replace").strip()
                        or f"ripgrep exited with code {process.returncode}")
    matcher = _glob_matcher(_anchored(pattern))
    names = sorted(line for line in process.stdout.decode("utf-8", "surrogateescape").split("\n") if line)
    found = [Path(name) for name in names if matcher(Path(name).relative_to(target).as_posix())]
    return found[:limit]


def _find_python(target: Path, pattern: str, limit: int) -> list[Path]:
    matcher = _glob_matcher(_anchored(pattern))
    found = []
    for file in _walk(target):
        if matcher(file.relative_to(target).as_posix()):
            found.append(file)
            if len(found) >= limit:
                break
    return found

