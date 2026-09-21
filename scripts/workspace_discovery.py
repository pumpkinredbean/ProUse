"""Read recent local project roots from the installed Codex app-server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time


class WorkspaceDiscoveryError(RuntimeError):
    pass


THREAD_SOURCES = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


def _is_private_or_ephemeral(path: Path) -> bool:
    default_codex_home = (Path.home() / ".codex").resolve()
    configured_codex_home = Path(os.environ.get("CODEX_HOME", str(default_codex_home))).resolve()
    codex_roots = {default_codex_home, configured_codex_home}
    temp_roots = {Path(tempfile.gettempdir()).resolve()}
    for value in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"):
        try:
            temp_roots.add(Path(value).resolve())
        except OSError:
            pass
    if any(path == root or path.is_relative_to(root) for root in temp_roots):
        return True
    if any(path == root or path.is_relative_to(root) for root in codex_roots):
        return True
    return ".state" in path.parts


def _send(process: subprocess.Popen, message: dict) -> None:
    if process.stdin is None:
        raise WorkspaceDiscoveryError("Codex app-server input is unavailable")
    try:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise WorkspaceDiscoveryError("Codex app-server closed during workspace discovery") from exc


def _receive(process: subprocess.Popen, request_id: int, deadline: float) -> dict:
    if process.stdout is None:
        raise WorkspaceDiscoveryError("Codex app-server output is unavailable")
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        line = process.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise WorkspaceDiscoveryError("Codex app-server returned malformed workspace data") from exc
        if isinstance(message, dict) and message.get("id") == request_id:
            if "error" in message:
                raise WorkspaceDiscoveryError("Codex app-server rejected workspace discovery")
            result = message.get("result")
            if not isinstance(result, dict):
                raise WorkspaceDiscoveryError("Codex app-server returned invalid workspace data")
            return result
    raise WorkspaceDiscoveryError("Timed out while reading recent Codex workspaces")


def recent_workspaces(codex_bin: Path, cwd: Path, *, limit: int = 200, timeout: float = 10.0) -> list[dict]:
    """Return recent unique existing CWDs without starting a Codex model turn."""
    process = None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            [str(codex_bin), "app-server", "--listen", "stdio://"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            close_fds=True,
        )
        _send(process, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "workspace-orchestrator-admin", "title": "ProUse Admin", "version": "1"}
        }})
        _receive(process, 1, deadline)
        _send(process, {"method": "initialized", "params": {}})
        _send(process, {"id": 2, "method": "thread/list", "params": {
            "limit": limit,
            "sortKey": "recency_at",
            "sortDirection": "desc",
            "sourceKinds": THREAD_SOURCES,
        }})
        result = _receive(process, 2, deadline)
        threads = result.get("data")
        if not isinstance(threads, list):
            raise WorkspaceDiscoveryError("Codex app-server omitted the recent thread list")
        found = []
        seen = set()
        for thread in threads:
            if not isinstance(thread, dict) or not isinstance(thread.get("cwd"), str):
                continue
            try:
                path = Path(thread["cwd"]).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if (not path.is_dir() or not path.is_absolute() or
                    path in (Path(path.anchor), Path.home().resolve()) or _is_private_or_ephemeral(path)):
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "path": key,
                "label": path.name,
                "thread_name": thread.get("name") if isinstance(thread.get("name"), str) else None,
                "project_id": thread.get("projectId") if isinstance(thread.get("projectId"), str) else None,
                "last_used": thread.get("recencyAt") or thread.get("updatedAt") or thread.get("createdAt"),
            })
        return found
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceDiscoveryError("Codex app-server could not be started for workspace discovery") from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
