"""Bounded, read-only context access. No model, network, shell or database calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


MAX_FILE_BYTES = 524_288
MAX_FILES = 1_000
MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_CHARS = 16_000
MAX_READ_CHARS = 10_000
MAX_READ_LINES = 240
MAX_BATCH = 8
DENIED_PARTS = {".git", ".state", ".cache", "node_modules", "target", ".venv"}
SECRET_CONTENT = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    rb"|\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}"
    rb"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    rb"|\bgh[pousr]_[A-Za-z0-9]{30,}"
)
ALLOWED_EXTENSIONS = {".rs", ".py", ".ts", ".tsx", ".js", ".mjs", ".md", ".sql", ".json", ".toml", ".lock", ".csv"}


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
            path = str(relative_path(item["path"]))
            extensions = set(item["extensions"])
            if not extensions or not extensions <= ALLOWED_EXTENSIONS:
                raise ContextError("Unsupported file extension in context policy")
            self.directories.append((path, extensions))
        if not self.files and not self.directories:
            raise ContextError("Empty context policy")
        self.calls = 0
        self.output_chars = 0

    def _allowed(self, path: str) -> bool:
        p = relative_path(path)
        return path in self.files or any(
            p.is_relative_to(PurePosixPath(directory)) and p.suffix in extensions
            for directory, extensions in self.directories
        )

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

    def _read(self, path: str) -> bytes:
        if not self._allowed(path):
            raise ContextError("Path is outside the context policy")
        parts = relative_path(path).parts
        parent = "/".join(parts[:-1]) or None
        directory_fd = self._open_directory(parent)
        fd = None
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContextError("Only regular workspace files without hardlinks are allowed")
            if info.st_size > MAX_FILE_BYTES:
                raise ContextError("File exceeds the context size limit; provide a bounded result")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or b"\0" in data:
                raise ContextError("Oversized or binary context file")
            if SECRET_CONTENT.search(data):
                raise ContextError("Credential-shaped content blocked; inspect locally")
            data.decode("utf-8")
            return data
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory_fd)

    def _walk(self, base: str, extensions: set[str]):
        # Walk directory descriptors: never follow a symlink during discovery.
        directory_fd = self._open_directory(base)
        try:
            with os.scandir(directory_fd) as entries:
                entries = sorted(entries, key=lambda item: item.name)
            for entry in entries:
                path = f"{base}/{entry.name}"
                try:
                    relative_path(path)
                except ContextError:
                    continue
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from self._walk(path, extensions)
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix in extensions:
                    yield path
        finally:
            os.close(directory_fd)

    def _snapshot(self):
        paths = set(self.files)
        for directory, extensions in self.directories:
            try:
                for path in self._walk(directory, extensions):
                    paths.add(path)
                    if len(paths) > MAX_FILES:
                        raise ContextError("Context has too many files; narrow the local policy")
            except OSError:
                raise ContextError("A configured context directory is unavailable") from None
        files = {}
        issues = []
        total_bytes = 0
        identity = []
        for path in sorted(paths):
            try:
                data = self._read(path)
            except (OSError, UnicodeError, ContextError) as exc:
                reason = str(exc) if isinstance(exc, ContextError) else "File unavailable or not UTF-8"
                issues.append({"path": path, "status": "unavailable", "reason": reason})
                identity.append([path, "unavailable", reason])
                continue
            total_bytes += len(data)
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise ContextError("Context exceeds the local snapshot size limit")
            text = data.decode("utf-8")
            metadata = {"path": path, "sha256": digest(data), "bytes": len(data), "lines": len(text.splitlines())}
            files[path] = (metadata, text)
            identity.append([path, metadata["sha256"]])
        identity = [self.workspace_id, str(self.root), self.policy_hash, identity]
        return self.view_prefix + digest(encoded(identity).encode()), files, issues

    def _base(self, view_id, files, issues):
        return {"workspace_id": self.workspace_id, "workspace": self.name, "view_id": view_id, "policy_sha256": self.policy_hash,
                "available_files": len(files), "unavailable_files": len(issues),
                "max_output_chars": MAX_OUTPUT_CHARS}

    def _finish(self, result):
        count = len(encoded(result))
        if count > MAX_OUTPUT_CHARS:
            raise ContextError("Output budget exceeded; request a smaller batch")
        self.calls += 1
        self.output_chars += count
        return result

    def _view(self, expected_view_id=None):
        view_id, files, issues = self._snapshot()
        base = self._base(view_id, files, issues)
        if expected_view_id and expected_view_id != view_id:
            return base | {"status": "changed_view", "message": "Files changed. Refresh the index or changed_files before combining evidence."}, {}, []
        return base, files, issues

    @staticmethod
    def _bounded_items(base, key, items, start=0, limit=50):
        result = base | {key: [], "total_matches": len(items), "next_offset": None}
        stop = min(len(items), start + limit)
        for item in items[start:stop]:
            result[key].append(item)
            if len(encoded(result)) > MAX_OUTPUT_CHARS - 160:
                result[key].pop()
                break
        end = start + len(result[key])
        if end < len(items):
            result["next_offset"] = end
        return result

    def index(self, query="", offset=0, limit=50, expected_view_id=None):
        if len(query) > 200 or not 0 <= offset <= MAX_FILES or not 1 <= limit <= 100:
            raise ContextError("Invalid index query or pagination")
        base, files, issues = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        items = [v[0] | {"status": "available"} for p, v in files.items() if query.lower() in p.lower()]
        items += [v for v in issues if query.lower() in v["path"].lower()]
        return self._finish(self._bounded_items(base, "files", sorted(items, key=lambda x: x["path"]), offset, limit))

    def search(self, queries, path_prefix="", offset=0, limit=40, expected_view_id=None):
        if not 1 <= len(queries) <= 6 or any(not isinstance(q, str) or not 1 <= len(q) <= 200 for q in queries):
            raise ContextError("Supply one to six literal search terms, each at most 200 characters")
        if path_prefix:
            # A directory selector may include its final slash. Normalize only
            # that suffix; absolute paths, traversal and internal empty parts
            # still pass through the same strict validation as file reads.
            path_prefix = str(relative_path(path_prefix.removesuffix("/")))
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 80:
            raise ContextError("Invalid search pagination")
        base, files, _ = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        results = []
        total = 0
        for path, (metadata, text) in files.items():
            if path_prefix and path != path_prefix and not path.startswith(path_prefix + "/"):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                matched = [q for q in queries if q.casefold() in line.casefold()]
                if matched:
                    if offset <= total < offset + limit:
                        results.append({"path": path, "sha256": metadata["sha256"], "line": number,
                                        "matched_queries": matched, "preview": line[:240],
                                        "preview_truncated": len(line) > 240})
                    total += 1
        result = self._bounded_items(base, "matches", results, 0, limit)
        result["total_matches"] = total
        end = offset + len(result["matches"])
        result["next_offset"] = end if end < total else None
        return self._finish(result)

    def read(self, requests, expected_view_id=None):
        if not 1 <= len(requests) <= MAX_BATCH:
            raise ContextError("Read one to eight file ranges in a batch")
        # Validate before touching the filesystem, and never echo a denied path.
        for request in requests:
            if not self._allowed(request["path"]):
                raise ContextError("A requested path is outside the context policy")
            start, end = request.get("start_line", 1), request.get("end_line")
            if type(start) is not int or start < 1 or (end is not None and (type(end) is not int or end < start)):
                raise ContextError("Invalid inclusive line range")
        base, files, issues = self._view(expected_view_id)
        if base.get("status"):
            return self._finish(base)
        results = []
        for request in requests:
            path = request["path"]
            if path not in files:
                results.append({"path": path, "status": "unavailable"})
                continue
            metadata, text = files[path]
            if request.get("sha256") and request["sha256"] != metadata["sha256"]:
                results.append(metadata | {"status": "changed_file"})
                continue
            lines = text.splitlines()
            start = request.get("start_line", 1)
            requested_end = min(request.get("end_line") or len(lines), len(lines))
            end = min(requested_end, start + MAX_READ_LINES - 1)
            if start > len(lines) and lines:
                results.append(metadata | {"status": "invalid_range"})
                continue
            selected = []
            chars = 0
            for number in range(start, end + 1):
                line = f"{number}: {lines[number - 1]}"
                if chars + len(line) + 1 > MAX_READ_CHARS:
                    break
                selected.append(line)
                chars += len(line) + 1
            last = start + len(selected) - 1
            results.append(metadata | {"status": "ok" if selected or not lines else "line_too_long",
                                      "start_line": start, "end_line": last, "text": "\n".join(selected),
                                      "next_line": last + 1 if last < requested_end else None})
        result = self._bounded_items(base, "files", results, 0, MAX_BATCH)
        if result["next_offset"] is not None:
            result["deferred_paths"] = [r["path"] for r in results[result["next_offset"]:]]
        return self._finish(result)

    def changes(self, known, offset=0, limit=50):
        if len(known) > MAX_FILES or not 0 <= offset <= MAX_FILES or not 1 <= limit <= 100:
            raise ContextError("Invalid known-file list or pagination")
        if any(not self._allowed(path) for path in known):
            raise ContextError("A known path is outside the context policy")
        base, files, issues = self._view()
        changed = []
        for path, (metadata, _) in files.items():
            if path in known and known[path] != metadata["sha256"]:
                changed.append(metadata | {"status": "changed"})
        for path in sorted(known.keys() - files.keys()):
            changed.append({"path": path, "status": "unavailable"})
        base["unchanged_files"] = sum(known.get(path) == metadata["sha256"] for path, (metadata, _) in files.items())
        return self._finish(self._bounded_items(base, "changes", sorted(changed, key=lambda x: x["path"]), offset, limit))
