"""Process identity and ownership receipts; never select a process by name."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
import subprocess
import time

from ..configuration import AdminSettings
from ..errors import CLIError
from ..state import InstancePaths, atomic_json, file_lock, read_json


def fingerprint(pid: int) -> str | None:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=3, check=False)
        value = " ".join(result.stdout.split())
        return hashlib.sha256(value.encode()).hexdigest() if result.returncode == 0 and value else None
    except (OSError, subprocess.SubprocessError):
        return None


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    fingerprint: str
    home: str
    host: str
    port: int
    started_at: float

    @property
    def settings(self) -> AdminSettings:
        return AdminSettings(self.host, self.port)

    def is_current(self) -> bool:
        return alive(self.pid) and fingerprint(self.pid) == self.fingerprint

    @classmethod
    def capture(cls, paths: InstancePaths, settings: AdminSettings) -> ProcessRecord:
        identity = fingerprint(os.getpid())
        if not identity:
            raise CLIError("Cannot establish process ownership; inspect the host's ps command")
        return cls(os.getpid(), identity, str(paths.home), settings.host, settings.port, time.time())


class InstanceFile:
    def __init__(self, paths: InstancePaths):
        self.paths = paths
        self.lock = paths.run / "admin.lock"

    def current(self) -> tuple[ProcessRecord | None, str | None]:
        try:
            document = read_json(self.paths.instance)
        except CLIError:
            return None, "invalid instance state"
        if document is None:
            return None, None
        try:
            record = ProcessRecord(**document)
            if (type(record.pid) is int and 0 < record.pid < 2**31
                    and isinstance(record.fingerprint, str) and record.fingerprint
                    and record.home == str(self.paths.home) and record.is_current()):
                record.settings  # Validate the recorded listener before health requests.
                return record, None
        except (TypeError, ValueError, CLIError):
            pass
        # A new process cannot publish a receipt while this lock is held. Do not
        # erase its state based on an earlier observation from another process.
        try:
            with file_lock(self.lock, blocking=False):
                if read_json(self.paths.instance) == document:
                    self.paths.instance.unlink(missing_ok=True)
        except (BlockingIOError, CLIError):
            pass
        return None, "stale instance state"

    def write(self, record: ProcessRecord) -> None:
        atomic_json(self.paths.instance, asdict(record))

    def remove(self, record: ProcessRecord) -> None:
        if read_json(self.paths.instance) == asdict(record):
            self.paths.instance.unlink(missing_ok=True)
