"""The workspace registry: which project folders ProUse may work in.

The registry lives at `$PROUSE_HOME/config/workspace-registry.json`. Version 2 holds only
workspaces; a version 1 registry from earlier releases is read as is and rewritten as
version 2 (dropping worker profiles and context policies) the next time it changes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import re

from .errors import CLIError, ExitCode
from .state import InstancePaths, atomic_json, file_lock, read_json
from .workspaces import Workspaces, parse_registry

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class AdminSettings:
    """Where the optional workspace dashboard listens."""
    host: str = "127.0.0.1"
    port: int = 8848

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", self.host):
            raise CLIError("Host must be an IPv4 address or hostname", ExitCode.USAGE)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise CLIError("Port must be between 1 and 65535", ExitCode.USAGE)

    def override(self, host: str | None = None, port: int | None = None) -> AdminSettings:
        return replace(self, host=self.host if host is None else host,
                       port=self.port if port is None else port)

    @classmethod
    def from_dict(cls, value: dict) -> AdminSettings:
        return cls(host=value.get("host", "127.0.0.1"), port=value.get("port", 8848))


def validate_workspace(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CLIError(f"Workspace is not accessible: {value}: {exc}", ExitCode.USAGE) from None
    if not path.is_dir() or path in (Path(path.anchor), Path.home().resolve()):
        raise CLIError("Workspace must be an existing project directory, not your home folder or the "
                       "filesystem root", ExitCode.USAGE)
    return path


def workspace_slug(path: Path) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")
    if not value or not value[0].isalpha():
        value = "workspace-" + value
    return value[:48].rstrip("-") or "workspace"


class Configuration:
    def __init__(self, paths: InstancePaths):
        self.paths = paths

    def _document(self) -> dict | None:
        document = read_json(self.paths.registry)
        if document is None:
            return None
        try:
            parse_registry(document, self.paths.config)
        except ValueError as exc:
            raise CLIError(f"Invalid workspace registry {self.paths.registry}: {exc}") from None
        return document

    def _entries(self, document: dict | None) -> list[dict]:
        if document is None:
            return []
        return [{"id": item["id"], "label": item.get("label", item["id"]), "root": item["root"],
                 "enabled": item.get("enabled", True)} for item in document.get("workspaces", [])]

    def _save(self, entries: list[dict], default: str | None) -> None:
        ids = [item["id"] for item in entries if item["enabled"]]
        if default not in ids:
            default = ids[0] if ids else None
        document: dict = {"version": 2, "default_workspace_id": default, "workspaces": entries}
        if not default:
            del document["default_workspace_id"]
        atomic_json(self.paths.registry, document)

    def settings_document(self) -> dict:
        document = read_json(self.paths.settings, {})
        if not isinstance(document, dict):
            raise CLIError(f"Settings must be an object: {self.paths.settings}")
        return document

    def settings(self) -> AdminSettings:
        return AdminSettings.from_dict(self.settings_document())

    def save_settings(self, *, host: str | None = None, port: int | None = None) -> AdminSettings:
        with file_lock(self.paths.config / "setup.lock"):
            document = self.settings_document()
            settings = AdminSettings.from_dict(document).override(host, port)
            atomic_json(self.paths.settings, {**document, **asdict(settings)})
        return settings

    def check(self) -> None:
        """Fail early on a broken registry or settings file; a missing registry is fine."""
        self._document()
        self.settings()

    def workspaces(self) -> Workspaces:
        if not self.paths.registry.is_file():
            raise CLIError("No workspace is registered yet. Run `prouse setup --workspace /path/to/project`.",
                           ExitCode.USAGE)
        self._document()
        return Workspaces(self.paths.registry)

    def listing(self) -> dict:
        """Every registered workspace, with whether the tools can use it right now."""
        document = self._document()
        available, unavailable = parse_registry(document, self.paths.config) if document else ([], [])
        ready = {item.id: item for item in available}
        reasons = {item.id: item.reason for item in unavailable}
        default = next((item.id for item in available if item.default), None)
        workspaces = []
        for entry in self._entries(document):
            item = {**entry, "default": entry["id"] == default}
            if entry["id"] in ready:
                item.update(status="ready", root=str(ready[entry["id"]].root))
            elif entry["id"] in reasons:
                item.update(status="unavailable", reason=reasons[entry["id"]])
            else:
                item["status"] = "disabled"
            workspaces.append(item)
        return {"workspaces": workspaces, "default": default, "registry": str(self.paths.registry)}

    def add(self, workspace: str, *, workspace_id: str | None = None, label: str | None = None,
            make_default: bool = False) -> dict:
        project = validate_workspace(workspace)
        wanted = workspace_id or workspace_slug(project)
        if not ID_RE.fullmatch(wanted):
            raise CLIError("Workspace IDs must match [a-z][a-z0-9_-]{0,63}", ExitCode.USAGE)
        if label is not None and not 1 <= len(label) <= 120:
            raise CLIError("Labels must contain 1 to 120 characters", ExitCode.USAGE)
        with file_lock(self.paths.config / "registry.lock"):
            document = self._document()
            created = document is None
            entries = self._entries(document)
            default = (document or {}).get("default_workspace_id")
            existing = next((item for item in entries
                             if (self.paths.config / Path(item["root"]).expanduser()).resolve() == project), None)
            added = existing is None
            if added:
                identifier, counter = wanted, 2
                taken = {item["id"] for item in entries}
                while identifier in taken:
                    suffix = f"-{counter}"
                    identifier = wanted[:64 - len(suffix)].rstrip("-") + suffix
                    counter += 1
                existing = {"id": identifier, "label": label or project.name, "root": str(project), "enabled": True}
                entries.append(existing)
            else:
                existing["enabled"] = True
                if label is not None:
                    existing["label"] = label
            if make_default or not default:
                default = existing["id"]
            self._save(entries, default)
        return {"status": "configured", "created": created, "workspace_added": added,
                "workspace_id": existing["id"], "workspace": str(project), "default": default == existing["id"],
                "registry": str(self.paths.registry)}

    def remove(self, workspace_id: str) -> dict:
        with file_lock(self.paths.config / "registry.lock"):
            document = self._document()
            entries = self._entries(document)
            if not any(item["id"] == workspace_id for item in entries):
                raise CLIError(f"Unknown workspace: {workspace_id}", ExitCode.USAGE)
            entries = [item for item in entries if item["id"] != workspace_id]
            self._save(entries, (document or {}).get("default_workspace_id"))
        return {"status": "removed", "workspace_id": workspace_id, "registry": str(self.paths.registry)}

    def update(self, workspace_id: str, *, label: str | None = None, enabled: bool | None = None,
               make_default: bool = False) -> dict:
        if label is not None and not 1 <= len(label) <= 120:
            raise CLIError("Labels must contain 1 to 120 characters", ExitCode.USAGE)
        with file_lock(self.paths.config / "registry.lock"):
            document = self._document()
            entries = self._entries(document)
            entry = next((item for item in entries if item["id"] == workspace_id), None)
            if entry is None:
                raise CLIError(f"Unknown workspace: {workspace_id}", ExitCode.USAGE)
            if label is not None:
                entry["label"] = label
            if enabled is not None:
                entry["enabled"] = enabled
            default = (document or {}).get("default_workspace_id")
            if make_default:
                if not entry["enabled"]:
                    raise CLIError(f"Workspace {workspace_id} is disabled; enable it before making it the default",
                                   ExitCode.USAGE)
                default = workspace_id
            self._save(entries, default)
        return {"status": "updated", "workspace_id": workspace_id, "registry": str(self.paths.registry)}

    def set_default(self, workspace_id: str) -> dict:
        return {**self.update(workspace_id, make_default=True), "status": "default_set"}
