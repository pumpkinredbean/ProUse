"""read, write and ls, following pi's tool contracts."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

from . import Result, ToolError
from .truncate import DEFAULT_MAX_BYTES, format_size, truncate_head

if TYPE_CHECKING:
    from ..workspaces import Workspaces

MAX_TEXT_FILE_BYTES = 32 * 1024 * 1024
# Keep an attached image well below what MCP clients accept in one tool result.
MAX_IMAGE_BYTES = 700 * 1024
IMAGE_RESIZE_EDGE = 1568

_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def file_lock(path: Path) -> threading.Lock:
    """Serialize writes to one file across concurrent tool calls (pi's mutation queue)."""
    with _file_locks_guard:
        return _file_locks.setdefault(str(path), threading.Lock())


def image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _shrink_image(path: Path) -> bytes | None:
    """Resize with macOS `sips` when the image is too large to attach as is."""
    sips = shutil.which("sips")
    if sips is None:
        return None
    with tempfile.TemporaryDirectory(prefix="prouse-image-") as directory:
        target = Path(directory) / "image.jpg"
        try:
            subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "80",
                            "-Z", str(IMAGE_RESIZE_EDGE), str(path), "--out", str(target)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=True)
            return target.read_bytes()
        except (OSError, subprocess.SubprocessError):
            return None


def _read_image(path: Path, shown: str, mime: str, size: int) -> Result:
    data = path.read_bytes()
    note = f"Read image file [{mime}]"
    if size > MAX_IMAGE_BYTES:
        smaller = _shrink_image(path)
        if smaller is None or len(smaller) > MAX_IMAGE_BYTES:
            raise ToolError(f"{shown} is a {format_size(size)} image, too large to attach. Make a smaller copy "
                            f"with bash (for example `sips -Z {IMAGE_RESIZE_EDGE} {shown} --out /tmp/preview.jpg` "
                            f"on macOS) and read that inside a workspace.")
        data, mime = smaller, "image/jpeg"
        note = f"Read image file [{mime}], resized to at most {IMAGE_RESIZE_EDGE}px to fit the attachment limit"
    return Result(note, images=[(base64.b64encode(data).decode("ascii"), mime)])


def read(workspaces: Workspaces, path: str, offset: int | None = None, limit: int | None = None) -> Result:
    target = workspaces.resolve(path, readable_extra=True)
    shown = path
    try:
        info = target.stat()
    except FileNotFoundError:
        raise ToolError(f"File not found: {shown}") from None
    if target.is_dir():
        raise ToolError(f"{shown} is a directory. Use ls to list it.")
    if offset is not None and offset < 1:
        raise ToolError("offset is 1-based and must be at least 1")
    if limit is not None and limit < 1:
        raise ToolError("limit must be at least 1")
    with target.open("rb") as handle:
        head = handle.read(8192)
    mime = image_type(head)
    if mime:
        return _read_image(target, shown, mime, info.st_size)
    if b"\0" in head:
        raise ToolError(f"{shown} is a binary file ({format_size(info.st_size)}). Inspect it with bash "
                        f"(for example `file`, `xxd | head`).")
    if info.st_size > MAX_TEXT_FILE_BYTES:
        raise ToolError(f"{shown} is {format_size(info.st_size)}, too large to load. Read parts of it with bash "
                        f"(for example `sed -n '1,200p' {shown}` or `tail -n 200 {shown}`).")
    text = target.read_bytes().decode("utf-8", errors="replace").removeprefix("\ufeff")
    if not text:
        return Result("(empty file)")
    lines = text.split("\n")
    start = (offset or 1) - 1
    if start >= len(lines):
        raise ToolError(f"Offset {offset} is beyond end of file ({len(lines)} lines total)")
    end = min(start + limit, len(lines)) if limit is not None else len(lines)
    selected = "\n".join(lines[start:end])
    truncation = truncate_head(selected)
    first = start + 1
    if truncation.first_line_exceeds_limit:
        size = format_size(len(lines[start].encode("utf-8")))
        return Result(f"[Line {first} is {size}, exceeds {format_size(DEFAULT_MAX_BYTES)} limit. Use bash: "
                      f"sed -n '{first}p' {shown} | head -c {DEFAULT_MAX_BYTES}]")
    if truncation.truncated:
        last = first + truncation.output_lines - 1
        limit_note = "" if truncation.truncated_by == "lines" else f" ({format_size(DEFAULT_MAX_BYTES)} limit)"
        return Result(f"{truncation.content}\n\n[Showing lines {first}-{last} of {len(lines)}{limit_note}. "
                      f"Use offset={last + 1} to continue.]")
    if limit is not None and end < len(lines):
        return Result(f"{truncation.content}\n\n[{len(lines) - end} more lines in file. "
                      f"Use offset={end + 1} to continue.]")
    return Result(truncation.content)


def write(workspaces: Workspaces, path: str, content: str) -> Result:
    target = workspaces.resolve(path)
    if target.is_dir():
        raise ToolError(f"{path} is a directory")
    with file_lock(target):
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    size = len(content.encode("utf-8"))
    return Result(f"Successfully {'overwrote' if existed else 'created'} {path} ({size} bytes)")


def ls(workspaces: Workspaces, path: str | None = None, limit: int | None = None) -> Result:
    target = workspaces.resolve(path)
    shown = path or workspaces.display(target)
    if not target.exists():
        raise ToolError(f"Path not found: {shown}")
    if not target.is_dir():
        raise ToolError(f"Not a directory: {shown}")
    effective = limit if limit and limit > 0 else 500
    try:
        names = sorted(os.listdir(target), key=str.lower)
    except OSError as exc:
        raise ToolError(f"Cannot read directory: {exc.strerror or exc}") from None
    entries: list[str] = []
    limit_reached = False
    for name in names:
        if len(entries) >= effective:
            limit_reached = True
            break
        try:
            suffix = "/" if (target / name).is_dir() else ""
        except OSError:
            continue
        entries.append(name + suffix)
    if not entries:
        return Result("(empty directory)")
    truncation = truncate_head("\n".join(entries), max_lines=10 ** 9)
    notices = []
    if limit_reached:
        notices.append(f"{effective} entries limit reached. Use limit={effective * 2} for more")
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
    output = truncation.content
    if notices:
        output += f"\n\n[{'. '.join(notices)}]"
    return Result(output)
