"""Bounded context access. No model, network, shell or database calls.

The store exposes coding-agent filesystem primitives over an administrator-approved
allowlist: directory listing, glob discovery, ripgrep-backed literal and regex search
(with a built-in fallback), line-range and byte-window reads, path/hash change
detection, and exact file writes, edits and deletes.

Discovery is stat-only, and no call reads or hashes the whole workspace. Content
hashes are computed per file on demand and cached against the file identity
(device, inode, size, mtime_ns), so a repeated read or search only re-reads bytes
that actually changed. Every returned byte still passes the same path, symlink,
hardlink, binary and credential-shaped-content checks.
"""

from __future__ import annotations

import bisect
import base64
import codecs
import fnmatch
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections import OrderedDict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, NamedTuple

MAX_READ_CHARS = 10_000
MAX_READ_LINES = 240
MAX_BATCH = 8
MAX_OUTPUT_CHARS = 16_000
MAX_FILE_BYTES = 524_288
MAX_DISCOVERED_FILES = 200_000
MAX_KNOWN_FILES = 20_000
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_HASH_BYTES = 32 * 1024 * 1024
MAX_PATTERN_CHARS = 200
MAX_GLOBS = 16
MAX_DEPTH = 8
MAX_DIRECTORY_ENTRIES = 500
MAX_BYTE_READ = 12_000
MAX_CONTEXT_LINES = 20
MAX_CURSOR_CHARS = 512
MAX_EDITS = 32
CHUNK_BYTES = 65_536
HASH_CACHE_LIMIT = 50_000
RG = shutil.which("rg")
RG_ARG_CHUNK = 400
RG_TIMEOUT = 120
DENIED_PARTS = {".git", ".state", ".cache", "node_modules", "target", ".venv"}
SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    rb"|\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}"
    rb"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    rb"|\bgh[pousr]_[A-Za-z0-9]{30,}"
)
ALLOWED_EXTENSIONS = frozenset({
    ".adoc", ".bash", ".bib", ".c", ".cc", ".cfg", ".cjs", ".clj", ".cljs", ".conf", ".cpp", ".cs", ".css",
    ".csv", ".cxx", ".dart", ".erl", ".ex", ".exs", ".fish", ".go", ".gql", ".gradle", ".graphql", ".groovy",
    ".h", ".hcl", ".hh", ".hpp", ".hrl", ".hs", ".htm", ".html", ".hxx", ".ini", ".ipynb", ".java", ".jl",
    ".js", ".json", ".json5", ".jsonc", ".jsx", ".kt", ".kts", ".less", ".lock", ".lua", ".m", ".markdown",
    ".md", ".mdx", ".mjs", ".ml", ".mli", ".mm", ".nim", ".org", ".php", ".pl", ".pm", ".properties", ".proto",
    ".py", ".pyi", ".r", ".rb", ".rs", ".rst", ".sass", ".scala", ".scss", ".sh", ".sql", ".svelte", ".svg",
    ".swift", ".tex", ".tf", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".v", ".vue", ".xml", ".yaml", ".yml",
    ".zig", ".zsh", ".sv",
})
ALLOWED_FILENAMES = frozenset({
    "AUTHORS", "Brewfile", "CHANGELOG", "CHANGES", "CMakeLists.txt", "CODEOWNERS", "CONTRIBUTING", "Containerfile",
    "Dockerfile", "Gemfile", "GNUmakefile", "INSTALL", "Jenkinsfile", "Justfile", "LICENSE", "LICENCE", "Makefile",
    "NOTICE", "Procfile", "README", "Rakefile", "TODO", "VERSION", "Vagrantfile", "justfile", "makefile",
    ".babelrc", ".clang-format", ".clang-tidy", ".dockerignore", ".editorconfig", ".eslintrc", ".flake8",
    ".gitattributes", ".gitignore", ".mailmap", ".npmignore", ".nvmrc", ".prettierignore", ".prettierrc",
    ".pylintrc", ".python-version", ".ruby-version", ".shellcheckrc", ".stylelintrc", ".terraform-version",
    ".tool-versions", ".watchmanconfig",
})


class ContextError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ContextError("Expected a workspace-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ContextError("Absolute paths and traversal are not allowed")
    for part in path.parts:
        low = part.lower()
        if low in DENIED_PARTS or low.startswith(".env") or any(
            word in low for word in ("credential", "secret", "keystore", "private_key")
        ):
            raise ContextError("Path is outside the context policy")
    if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        raise ContextError("Path is outside the context policy")
    return path


def _reason(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, ContextError) else "File unavailable or not UTF-8"


def _count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _read_all(fd: int, limit: int) -> bytes:
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _decode_window(data: bytes) -> str:
    for trim in range(4):
        try:
            return data[trim:].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _translate_glob(pattern: str) -> str:
    return "^" + _glob_body(pattern) + "$"


def _glob_body(pattern: str) -> str:
    out = []
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                index += 2
                if index < length and pattern[index] == "/":
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape("["))
                index += 1
            else:
                body = pattern[index + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                index = end + 1
        elif char == "{":
            end = index + 1
            depth = 1
            while end < length and depth:
                if pattern[end] == "{":
                    depth += 1
                elif pattern[end] == "}":
                    depth -= 1
                end += 1
            if depth:
                out.append(re.escape("{"))
                index += 1
            else:
                alternatives, current, nested = [], "", 0
                for inner in pattern[index + 1:end - 1]:
                    if inner == "{":
                        nested += 1
                    elif inner == "}":
                        nested -= 1
                    if inner == "," and nested == 0:
                        alternatives.append(current)
                        current = ""
                    else:
                        current += inner
                alternatives.append(current)
                out.append("(?:" + "|".join(_glob_body(alt) for alt in alternatives) + ")")
                index = end
        else:
            out.append(re.escape(char))
            index += 1
    return "".join(out)


@functools.lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    return re.compile(_translate_glob(pattern))


def glob_match(pattern: str, path: str) -> bool:
    """Match a workspace-relative path. A pattern without '/' matches the basename."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)
    return _glob_regex(pattern).match(path) is not None


def _valid_glob(pattern: Any) -> bool:
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN_CHARS or "\0" in pattern:
        return False
    if pattern.startswith("/") or any(part == ".." for part in pattern.split("/")):
        return False
    return True


class _Budget:
    __slots__ = ("limit", "used", "exhausted")

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.exhausted = False

    def remaining(self) -> int:
        return self.limit - self.used

    def take(self, amount: int) -> bool:
        if self.used + amount > self.limit:
            self.exhausted = True
            return False
        self.used += amount
        return True


class FileEntry(NamedTuple):
    path: str
    size: int
    mtime_ns: int
    ino: int
    dev: int


_HASH_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()


def _cache_get(key: tuple, identity: tuple):
    entry = _HASH_CACHE.get(key)
    if entry is None or entry[0] != identity:
        return None
    _HASH_CACHE.move_to_end(key)
    return entry[1]


def _cache_put(key: tuple, identity: tuple, value: tuple) -> None:
    _HASH_CACHE[key] = (identity, value)
    _HASH_CACHE.move_to_end(key)
    while len(_HASH_CACHE) > HASH_CACHE_LIMIT:
        _HASH_CACHE.popitem(last=False)


class ContextStore:
    def __init__(self, root: Path, policy: dict[str, Any], workspace_id: str = "legacy"):
        self.root = root.resolve(strict=True)
        self.workspace_id = workspace_id
        info = self.root.stat()
        self.root_identity = (info.st_dev, info.st_ino)
        self.view_prefix = workspace_id + ":" + digest(encoded([str(self.root), self.root_identity]).encode())[:16] + ":"
        if not self.root.is_dir() or policy.get("version") != 1:
            raise ContextError("Expected a directory and context policy version 1")
        self.name = str(policy.get("name", "advisor-context"))
        self.policy_hash = digest(encoded(policy).encode())
        self.files = {str(relative_path(p)) for p in policy.get("files", [])}
        self.directories: list[tuple[str, set[str]]] = []
        for item in policy.get("directories", []):
            # "." (or "") selects the approved workspace root itself.
            raw = item["path"]
            path = "" if raw in ("", ".") else str(relative_path(raw))
            extensions = {str(extension).lower() for extension in item["extensions"]}
            if not extensions or not extensions <= ALLOWED_EXTENSIONS:
                raise ContextError("Unsupported file extension in context policy")
            self.directories.append((path, extensions))
        if not self.files and not self.directories:
            raise ContextError("Empty context policy")
        self.cache_prefix = (workspace_id, str(self.root), self.policy_hash)
        self.calls = 0
        self.output_chars = 0

    # ------------------------------------------------------------------ policy

    def _allowed(self, path: str) -> bool:
        p = relative_path(path)
        if path in self.files:
            return True
        suffix = p.suffix.lower()
        name = p.name
        return any(
            p.is_relative_to(PurePosixPath(directory)) and (suffix in extensions or name in ALLOWED_FILENAMES)
            for directory, extensions in self.directories
        )

    def _in_scope(self, path: str) -> bool:
        """True when a path is, contains, or is contained by an approved policy path."""
        p = PurePosixPath(path or ".")
        for candidate in [PurePosixPath(directory) for directory, _ in self.directories] + [
            PurePosixPath(item) for item in self.files
        ]:
            if p == candidate or p.is_relative_to(candidate) or candidate.is_relative_to(p):
                return True
        return False

    # ------------------------------------------------------------- filesystem

    def _open_directory(self, path: str | None = None) -> int:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.root_identity:
                raise ContextError("Approved workspace root was replaced")
            for part in (() if path is None else relative_path(path).parts):
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, path: str) -> tuple[int, os.stat_result]:
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        parts = relative_path(path).parts
        parent = "/".join(parts[:-1]) or None
        directory_fd = self._open_directory(parent)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError:
            raise ContextError("File is missing") from None
        except OSError:
            raise ContextError("File is unavailable") from None
        finally:
            os.close(directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContextError("Only regular workspace files without hardlinks are allowed")
            return fd, info
        except BaseException:
            os.close(fd)
            raise

    def _stat(self, path: str) -> FileEntry | None:
        if not self._allowed(path):
            return None
        try:
            parts = relative_path(path).parts
            parent = "/".join(parts[:-1]) or None
            directory_fd = self._open_directory(parent)
            try:
                info = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            finally:
                os.close(directory_fd)
        except (OSError, ContextError):
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return FileEntry(path, info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)

    def _walk(self, base: str, extensions: set[str]) -> Iterator[FileEntry]:
        # Walk directory descriptors: never follow a symlink during discovery.
        directory_fd = self._open_directory(base or None)
        try:
            with os.scandir(directory_fd) as scandir:
                entries = sorted(scandir, key=lambda item: item.name)
            for entry in entries:
                path = f"{base}/{entry.name}" if base else entry.name
                try:
                    relative_path(path)
                except ContextError:
                    continue
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from self._walk(path, extensions)
                elif entry.is_file(follow_symlinks=False):
                    if Path(entry.name).suffix.lower() not in extensions and entry.name not in ALLOWED_FILENAMES:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        continue
                    yield FileEntry(path, info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)
        finally:
            os.close(directory_fd)

    def _discover(self) -> tuple[list[FileEntry], list[dict[str, Any]], bool]:
        """Stat-only discovery of approved paths. No file content is read."""
        entries: dict[str, FileEntry] = {}
        unavailable: list[dict[str, Any]] = []
        truncated = False
        for path in sorted(self.files):
            entry = self._stat(path)
            if entry is None:
                unavailable.append({"path": path, "status": "unavailable",
                                    "reason": "File is missing or not a readable regular file"})
            else:
                entries[path] = entry
        for directory, extensions in self.directories:
            try:
                for entry in self._walk(directory, extensions):
                    entries[entry.path] = entry
                    if len(entries) >= MAX_DISCOVERED_FILES:
                        truncated = True
                        break
            except OSError:
                raise ContextError("A configured context directory is unavailable") from None
            if truncated:
                break
        return [entries[path] for path in sorted(entries)], unavailable, truncated

    # ----------------------------------------------------------------- hashes

    def _view_id(self, entries: list[FileEntry]) -> str:
        hasher = hashlib.sha256()
        hasher.update(encoded([self.workspace_id, str(self.root), self.policy_hash]).encode())
        for entry in entries:
            hasher.update(f"{entry.path}\0{entry.size}\0{entry.mtime_ns}\n".encode())
        return self.view_prefix + hasher.hexdigest()

    def _cached(self, path: str, info: os.stat_result):
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        return _cache_get(self.cache_prefix + (path,), identity), identity

    def _probe(self, path: str, budget: _Budget) -> dict[str, Any]:
        """Hash one file on demand. Returns metadata, an unavailable status, or a deferred hash."""
        try:
            fd, info = self._open_file(path)
        except (OSError, ContextError) as exc:
            return {"status": "unavailable", "reason": _reason(exc), "sha256": None}
        try:
            cached, identity = self._cached(path, info)
            if cached is not None:
                return {"sha256": cached[0], "lines": cached[1], "bytes": info.st_size, "hash_status": "ok"}
            if not budget.take(info.st_size):
                return {"sha256": None, "bytes": info.st_size, "hash_status": "deferred"}
            return self._digest(fd, info, identity, path)
        finally:
            os.close(fd)

    def _digest(self, fd: int, info: os.stat_result, identity: tuple, path: str) -> dict[str, Any]:
        key = self.cache_prefix + (path,)
        if info.st_size <= MAX_FILE_BYTES:
            data = _read_all(fd, MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return {"status": "unavailable", "reason": "File changed while reading", "sha256": None}
            if b"\0" in data:
                return {"status": "unavailable", "reason": "Binary content is not exposed", "sha256": None,
                        "bytes": info.st_size}
            if SECRET_CONTENT.search(data):
                return {"status": "unavailable", "reason": "Credential-shaped content blocked; inspect locally",
                        "sha256": None, "bytes": info.st_size}
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"status": "unavailable", "reason": "File is not valid UTF-8", "sha256": None,
                        "bytes": info.st_size}
            sha, lines = digest(data), _count_lines(text)
        else:
            hasher = hashlib.sha256()
            lines = 0
            head = b""
            last = b""
            while True:
                chunk = os.read(fd, CHUNK_BYTES)
                if not chunk:
                    break
                if len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                lines += chunk.count(b"\n")
                last = chunk[-1:]
                hasher.update(chunk)
            if last and last != b"\n":
                lines += 1
            if b"\0" in head:
                return {"status": "unavailable", "reason": "Binary content is not exposed", "sha256": None,
                        "bytes": info.st_size}
            if SECRET_CONTENT.search(head):
                return {"status": "unavailable", "reason": "Credential-shaped content blocked; inspect locally",
                        "sha256": None, "bytes": info.st_size}
            sha = hasher.hexdigest()
        _cache_put(key, identity, (sha, lines))
        return {"sha256": sha, "lines": lines, "bytes": info.st_size, "hash_status": "ok"}

    # ------------------------------------------------------------------ output

    def _base(self, view_id: str | None = None, entries=None, unavailable=None) -> dict[str, Any]:
        base: dict[str, Any] = {"workspace_id": self.workspace_id, "workspace": self.name, "view_id": view_id,
                                "policy_sha256": self.policy_hash, "max_output_chars": MAX_OUTPUT_CHARS}
        if entries is not None:
            base["available_files"] = len(entries)
        if unavailable is not None:
            base["unavailable_files"] = len(unavailable)
        return base

    def _finish(self, result):
        count = len(encoded(result))
        if count > MAX_OUTPUT_CHARS:
            raise ContextError("Output budget exceeded; request a smaller batch")
        self.calls += 1
        self.output_chars += count
        return result

    def _view(self, expected_view_id=None):
        entries, unavailable, truncated = self._discover()
        view_id = self._view_id(entries)
        base = self._base(view_id, entries, unavailable) | {"discovery_truncated": truncated}
        if expected_view_id and expected_view_id != view_id:
            return base | {"status": "changed_view",
                           "message": "Files changed. Refresh the index or changed_files before combining evidence."}, None, None
        return base, entries, unavailable

    def _base_for(self, expected_view_id):
        if expected_view_id is None:
            return self._base(None)
        base, _, _ = self._view(expected_view_id)
        return base

    @staticmethod
    def _fit(result: dict[str, Any], key: str, items: list, limit: int) -> dict[str, Any]:
        result[key] = []
        for item in items[:limit]:
            result[key].append(item)
            if len(result[key]) > 1 and len(encoded(result)) > MAX_OUTPUT_CHARS - 200:
                result[key].pop()
                result["truncated"] = True
                break
        if len(items) > len(result[key]):
            result["truncated"] = True
        return result

    # ------------------------------------------------------------------- tools

    def index(self, query: str = "", offset: int = 0, limit: int = 50, expected_view_id=None,
              include_hashes: bool = True):
        """Discover approved paths; hashes are computed lazily for the returned page only."""
        if len(query) > MAX_PATTERN_CHARS or not 0 <= offset <= MAX_DISCOVERED_FILES or not 1 <= limit <= 200:
            raise ContextError("Invalid index query or pagination")
        entries, unavailable, truncated = self._discover()
        view_id = self._view_id(entries)
        base = self._base(view_id, entries, unavailable) | {"discovery_truncated": truncated}
        if expected_view_id and expected_view_id != view_id:
            return self._finish(base | {"status": "changed_view",
                                        "message": "Files changed. Refresh the index or changed_files before combining evidence."})
        needle = query.lower()
        items = [{"path": entry.path, "bytes": entry.size, "status": "available"}
                 for entry in entries if needle in entry.path.lower()]
        items += [item for item in unavailable if needle in item["path"].lower()]
        items.sort(key=lambda item: item["path"])
        page = items[offset:offset + limit]
        budget = _Budget(MAX_HASH_BYTES)
        for item in page:
            if item["status"] != "available" or not include_hashes:
                continue
            probed = self._probe(item["path"], budget)
            if probed.get("status") == "unavailable":
                item.update({"status": "unavailable", "reason": probed.get("reason"), "sha256": None})
            else:
                item.update({"sha256": probed.get("sha256"), "lines": probed.get("lines"),
                             "hash_status": probed.get("hash_status")})
        end = offset + len(page)
        return self._finish(base | {"files": page, "total_files": len(items),
                                    "next_offset": end if end < len(items) else None})

    def list_directory(self, path: str = "", depth: int = 1, offset: int = 0, limit: int = 200,
                       expected_view_id=None):
        """List approved directories and readable files below one approved directory."""
        if not 1 <= depth <= MAX_DEPTH or not 1 <= limit <= MAX_DIRECTORY_ENTRIES or offset < 0:
            raise ContextError("Invalid directory listing request")
        base_path = "" if path in ("", ".") else str(relative_path(path.removesuffix("/")))
        base, _, _ = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        if not self._in_scope(base_path):
            raise ContextError("Directory is outside the context policy")
        items, truncated = self._list(base_path, depth)
        page = items[offset:offset + limit]
        end = offset + len(page)
        return self._finish(base | {"path": base_path, "entries": page, "total_entries": len(items),
                                    "next_offset": end if end < len(items) else None, "truncated": truncated})

    def _list(self, base: str, depth: int) -> tuple[list[dict[str, Any]], bool]:
        items: list[dict[str, Any]] = []
        truncated = False

        def visit(path: str, level: int) -> None:
            nonlocal truncated
            if level > depth or truncated:
                return
            directory_fd = self._open_directory(path or None)
            try:
                with os.scandir(directory_fd) as scandir:
                    entries = sorted(scandir, key=lambda item: item.name)
                for entry in entries:
                    child = f"{path}/{entry.name}" if path else entry.name
                    try:
                        relative_path(child)
                    except ContextError:
                        continue
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not self._in_scope(child):
                            continue
                        items.append({"path": child, "name": entry.name, "type": "directory"})
                        visit(child, level + 1)
                    elif entry.is_file(follow_symlinks=False) and self._allowed(child):
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        items.append({"path": child, "name": entry.name, "type": "file", "bytes": info.st_size})
                    if len(items) >= MAX_DISCOVERED_FILES:
                        truncated = True
                        return
            finally:
                os.close(directory_fd)

        visit(base, 1)
        items.sort(key=lambda item: item["path"])
        return items, truncated

    def glob_files(self, patterns, exclude=(), offset: int = 0, limit: int = 200, expected_view_id=None):
        """Match approved paths against glob patterns ('**' spans directories)."""
        if not 1 <= len(patterns) <= MAX_GLOBS or any(not _valid_glob(pattern) for pattern in patterns):
            raise ContextError("Supply one to sixteen valid glob patterns")
        if len(exclude) > MAX_GLOBS or any(not _valid_glob(pattern) for pattern in exclude):
            raise ContextError("Supply at most sixteen valid exclusion patterns")
        if not 0 <= offset <= MAX_DISCOVERED_FILES or not 1 <= limit <= 200:
            raise ContextError("Invalid glob pagination")
        base, entries, _ = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        matched = [entry for entry in entries
                   if any(glob_match(pattern, entry.path) for pattern in patterns)
                   and not any(glob_match(pattern, entry.path) for pattern in exclude)]
        page = matched[offset:offset + limit]
        budget = _Budget(MAX_HASH_BYTES)
        items = []
        for entry in page:
            probed = self._probe(entry.path, budget)
            item = {"path": entry.path, "bytes": entry.size, "sha256": probed.get("sha256")}
            if probed.get("status") == "unavailable":
                item.update({"status": "unavailable", "reason": probed.get("reason")})
            elif probed.get("sha256") is None:
                item["hash_status"] = "deferred"
            items.append(item)
        end = offset + len(items)
        return self._finish(base | {"files": items, "total_files": len(matched),
                                    "next_offset": end if end < len(matched) else None})

    def search(self, queries, path_prefix: str = "", limit: int = 40, cursor=None, expected_view_id=None,
               include_globs=(), exclude_globs=(), case_sensitive: bool = False, before: int = 0, after: int = 0,
               output_mode: str = "matches"):
        """Literal multi-term search with resumable cursor pagination."""
        if not 1 <= len(queries) <= 6 or any(
            not isinstance(query, str) or not 1 <= len(query) <= MAX_PATTERN_CHARS for query in queries
        ):
            raise ContextError("Supply one to six literal search terms, each at most 200 characters")
        spec = {"mode": "literal", "terms": list(queries), "case_sensitive": case_sensitive}
        return self._scan(spec, True, path_prefix, include_globs, exclude_globs, before, after, limit,
                          cursor, output_mode, expected_view_id)

    def grep(self, pattern: str, regex: bool = True, path_prefix: str = "", limit: int = 40, cursor=None,
             expected_view_id=None, include_globs=(), exclude_globs=(), case_sensitive: bool = False,
             before: int = 0, after: int = 0, output_mode: str = "matches"):
        """Regex (or literal) search with glob filters, context lines and cursor pagination."""
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN_CHARS or "\0" in pattern:
            raise ContextError("Supply a pattern of one to 200 characters")
        if regex:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ContextError(f"Invalid regular expression: {exc}") from None
            spec = {"mode": "regex", "pattern": pattern, "case_sensitive": case_sensitive}
        else:
            spec = {"mode": "literal", "terms": [pattern], "case_sensitive": case_sensitive}
        return self._scan(spec, False, path_prefix, include_globs, exclude_globs, before, after, limit,
                          cursor, output_mode, expected_view_id)

    def _scan(self, spec, report_terms, path_prefix, include_globs, exclude_globs,
              before, after, limit, cursor, output_mode, expected_view_id):
        if output_mode not in {"matches", "files", "count"}:
            raise ContextError("output_mode must be matches, files or count")
        if not 1 <= limit <= 200:
            raise ContextError("Invalid search limit")
        if not 0 <= before <= MAX_CONTEXT_LINES or not 0 <= after <= MAX_CONTEXT_LINES:
            raise ContextError("Context lines must be between 0 and 20")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_CHARS or "\0" in cursor):
            raise ContextError("Invalid search cursor")
        if len(include_globs) > MAX_GLOBS or any(not _valid_glob(pattern) for pattern in include_globs):
            raise ContextError("Supply at most sixteen valid include patterns")
        if len(exclude_globs) > MAX_GLOBS or any(not _valid_glob(pattern) for pattern in exclude_globs):
            raise ContextError("Supply at most sixteen valid exclusion patterns")
        base, entries, _ = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        prefix = str(relative_path(path_prefix.removesuffix("/"))) if path_prefix else ""
        candidates = [entry for entry in entries
                      if self._selected(entry.path, prefix, include_globs, exclude_globs)]
        start_index, start_line = self._resume(candidates, cursor)
        remaining = candidates[start_index:]
        budget = _Budget(MAX_SCAN_BYTES)
        scan_set: list[FileEntry] = []
        for entry in remaining:
            if scan_set and budget.used + entry.size > budget.limit:
                budget.exhausted = True
                break
            budget.used += entry.size
            scan_set.append(entry)
        overflow = remaining[len(scan_set):]
        outcome = self._scan_rg(spec, report_terms, scan_set, start_line, before, after,
                                output_mode) if RG else None
        if outcome is None:
            outcome = self._scan_py(spec, report_terms, scan_set, start_line, before, after, output_mode)
        items = outcome["items"]
        hash_budget = _Budget(MAX_HASH_BYTES)
        probed: dict[str, dict[str, Any]] = {}
        for item in items:
            path = item["path"]
            if path not in probed:
                probed[path] = self._probe(path, hash_budget)
            probe = probed[path]
            if probe.get("status") == "unavailable":
                item["sha256"] = None
            else:
                item["sha256"] = probe.get("sha256")
                if probe.get("sha256") is None:
                    item["hash_status"] = "deferred"
        key = {"matches": "matches", "files": "files", "count": "counts"}[output_mode]
        per_file = output_mode != "matches"
        kept: list[dict[str, Any]] = []
        complete = True
        resume = None
        for item in items:
            if len(kept) >= limit:
                complete = False
                resume = f"{item['path']}:1" if per_file else f"{item['path']}:{item.get('line', 1)}"
                break
            kept.append(item)
            if len(kept) > 1 and len(encoded(base | {key: kept})) > MAX_OUTPUT_CHARS - 400:
                kept.pop()
                complete = False
                resume = f"{item['path']}:1" if per_file else f"{item['path']}:{item.get('line', 1)}"
                break
        if complete and overflow:
            complete = False
            resume = f"{overflow[0].path}:1"
        total = sum(item.get("count", 1) for item in kept)
        result = base | {key: kept, "total_matches": total, "files_scanned": len(scan_set),
                         "scanned_bytes": budget.used, "scan_complete": complete, "next_cursor": resume}
        if outcome["unavailable"]:
            result["skipped_files"] = outcome["unavailable"]
        return self._finish(result)

    def _scan_rg(self, spec, report_terms, scan_set, start_line, before, after, output_mode):
        """ripgrep engine over the approved files; None means fall back to the built-in scanner."""
        paths = [entry.path for entry in scan_set]
        flagged = self._rg_secret_scan(paths)
        if flagged is None:
            return None
        args = ["--json", "--line-number", "--with-filename"]
        if not spec["case_sensitive"]:
            args.append("-i")
        if spec["mode"] == "regex":
            args += ["-e", spec["pattern"]]
        else:
            args.append("-F")
            for term in spec["terms"]:
                args += ["-e", term]
        if before:
            args.append(f"-B{before}")
        if after:
            args.append(f"-A{after}")
        search_paths = [path for path in paths if path not in flagged]
        ran = self._rg_run(args, search_paths)
        if ran is None:
            return None
        raw, errored = ran
        files = self._rg_collect(raw)
        items: list[dict[str, Any]] = []
        unavailable = [{"path": path, "status": "unavailable",
                        "reason": "Credential-shaped content blocked; inspect locally"}
                       for path in paths if path in flagged]
        term_matcher = self._matcher_for(spec, True) if report_terms else None
        first = paths[0] if paths else None
        for path in search_paths:
            info = files.get(path)
            if info is None:
                if path in errored:
                    unavailable.append({"path": path, "status": "unavailable",
                                        "reason": "File unavailable or not UTF-8"})
                continue
            if info["bad"]:
                unavailable.append({"path": path, "status": "unavailable", "reason": info["bad"]})
                continue
            match_lines = info["matches"]
            if path == first and start_line > 1:
                match_lines = [number for number in match_lines if number >= start_line]
            if output_mode == "files":
                if match_lines:
                    items.append({"path": path, "line": match_lines[0]})
            elif output_mode == "count":
                if match_lines:
                    items.append({"path": path, "count": len(match_lines)})
            else:
                ordered = sorted(info["text"])
                for number in match_lines:
                    line_text = info["text"][number]
                    item: dict[str, Any] = {"path": path, "line": number, "text": line_text[:240],
                                            "preview_truncated": len(line_text) > 240}
                    if term_matcher is not None:
                        item["matched_queries"] = term_matcher(line_text)
                    if before:
                        item["before"] = [{"line": mark, "text": info["text"][mark][:240]}
                                          for mark in ordered if mark < number][-before:]
                    if after:
                        item["after"] = [{"line": mark, "text": info["text"][mark][:240]}
                                         for mark in ordered if mark > number][:after]
                    items.append(item)
        return {"items": items, "unavailable": unavailable}

    def _rg_run(self, args: list[str], paths: list[str]) -> tuple[bytes, set[str]] | None:
        """One rg invocation over approved paths; None when rg cannot serve this request.
        Returns the JSON event stream plus the paths rg reported errors for."""
        if not paths:
            return b"", set()
        out, errors = [], set()
        known = set(paths)
        for index in range(0, len(paths), RG_ARG_CHUNK):
            try:
                proc = subprocess.run([RG, "--no-config", "--no-ignore", "--hidden",
                                       *args, "--", *paths[index:index + RG_ARG_CHUNK]],
                                      cwd=self.root, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=RG_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                return None
            if proc.returncode not in (0, 1):
                return None
            out.append(proc.stdout)
            for line in proc.stderr.decode("utf-8", "replace").splitlines():
                name = line.split(":", 1)[0]
                if name in known:
                    errors.add(name)
        return b"".join(out), errors

    def _rg_secret_scan(self, paths: list[str]):
        ran = self._rg_run(["--files-with-matches", "--no-messages",
                            "-e", SECRET_CONTENT.pattern.decode("ascii")], paths)
        if ran is None:
            return None
        return set(ran[0].decode("utf-8", "replace").splitlines())

    @staticmethod
    def _rg_collect(raw: bytes) -> dict[str, dict[str, Any]]:
        files: dict[str, dict[str, Any]] = {}
        current = None
        for raw_line in raw.splitlines():
            try:
                event = json.loads(raw_line)
            except ValueError:
                continue
            kind = event.get("type")
            data = event.get("data") or {}
            if kind == "begin":
                path = data.get("path") or {}
                name = path.get("text")
                if name is None:
                    try:
                        name = base64.b64decode(path.get("bytes", "")).decode("utf-8")
                    except Exception:
                        current = None
                        continue
                current = files.setdefault(name, {"text": {}, "matches": [], "bad": None})
            elif kind in ("match", "context"):
                if current is None:
                    continue
                line = data.get("lines") or {}
                text = line.get("text")
                if text is None:
                    try:
                        text = base64.b64decode(line.get("bytes", "")).decode("utf-8")
                    except Exception:
                        current["bad"] = "File is not valid UTF-8"
                        continue
                if text.endswith("\n"):
                    text = text[:-1]
                if text.endswith("\r"):
                    text = text[:-1]
                if "\0" in text:
                    current["bad"] = "Binary content is not exposed"
                    continue
                number = data.get("line_number")
                if number is not None:
                    current["text"][number] = text
                    if kind == "match":
                        current["matches"].append(number)
            elif kind == "end":
                if current is not None and data.get("binary_offset") is not None:
                    current["bad"] = "Binary content is not exposed"
        return files

    def _scan_py(self, spec, report_terms, scan_set, start_line, before, after, output_mode):
        """Built-in scanner used when ripgrep is unavailable or cannot serve the pattern."""
        matcher = self._matcher_for(spec, report_terms)
        literal_queries = spec.get("terms") if report_terms else None
        items: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        for index, entry in enumerate(scan_set):
            line = start_line if index == 0 else 1
            outcome = self._scan_file(entry, matcher, literal_queries, line, before, after,
                                      1 << 30, _Budget(max(entry.size, 1)), output_mode)
            if outcome["status"] != "ok":
                unavailable.append({"path": entry.path, "status": "unavailable",
                                    "reason": outcome.get("reason", "File unavailable or not UTF-8")})
                continue
            items.extend(outcome["matches"])
        return {"items": items, "unavailable": unavailable}

    @staticmethod
    def _matcher_for(spec, report_terms):
        if spec["mode"] == "regex":
            compiled = re.compile(spec["pattern"], 0 if spec["case_sensitive"] else re.IGNORECASE)

            def matcher(line: str):
                return compiled.search(line) is not None
        else:
            terms = spec["terms"]
            folded = terms if spec["case_sensitive"] else [term.casefold() for term in terms]

            def matcher(line: str):
                haystack = line if spec["case_sensitive"] else line.casefold()
                hits = [term for term, needle in zip(terms, folded) if needle in haystack]
                return hits if report_terms else bool(hits)
        return matcher

    def _selected(self, path: str, prefix: str, include_globs, exclude_globs) -> bool:
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            return False
        if include_globs and not any(glob_match(pattern, path) for pattern in include_globs):
            return False
        if exclude_globs and any(glob_match(pattern, path) for pattern in exclude_globs):
            return False
        return True

    @staticmethod
    def _resume(candidates: list[FileEntry], cursor) -> tuple[int, int]:
        if not cursor:
            return 0, 1
        path, _, line = str(cursor).rpartition(":")
        index = bisect.bisect_left([entry.path for entry in candidates], path)
        if index < len(candidates) and candidates[index].path == path and line.isdigit() and int(line) > 0:
            return index, int(line)
        return index, 1

    def _scan_file(self, entry: FileEntry, matcher, literal_queries, start_line: int, before: int, after: int,
                   limit: int, budget: _Budget, output_mode: str) -> dict[str, Any]:
        try:
            fd, info = self._open_file(entry.path)
        except (OSError, ContextError) as exc:
            return {"status": "unavailable", "reason": _reason(exc), "matches": []}
        try:
            keep = info.st_size <= MAX_FILE_BYTES
            collected: list[bytes] | None = [] if keep else None
            head = b""
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            buffer = ""
            number = 0
            count = 0
            matches: list[dict[str, Any]] = []
            before_lines: deque = deque(maxlen=before)
            after_remaining = 0
            resume_line = None
            done = False
            eof = False
            while not done:
                chunk = os.read(fd, CHUNK_BYTES)
                if not chunk:
                    eof = True
                    break
                if not budget.take(len(chunk)):
                    resume_line = number + 1
                    break
                if collected is not None:
                    collected.append(chunk)
                elif len(head) < 4096:
                    head += chunk[:4096 - len(head)]
                try:
                    buffer += decoder.decode(chunk)
                except UnicodeDecodeError:
                    return {"status": "unavailable", "reason": "File is not valid UTF-8", "matches": []}
                while not done:
                    cut = buffer.find("\n")
                    if cut < 0:
                        break
                    raw = buffer[:cut]
                    buffer = buffer[cut + 1:]
                    if raw.endswith("\r"):
                        raw = raw[:-1]
                    number += 1
                    if number < start_line:
                        continue
                    found = matcher(raw)
                    if output_mode == "matches":
                        if found:
                            item: dict[str, Any] = {"path": entry.path, "line": number,
                                                    "text": raw[:240], "preview_truncated": len(raw) > 240}
                            if literal_queries is not None:
                                item["matched_queries"] = found
                            if before:
                                item["before"] = [{"line": n, "text": t} for n, t in before_lines]
                            if after:
                                item["after"] = []
                            matches.append(item)
                            after_remaining = after
                            if len(matches) >= limit:
                                resume_line = number + 1
                                done = True
                        else:
                            if after_remaining > 0 and matches:
                                matches[-1]["after"].append({"line": number, "text": raw[:240]})
                                after_remaining -= 1
                        if before:
                            before_lines.append((number, raw[:240]))
                    elif output_mode == "count":
                        if found:
                            count += 1
                    else:
                        if found:
                            count += 1
                            done = True
            if not done and buffer:
                raw = buffer[:-1] if buffer.endswith("\r") else buffer
                number += 1
                if number >= start_line and budget.remaining() > 0:
                    found = matcher(raw)
                    if output_mode == "matches":
                        if found:
                            item = {"path": entry.path, "line": number, "text": raw[:240],
                                    "preview_truncated": len(raw) > 240}
                            if literal_queries is not None:
                                item["matched_queries"] = found
                            if before:
                                item["before"] = [{"line": n, "text": t} for n, t in before_lines]
                            if after:
                                item["after"] = []
                            matches.append(item)
                    elif output_mode == "files":
                        if found:
                            count += 1
                            done = True
                    elif found:
                        count += 1
            if collected is not None and not eof:
                # Small file: finish the read so the checks and the reported hash cover every byte.
                while True:
                    chunk = os.read(fd, CHUNK_BYTES)
                    if not chunk:
                        break
                    collected.append(chunk)
                eof = True
            scanned = b"".join(collected) if collected is not None else head
            if b"\0" in scanned:
                return {"status": "unavailable", "reason": "Binary content is not exposed", "matches": []}
            if SECRET_CONTENT.search(scanned):
                return {"status": "unavailable", "reason": "Credential-shaped content blocked; inspect locally",
                        "matches": []}
            sha = None
            cached, _ = self._cached(entry.path, info)
            if cached is not None:
                sha = cached[0]
            elif collected is not None:
                sha = digest(b"".join(collected))
            if output_mode == "files":
                if count:
                    return {"status": "ok", "matches": [{"path": entry.path, "line": number, "sha256": sha}],
                            "resume_line": None}
                return {"status": "ok", "matches": [], "resume_line": None}
            if output_mode == "count":
                if not count:
                    return {"status": "ok", "matches": [], "resume_line": resume_line}
                return {"status": "ok", "matches": [{"path": entry.path, "count": count, "sha256": sha}],
                        "resume_line": resume_line}
            for item in matches:
                item["sha256"] = sha
            return {"status": "ok", "matches": matches, "resume_line": resume_line}
        finally:
            os.close(fd)

    def read(self, requests, expected_view_id=None):
        if not 1 <= len(requests) <= MAX_BATCH:
            raise ContextError("Read one to eight file ranges in a batch")
        for request in requests:
            if not self._allowed(request["path"]):
                raise ContextError("A requested path is outside the context policy")
            start, end = request.get("start_line", 1), request.get("end_line")
            if type(start) is not int or start < 1 or (end is not None and (type(end) is not int or end < start)):
                raise ContextError("Invalid inclusive line range")
            if request.get("sha256") is not None and not isinstance(request["sha256"], str):
                raise ContextError("Expected a SHA256 string")
        base = self._base_for(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        budget = _Budget(MAX_HASH_BYTES)
        results = [self._read_range(request, budget) for request in requests]
        result = self._fit(base, "files", results, MAX_BATCH)
        if result.get("truncated"):
            result["deferred_paths"] = [item["path"] for item in results[len(result["files"]):]]
        return self._finish(result)

    def _read_range(self, request, budget: _Budget) -> dict[str, Any]:
        path = request["path"]
        expected = request.get("sha256")
        start = request.get("start_line", 1)
        requested_end = request.get("end_line")
        try:
            fd, info = self._open_file(path)
        except (OSError, ContextError) as exc:
            return {"path": path, "status": "unavailable", "reason": _reason(exc)}
        try:
            cached, identity = self._cached(path, info)
            if info.st_size <= MAX_FILE_BYTES:
                return self._read_small(fd, info, path, start, requested_end, expected, identity, cached)
            return self._read_large(fd, info, path, start, requested_end, expected, identity, cached, budget)
        finally:
            os.close(fd)

    def _read_small(self, fd, info, path, start, requested_end, expected, identity, cached) -> dict[str, Any]:
        data = _read_all(fd, MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return {"path": path, "status": "unavailable", "reason": "File changed while reading"}
        if b"\0" in data:
            return {"path": path, "status": "unavailable", "reason": "Binary content is not exposed"}
        if SECRET_CONTENT.search(data):
            return {"path": path, "status": "unavailable",
                    "reason": "Credential-shaped content blocked; inspect locally"}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"path": path, "status": "unavailable", "reason": "File is not valid UTF-8"}
        sha = digest(data)
        lines = _split_lines(text)
        total = len(lines)
        _cache_put(self.cache_prefix + (path,), identity, (sha, total))
        metadata = {"path": path, "sha256": sha, "bytes": info.st_size, "lines": total}
        if expected and expected != sha:
            return metadata | {"status": "changed_file"}
        if start > total and total:
            return metadata | {"status": "invalid_range"}
        requested_end = min(requested_end or total, total)
        end = min(requested_end, start + MAX_READ_LINES - 1)
        selected: list[str] = []
        chars = 0
        for number in range(start, end + 1):
            line = f"{number}: {lines[number - 1]}"
            if chars + len(line) + 1 > MAX_READ_CHARS:
                break
            selected.append(line)
            chars += len(line) + 1
        last = start + len(selected) - 1
        if not selected and total:
            return metadata | {"status": "line_too_long", "start_line": start, "end_line": start - 1, "text": "",
                               "next_line": start, "eof": False,
                               "hint": "Use read_file_bytes for a byte window of this line"}
        return metadata | {"status": "ok", "start_line": start, "end_line": last, "text": "\n".join(selected),
                           "next_line": last + 1 if last < requested_end else None, "eof": last >= total}

    def _read_large(self, fd, info, path, start, requested_end, expected, identity, cached, budget) -> dict[str, Any]:
        sha = cached[0] if cached else None
        total_lines = cached[1] if cached else None
        hasher = None
        if expected and sha is None:
            if not budget.take(info.st_size):
                return {"path": path, "status": "unverified", "sha256": None, "bytes": info.st_size,
                        "reason": "File is too large to hash within this call; read it without an expected hash"}
            hasher = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer = ""
        number = 0
        selected: list[str] = []
        chars = 0
        collecting = True
        eof = False
        while True:
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                eof = True
                break
            if hasher is not None:
                hasher.update(chunk)
            try:
                buffer += decoder.decode(chunk)
            except UnicodeDecodeError:
                return {"path": path, "status": "unavailable", "reason": "File is not valid UTF-8"}
            while True:
                cut = buffer.find("\n")
                if cut < 0:
                    break
                raw = buffer[:cut]
                buffer = buffer[cut + 1:]
                if raw.endswith("\r"):
                    raw = raw[:-1]
                number += 1
                if number < start or not collecting:
                    continue
                if requested_end is not None and number > requested_end:
                    collecting = False
                    continue
                if len(selected) >= MAX_READ_LINES:
                    collecting = False
                    continue
                line = f"{number}: {raw}"
                if chars + len(line) + 1 > MAX_READ_CHARS:
                    collecting = False
                    continue
                selected.append(line)
                chars += len(line) + 1
            if hasher is None and not collecting:
                break
        if eof and buffer:
            raw = buffer[:-1] if buffer.endswith("\r") else buffer
            number += 1
            if (collecting and number >= start and (requested_end is None or number <= requested_end)
                    and len(selected) < MAX_READ_LINES):
                line = f"{number}: {raw}"
                if chars + len(line) + 1 <= MAX_READ_CHARS:
                    selected.append(line)
        if hasher is not None:
            sha = hasher.hexdigest()
            total_lines = number if eof else None
            _cache_put(self.cache_prefix + (path,), identity, (sha, total_lines))
        if hasher is None and not eof:
            eof = not os.read(fd, 1)
        metadata = {"path": path, "sha256": sha, "bytes": info.st_size, "lines": total_lines}
        if expected and sha != expected:
            return metadata | {"status": "changed_file"}
        text = "\n".join(selected)
        if b"\0" in text.encode():
            return {"path": path, "status": "unavailable", "reason": "Binary content is not exposed"}
        if SECRET_CONTENT.search(text.encode()):
            return {"path": path, "status": "unavailable",
                    "reason": "Credential-shaped content blocked; inspect locally"}
        if not selected and number and start > number:
            return metadata | {"status": "invalid_range"}
        if not selected and number:
            return metadata | {"status": "line_too_long", "start_line": start, "end_line": start - 1, "text": "",
                               "next_line": start, "eof": False,
                               "hint": "Use read_file_bytes for a byte window of this line"}
        last = start + len(selected) - 1
        if requested_end is not None:
            next_line = last + 1 if last < requested_end else None
        elif total_lines is not None:
            next_line = last + 1 if last < total_lines else None
        else:
            next_line = None if eof else last + 1
        return metadata | {"status": "ok", "start_line": start, "end_line": last, "text": text,
                           "next_line": next_line, "eof": eof}

    def read_bytes(self, path: str, offset: int = 0, limit: int = 8_000, sha256=None, expected_view_id=None):
        """Read one exact byte window, so oversized single lines remain readable."""
        if not self._allowed(path):
            raise ContextError("A requested path is outside the context policy")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_BYTE_READ:
            raise ContextError("Invalid byte window")
        if sha256 is not None and not isinstance(sha256, str):
            raise ContextError("Expected a SHA256 string")
        base = self._base_for(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        try:
            fd, info = self._open_file(path)
        except (OSError, ContextError) as exc:
            return self._finish(base | {"path": path, "status": "unavailable", "reason": _reason(exc)})
        try:
            cached, _ = self._cached(path, info)
            sha = cached[0] if cached else None
            if sha256 is not None and sha != sha256:
                probed = self._probe(path, _Budget(MAX_HASH_BYTES))
                if probed.get("status") == "unavailable":
                    return self._finish(base | {"path": path, "status": "unavailable", "sha256": None,
                                                "reason": probed.get("reason")})
                sha = probed.get("sha256")
                if sha is None:
                    return self._finish(base | {"path": path, "status": "unverified", "sha256": None,
                                                "bytes": info.st_size,
                                                "reason": "File is too large to hash within this call"})
                if sha != sha256:
                    return self._finish(base | {"path": path, "status": "changed_file", "sha256": sha,
                                                "bytes": info.st_size})
            data = os.pread(fd, limit, offset)
            if b"\0" in data:
                return self._finish(base | {"path": path, "status": "unavailable",
                                            "reason": "Binary content is not exposed"})
            if SECRET_CONTENT.search(data):
                return self._finish(base | {"path": path, "status": "unavailable",
                                            "reason": "Credential-shaped content blocked; inspect locally"})
            text = _decode_window(data)
            eof = offset + len(data) >= info.st_size
            return self._finish(base | {"path": path, "status": "ok", "sha256": sha, "bytes": info.st_size,
                                        "offset": offset, "next_offset": None if eof else offset + len(data),
                                        "eof": eof, "text": text, "chars": len(text)})
        finally:
            os.close(fd)

    def changes(self, known: dict[str, str], offset: int = 0, limit: int = 50, expected_view_id=None):
        """Compare a known path/hash map and report added, modified and deleted paths."""
        if len(known) > MAX_KNOWN_FILES or not 0 <= offset <= MAX_KNOWN_FILES or not 1 <= limit <= 200:
            raise ContextError("Invalid known-file list or pagination")
        if any(not self._allowed(path) for path in known):
            raise ContextError("A known path is outside the context policy")
        entries, unavailable, truncated = self._discover()
        view_id = self._view_id(entries)
        base = self._base(view_id, entries, unavailable) | {"discovery_truncated": truncated}
        if expected_view_id and expected_view_id != view_id:
            return self._finish(base | {"status": "changed_view",
                                        "message": "Files changed. Refresh the index or changed_files before combining evidence."})
        current = {entry.path: entry for entry in entries}
        known_paths = sorted(known)
        budget = _Budget(MAX_HASH_BYTES)
        added: list[dict[str, Any]] = []
        for path in sorted(set(current) - set(known)):
            probed = self._probe(path, budget)
            item = {"path": path, "status": "added", "sha256": probed.get("sha256"), "bytes": current[path].size}
            if probed.get("status") == "unavailable":
                item["reason"] = probed.get("reason")
            elif probed.get("sha256") is None:
                item["hash_status"] = "deferred"
            added.append(item)
        modified: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        unchanged = 0
        deferred = 0
        next_offset = None
        for position in range(offset, len(known_paths)):
            path = known_paths[position]
            entry = current.get(path)
            if entry is None:
                continue
            probed = self._probe(path, budget)
            sha = probed.get("sha256")
            if probed.get("status") == "unavailable":
                blocked.append({"path": path, "status": "unavailable", "reason": probed.get("reason")})
                continue
            if sha is None:
                deferred += 1
                if budget.exhausted:
                    next_offset = position
                    break
                continue
            if sha != known[path]:
                modified.append({"path": path, "status": "modified", "sha256": sha, "bytes": entry.size,
                                 "previous_sha256": known[path]})
            else:
                unchanged += 1
        deleted = [{"path": path, "status": "deleted"} for path in sorted(set(known) - set(current))]
        result = base | {"added": [], "modified": [], "deleted": [], "blocked": [], "unchanged_files": unchanged,
                         "deferred_files": deferred, "next_offset": next_offset,
                         "total_changes": len(added) + len(modified) + len(deleted)}
        self._fit(result, "added", added, limit)
        self._fit(result, "modified", modified, limit)
        self._fit(result, "deleted", deleted, limit)
        self._fit(result, "blocked", blocked, limit)
        return self._finish(result)

    # ------------------------------------------------------------------ writes

    def _open_write(self, path: str, create: bool) -> tuple[int, os.stat_result]:
        """Open one approved path for writing; never follows a final symlink."""
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        parts = relative_path(path).parts
        parent = "/".join(parts[:-1]) or None
        directory_fd = self._open_directory(parent)
        try:
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
            try:
                fd = os.open(parts[-1], flags, 0o644, dir_fd=directory_fd)
            except FileNotFoundError:
                raise ContextError("File is missing") from None
            except OSError:
                raise ContextError("File is unavailable") from None
        finally:
            os.close(directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContextError("Only regular workspace files without hardlinks are allowed")
            return fd, info
        except BaseException:
            os.close(fd)
            raise

    def _current_bytes(self, path: str) -> tuple[bytes, os.stat_result]:
        """Read one approved file's full current bytes for a write precondition or edit."""
        fd, info = self._open_file(path)
        try:
            data = _read_all(fd, MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ContextError("File exceeds the writable size limit")
            return data, info
        finally:
            os.close(fd)

    @staticmethod
    def _checked_content(data: bytes) -> bytes:
        if len(data) > MAX_FILE_BYTES:
            raise ContextError("File content exceeds the writable size limit")
        if b"\0" in data:
            raise ContextError("Binary content is not writable")
        if SECRET_CONTENT.search(data):
            raise ContextError("Credential-shaped content is not writable")
        return data

    def _commit(self, path: str, data: bytes, expected_identity: tuple | None, create: bool) -> None:
        """Replace one approved file's bytes; expected_identity pins the read-time inode."""
        fd, info = self._open_write(path, create)
        try:
            if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
                raise ContextError("File was replaced during the write")
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            new_info = os.fstat(fd)
        finally:
            os.close(fd)
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        _cache_put(self.cache_prefix + (path,),
                   (new_info.st_dev, new_info.st_ino, new_info.st_size, new_info.st_mtime_ns),
                   (digest(data), lines))

    def write_file(self, path: str, content: str, expected_sha256=None, create: bool = True):
        """Create or fully overwrite one approved file with exact UTF-8 content."""
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        if not isinstance(content, str):
            raise ContextError("Expected UTF-8 text content")
        data = self._checked_content(content.encode("utf-8"))
        entry = self._stat(path)
        if entry is None and not create:
            raise ContextError("File is missing")
        previous_sha = None
        identity = None
        if entry is not None:
            current, info = self._current_bytes(path)
            previous_sha = digest(current)
            identity = (info.st_dev, info.st_ino)
            if expected_sha256 is not None and previous_sha != expected_sha256:
                raise ContextError("File content changed; re-read it before overwriting")
        elif expected_sha256 is not None:
            raise ContextError("File is missing")
        self._commit(path, data, identity, create)
        return self._finish(self._base() | {
            "path": str(relative_path(path)), "status": "ok", "created": entry is None,
            "sha256": digest(data), "previous_sha256": previous_sha,
            "bytes": len(data), "lines": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)})

    def edit_file(self, path: str, edits: list[dict[str, Any]], expected_sha256=None):
        """Apply ordered exact-string replacements to one approved file."""
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        if not 1 <= len(edits) <= MAX_EDITS:
            raise ContextError("Apply one to thirty-two edits per call")
        current, info = self._current_bytes(path)
        previous_sha = digest(current)
        if expected_sha256 is not None and previous_sha != expected_sha256:
            raise ContextError("File content changed; re-read it before editing")
        try:
            text = current.decode("utf-8")
        except UnicodeDecodeError:
            raise ContextError("File is not valid UTF-8") from None
        replacements = 0
        for edit in edits:
            old, new = edit.get("old"), edit.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                raise ContextError("Each edit needs a non-empty 'old' string and a 'new' string")
            count = text.count(old)
            if count == 0:
                raise ContextError("Edit text not found in " + path + "; re-read the file")
            if count > 1 and not edit.get("replace_all"):
                raise ContextError(
                    "Edit text matches " + str(count) + " locations in " + path +
                    "; include more context or set replace_all")
            text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
            replacements += count if edit.get("replace_all") else 1
        data = self._checked_content(text.encode("utf-8"))
        self._commit(path, data, (info.st_dev, info.st_ino), create=False)
        return self._finish(self._base() | {
            "path": str(relative_path(path)), "status": "ok", "replacements": replacements,
            "sha256": digest(data), "previous_sha256": previous_sha, "bytes": len(data)})

    def delete_file(self, path: str, expected_sha256=None):
        """Delete one approved regular file and return its last content hash."""
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        current, info = self._current_bytes(path)
        previous_sha = digest(current)
        if expected_sha256 is not None and previous_sha != expected_sha256:
            raise ContextError("File content changed; re-read it before deleting")
        parts = relative_path(path).parts
        parent = "/".join(parts[:-1]) or None
        directory_fd = self._open_directory(parent)
        try:
            now = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
                raise ContextError("File was replaced during the delete")
            os.unlink(parts[-1], dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        return self._finish(self._base() | {
            "path": str(relative_path(path)), "status": "ok", "deleted": True,
            "previous_sha256": previous_sha, "bytes": info.st_size})
