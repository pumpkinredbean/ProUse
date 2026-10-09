"""Registered workspace roots: the folders the file tools may touch.

The registry is re-read whenever the file changes, so `prouse workspace add` takes
effect without restarting the MCP client. Paths given to tools are absolute, `~`-based,
or relative to the default workspace, and must resolve (after following symlinks)
inside a registered root.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading

from .tools import ToolError
from .tools.truncate import format_size, truncate_head

INSTRUCTION_FILES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


@dataclass(frozen=True)
class Workspace:
    id: str
    label: str
    root: Path
    default: bool = False


@dataclass(frozen=True)
class Unavailable:
    id: str
    root: str
    reason: str


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def parse_registry(document: object, base: Path) -> tuple[list[Workspace], list[Unavailable]]:
    """Read enabled workspaces from a version 1 or 2 registry document."""
    if not isinstance(document, dict) or document.get("version") not in (1, 2):
        raise ValueError("registry must be an object with version 1 or 2")
    entries = document.get("workspaces", [])
    if not isinstance(entries, list):
        raise ValueError("registry workspaces must be a list")
    available: list[Workspace] = []
    unavailable: list[Unavailable] = []
    seen: set[str] = set()
    home = Path.home().resolve()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
                or not isinstance(entry.get("root"), str) or entry["id"] in seen:
            raise ValueError("each workspace needs a unique string id and root")
        seen.add(entry["id"])
        if entry.get("enabled", True) is not True:
            continue
        root = Path(entry["root"]).expanduser()
        root = Path(os.path.realpath(root if root.is_absolute() else base / root))
        if not root.is_dir():
            unavailable.append(Unavailable(entry["id"], entry["root"], "folder not found"))
        elif root == Path(root.anchor) or root == home:
            unavailable.append(Unavailable(entry["id"], entry["root"], "home or filesystem root is not allowed"))
        else:
            label = entry.get("label") if isinstance(entry.get("label"), str) else entry["id"]
            available.append(Workspace(entry["id"], label, root))
    wanted = document.get("default_workspace_id")
    default = next((item for item in available if item.id == wanted), None)
    if default is None and len(available) == 1:
        default = available[0]
    if default is not None:
        available = [Workspace(item.id, item.label, item.root, item is default) for item in available]
    return available, unavailable


class Workspaces:
    def __init__(self, registry: Path, extra_readable: tuple[Path, ...] = ()):
        self.registry = registry
        self.extra_readable = tuple(Path(os.path.realpath(path)) for path in extra_readable)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._loaded: tuple[list[Workspace], list[Unavailable]] = ([], [])

    def _load(self) -> tuple[list[Workspace], list[Unavailable]]:
        try:
            info = self.registry.stat()
        except FileNotFoundError:
            return [], []
        stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        with self._lock:
            if stamp != self._stamp:
                try:
                    document = json.loads(self.registry.read_text(encoding="utf-8"))
                    self._loaded = parse_registry(document, self.registry.parent)
                except (OSError, ValueError) as exc:
                    raise ToolError(f"Cannot read the ProUse workspace registry {self.registry}: {exc}") from None
                self._stamp = stamp
            return self._loaded

    def all(self) -> list[Workspace]:
        return self._load()[0]

    def unavailable(self) -> list[Unavailable]:
        return self._load()[1]

    def default(self) -> Workspace | None:
        return next((item for item in self.all() if item.default), None)

    def _require(self) -> list[Workspace]:
        spaces = self.all()
        if not spaces:
            raise ToolError("No workspace is registered. Ask the user to run "
                            "`prouse setup --workspace /path/to/project` on their computer.")
        return spaces

    def roots_text(self) -> str:
        return ", ".join(str(item.root) for item in self.all()) or "none"

    def containing(self, path: Path) -> Workspace | None:
        matches = [item for item in self.all() if _within(path, item.root)]
        return max(matches, key=lambda item: len(item.root.parts)) if matches else None

    def resolve(self, path: str | None, *, readable_extra: bool = False) -> Path:
        """Return the canonical path, refusing anything outside the registered roots."""
        self._require()
        raw = (path or "").strip()
        if raw.startswith("@"):
            raw = raw[1:]
        expanded = os.path.expanduser(raw or ".")
        if os.path.isabs(expanded):
            candidate = expanded
        else:
            default = self.default()
            if default is None:
                raise ToolError(f"The relative path {raw or '.'!r} is ambiguous: several workspaces are registered "
                                f"and none is the default. Use an absolute path inside one of: {self.roots_text()}")
            candidate = os.path.join(default.root, expanded)
        resolved = Path(os.path.realpath(candidate))
        if self.containing(resolved) is not None:
            return resolved
        if readable_extra and any(_within(resolved, root) for root in self.extra_readable):
            return resolved
        raise ToolError(f"{raw or '.'} is outside the registered workspaces. "
                        f"Use a path inside one of: {self.roots_text()}")

    def display(self, path: Path) -> str:
        """Show a path the way `resolve` would read it back: relative to the default root if inside it."""
        default = self.default()
        if default is not None and _within(path, default.root):
            relative = path.relative_to(default.root).as_posix()
            return relative or "."
        return str(path)

    def find(self, name: str) -> Workspace:
        spaces = self._require()
        for item in spaces:
            if name in (item.id, item.label):
                return item
        try:
            path = self.resolve(name)
            located = self.containing(path) if path.exists() else None
        except ToolError:
            located = None
        if located is None:
            raise ToolError(f"Unknown workspace {name!r}. Registered: "
                            + ", ".join(item.id for item in spaces))
        return located

    @staticmethod
    def instructions(workspace: Workspace) -> list[tuple[Path, str]]:
        """AGENTS.md / CLAUDE.md files from the root and its ancestors, outermost first, like pi."""
        found: list[tuple[Path, str]] = []
        directory = workspace.root
        while True:
            for name in INSTRUCTION_FILES:
                candidate = directory / name
                try:
                    if candidate.is_file():
                        text = candidate.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
                        found.insert(0, (candidate, text))
                        break
                except OSError:
                    continue
            if directory.parent == directory:
                return found
            directory = directory.parent

    def describe(self, name: str | None = None) -> str:
        spaces = self._require()
        selected = self.find(name) if name else (spaces[0] if len(spaces) == 1 else None)
        lines = ["Registered workspaces. The file tools and bash work inside these folders; "
                 "relative paths resolve against the default workspace."]
        for item in spaces:
            notes = []
            if item.default:
                notes.append("default")
            if item.label != item.id:
                notes.append(item.label)
            suffix = f" ({', '.join(notes)})" if notes else ""
            lines.append(f"- {item.id}{suffix}: {item.root}")
        for item in self.unavailable():
            lines.append(f"- {item.id}: {item.root} (unavailable: {item.reason})")
        if selected is None:
            lines.append("")
            lines.append("Call workspaces with name=<id> to load a workspace's project instructions "
                         "(AGENTS.md / CLAUDE.md) before working in it.")
            return "\n".join(lines)
        files = self.instructions(selected)
        lines.append("")
        if not files:
            lines.append(f"{selected.id} has no AGENTS.md or CLAUDE.md project instructions.")
            return "\n".join(lines)
        lines.append(f"Project instructions for {selected.id}. Follow them while working there:")
        for path, text in files:
            truncation = truncate_head(text)
            body = truncation.content
            if truncation.truncated:
                body += (f"\n\n[Truncated at {format_size(truncation.output_bytes)}. "
                         f"Read the rest with read path={path} offset={truncation.output_lines + 1}.]")
            lines.append(f'<project_instructions path="{path}">\n{body}\n</project_instructions>')
        return "\n".join(lines)
