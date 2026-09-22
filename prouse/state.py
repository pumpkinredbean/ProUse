"""Instance paths and small, private, atomic state-file operations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator, Any

from .errors import CLIError


@dataclass(frozen=True)
class InstancePaths:
    home: Path

    @classmethod
    def from_environment(cls) -> InstancePaths:
        value = os.environ.get("PROUSE_HOME", str(Path.home() / ".prouse"))
        return cls(Path(value).expanduser().resolve())

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def registry(self) -> Path:
        return self.config / "workspace-registry.json"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def policies(self) -> Path:
        return self.config / "policies"

    @property
    def state(self) -> Path:
        return self.home / ".state" / "orchestrator"

    @property
    def run(self) -> Path:
        return self.home / "run"

    @property
    def instance(self) -> Path:
        return self.run / "instance.json"

    @property
    def log(self) -> Path:
        return self.home / "logs" / "admin.log"

    def prepare_runtime(self) -> None:
        for path in (self.run, self.log.parent):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise CLIError(f"Cannot read valid JSON from {path}: {exc}") from None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle, flags)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
