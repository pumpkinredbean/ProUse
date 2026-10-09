"""bash: run a shell command in a workspace, the way pi does, within MCP client limits.

Claude Desktop cancels an MCP tool call after about 60 seconds, so a command that is still
running after YIELD_SECONDS keeps running in the background as a numbered job. bash then
returns the output so far, and bash_job waits for more output or stops the job. Output is
captured in a log file and the model sees at most the last 2000 lines or 50KB of it.

Commands run with the user's login-shell environment (PATH from ~/.zprofile, ~/.zshrc and
the like), because an MCP server launched by a desktop app inherits only a minimal one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import anyio

from . import Result, ToolError
from .truncate import DEFAULT_MAX_BYTES, format_size, truncate_tail

if TYPE_CHECKING:
    from ..workspaces import Workspaces

YIELD_SECONDS = float(os.environ.get("PROUSE_BASH_YIELD_SECONDS", "45"))
LOG_RETENTION_SECONDS = 3 * 24 * 3600
FINISHED_JOBS_KEPT = 50
_ENV_MARKER = "__PROUSE_SHELL_ENVIRONMENT__"
_COMMON_PATHS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin")

_environment: dict[str, str] | None = None
_environment_lock = threading.Lock()


def _with_common_paths(env: dict[str, str]) -> dict[str, str]:
    parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    for candidate in (os.path.expanduser(path) for path in _COMMON_PATHS):
        if candidate not in parts and os.path.isdir(candidate):
            parts.append(candidate)
    return {**env, "PATH": os.pathsep.join(parts or ["/usr/bin", "/bin"])}


def _snapshot() -> dict[str, str]:
    base = dict(os.environ)
    if os.environ.get("PROUSE_SHELL_SNAPSHOT", "1") == "0":
        return _with_common_paths(base)
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    if not os.access(shell, os.X_OK):
        return _with_common_paths(base)
    dump = f"{shlex.quote(sys.executable)} -c 'import json, os; print(json.dumps(dict(os.environ)))'"
    try:
        completed = subprocess.run([shell, "-l", "-i", "-c", f"echo {_ENV_MARKER}; {dump}"],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                   env=base, start_new_session=True)
        output = completed.stdout.decode("utf-8", "replace")
        captured = json.loads(output.split(_ENV_MARKER + "\n", 1)[1].strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return _with_common_paths(base)
    if not isinstance(captured, dict) or "PATH" not in captured:
        return _with_common_paths(base)
    for name in ("SHLVL", "_", "PWD", "OLDPWD"):
        captured.pop(name, None)
    return _with_common_paths({**base, **captured})


def shell_environment() -> dict[str, str]:
    """The user's login-shell environment, captured once per server process."""
    global _environment
    with _environment_lock:
        if _environment is None:
            _environment = _snapshot()
        return _environment


def _bash() -> str:
    if os.access("/bin/bash", os.X_OK):
        return "/bin/bash"
    return shutil.which("bash", path=shell_environment().get("PATH")) or "/bin/sh"


@dataclass
class Job:
    id: int
    command: str
    process: subprocess.Popen
    log: Path
    started: float
    timeout: float | None
    done: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    ended: float | None = None
    end_reason: str | None = None  # "timeout" or "killed"
    reported: int = 0  # log bytes already shown to the model
    shown_as_job: bool = False
    keep_log: bool = False

    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started


def _safe_end(path: Path, end: int) -> int:
    """Back off so a running command's output is never cut inside a UTF-8 character."""
    if end <= 0:
        return end
    with path.open("rb") as handle:
        handle.seek(max(0, end - 4))
        tail = handle.read(end - max(0, end - 4))
    for back in range(1, min(4, len(tail)) + 1):
        byte = tail[-back]
        if byte & 0xC0 == 0x80:
            continue
        need = 1 if byte < 0x80 else 2 if byte >> 5 == 0b110 else 3 if byte >> 4 == 0b1110 else 4
        return end - back if need > back else end
    return end


def _segment(path: Path, start: int, end: int) -> tuple[str, int]:
    """Decode log bytes [start, end), keeping a tail window. Returns (text, line count)."""
    window = 2 * DEFAULT_MAX_BYTES + 4096
    lines = 0
    with path.open("rb") as handle:
        handle.seek(start)
        skip = max(0, (end - start) - window)
        while skip > 0:
            chunk = handle.read(min(1 << 20, skip))
            if not chunk:
                break
            lines += chunk.count(b"\n")
            skip -= len(chunk)
        data = handle.read(max(0, end - handle.tell()))
    lines += data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return data.decode("utf-8", "replace"), lines


class Jobs:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self._jobs: dict[int, Job] = {}
        self._next = 1
        self._lock = threading.Lock()
        self._cleaned = False

    def _clean_old_logs(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        cutoff = time.time() - LOG_RETENTION_SECONDS
        for log in self.output_dir.glob("bash-*.log"):
            try:
                if log.stat().st_mtime < cutoff:
                    log.unlink()
            except OSError:
                pass

    def start(self, command: str, cwd: Path, timeout: float | None) -> Job:
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._clean_old_logs()
        with self._lock:
            number = self._next
            self._next += 1
        log = self.output_dir / f"bash-{os.getpid()}-{number}.log"
        env = {**shell_environment(), "GIT_EDITOR": "true"}
        with log.open("wb") as output:
            try:
                process = subprocess.Popen([_bash(), "-c", command], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                raise ToolError(f"Could not start bash: {exc}") from None
        job = Job(number, command, process, log, time.monotonic(), timeout)
        with self._lock:
            self._jobs[number] = job
        threading.Thread(target=self._wait_for_exit, args=(job,), daemon=True).start()
        if timeout is not None:
            timer = threading.Timer(timeout, self.stop, args=(job, "timeout"))
            timer.daemon = True
            timer.start()
        return job

    def _wait_for_exit(self, job: Job) -> None:
        code = job.process.wait()
        job.exit_code = 128 - code if code < 0 else code
        job.ended = time.monotonic()
        job.done.set()

    def stop(self, job: Job, reason: str) -> None:
        if job.done.is_set():
            return
        job.end_reason = reason
        for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(job.process.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            if job.done.wait(grace):
                return

    def get(self, number: int) -> Job:
        with self._lock:
            job = self._jobs.get(number)
            running = [item.id for item in self._jobs.values() if not item.done.is_set() and item.shown_as_job]
        if job is None:
            listed = ", ".join(str(item) for item in running) or "none"
            raise ToolError(f"No bash job {number}. Running jobs: {listed}.")
        return job

    def forget(self, job: Job) -> None:
        if not job.keep_log:
            job.log.unlink(missing_ok=True)
        with self._lock:
            self._jobs.pop(job.id, None)
            finished = [item for item in self._jobs.values() if item.done.is_set()]
            for item in finished[:-FINISHED_JOBS_KEPT]:
                self._jobs.pop(item.id, None)

    def shutdown(self) -> None:
        with self._lock:
            running = [job for job in self._jobs.values() if not job.done.is_set()]
        for job in running:
            self.stop(job, "killed")

    @staticmethod
    async def wait(job: Job, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not job.done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await anyio.sleep(min(0.01 if job.elapsed() < 0.2 else 0.05, remaining))

    def take_output(self, job: Job) -> str:
        """New output since the last report, tail-truncated, with a notice when cut."""
        finished = job.done.is_set()
        try:
            end = job.log.stat().st_size
        except FileNotFoundError:
            return ""
        if not finished:
            end = _safe_end(job.log, end)
        if end <= job.reported:
            return ""
        text, total_lines = _segment(job.log, job.reported, end)
        job.reported = end
        truncation = truncate_tail(text)
        if not truncation.truncated:
            return text
        job.keep_log = True
        if truncation.last_line_partial:
            note = (f"[Showing the last {format_size(truncation.output_bytes)} of a {total_lines}-line output "
                    f"whose last line is very long. Full output: {job.log}]")
        else:
            note = (f"[Showing the last {truncation.output_lines} of {total_lines} lines. "
                    f"Full output: {job.log}]")
        return f"{truncation.content}\n\n{note}"

    def report(self, job: Job) -> Result:
        output = self.take_output(job).rstrip("\n")
        if not job.done.is_set():
            job.shown_as_job = True
            status = (f"[Still running after {job.elapsed():.0f}s as job {job.id}. Call bash_job with job={job.id} "
                      f"to wait for more output, or with kill=true to stop it.]")
            return Result(f"{output}\n\n{status}" if output else status)
        is_error = job.end_reason == "timeout" or (job.end_reason is None and job.exit_code != 0)
        if job.end_reason == "timeout":
            status = f"Command timed out after {job.timeout:g} seconds"
        elif job.end_reason == "killed":
            status = "Command was stopped"
        elif job.exit_code != 0:
            status = f"Command exited with code {job.exit_code}"
        else:
            status = ""
        if job.shown_as_job:
            status = f"[Job {job.id} finished after {job.elapsed():.0f}s" + (f": {status}]" if status else
                                                                            " with exit code 0]")
        self.forget(job)
        if not output and not status:
            return Result("(no output)")
        return Result("\n\n".join(part for part in (output, status) if part), is_error=is_error)


async def bash(workspaces: Workspaces, jobs: Jobs, command: str, cwd: str | None = None,
               timeout: float | None = None) -> Result:
    if not command.strip():
        raise ToolError("command must not be empty")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ToolError("Invalid timeout: must be a positive number of seconds")
    directory = workspaces.resolve(cwd)
    if not directory.is_dir():
        raise ToolError(f"Working directory does not exist: {cwd}")
    job = await anyio.to_thread.run_sync(jobs.start, command, directory, timeout)
    await jobs.wait(job, YIELD_SECONDS)
    return jobs.report(job)


async def bash_job(jobs: Jobs, job: int, wait: float | None = None, kill: bool = False) -> Result:
    found = jobs.get(job)
    if kill:
        await anyio.to_thread.run_sync(jobs.stop, found, "killed")
        await jobs.wait(found, 5)
    else:
        seconds = YIELD_SECONDS if wait is None else min(max(0.0, wait), YIELD_SECONDS)
        await jobs.wait(found, seconds)
    return jobs.report(found)
