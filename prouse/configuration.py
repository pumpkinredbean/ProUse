"""Validated operator configuration. No CLI parsing, prompting, or printing."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import re
import shutil

from .errors import CLIError, ExitCode
from .state import InstancePaths, atomic_json, file_lock, read_json

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


@dataclass(frozen=True)
class AdminSettings:
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


def codex_path() -> str | None:
    return shutil.which("codex")


def validate_workspace(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CLIError(f"Workspace is not accessible: {value}: {exc}", ExitCode.USAGE) from None
    if not path.is_dir() or path in (Path(path.anchor), Path.home().resolve()):
        raise CLIError("Workspace must be an existing explicit project directory", ExitCode.USAGE)
    return path


def workspace_slug(path: Path) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")
    if not value or not value[0].isalpha():
        value = "workspace-" + value
    return value[:48].rstrip("-") or "workspace"


class Configuration:
    def __init__(self, paths: InstancePaths):
        self.paths = paths

    def settings_document(self) -> dict:
        document = read_json(self.paths.settings, {})
        if not isinstance(document, dict):
            raise CLIError(f"Settings must be an object: {self.paths.settings}")
        return document

    def settings(self) -> AdminSettings:
        return AdminSettings.from_dict(self.settings_document())

    def registry(self):
        from workspace_registry import Registry
        if not self.paths.registry.is_file():
            raise CLIError("ProUse is not configured. Run `prouse setup --workspace .`.", ExitCode.USAGE)
        return Registry.load(self.paths.registry)

    def default_registry(self) -> dict:
        result = {
            "version": 1, "server_name": "ProUse", "state_dir": str(self.paths.state),
            "default_worker_profile": "standard", "model_capabilities": {"gpt-5-codex": ["high"]},
            "worker_profiles": [{"id": "standard", "label": "Standard", "model": "gpt-5-codex",
                                 "reasoning_effort": "high", "access": "full_access"}],
            "workspaces": [],
        }
        if executable := codex_path():
            result["codex_bin"] = executable
        return result

    def setup(self, workspace: str, *, workspace_id: str | None = None, label: str | None = None,
              host: str | None = None, port: int | None = None) -> dict:
        from context_store import ALLOWED_EXTENSIONS
        from workspace_registry import Registry
        project = validate_workspace(workspace)
        wanted = workspace_id or workspace_slug(project)
        if not ID_RE.fullmatch(wanted):
            raise CLIError("--workspace-id must match [a-z][a-z0-9-]{0,47}", ExitCode.USAGE)
        if label is not None and not 1 <= len(label) <= 120:
            raise CLIError("--label must contain 1 to 120 characters", ExitCode.USAGE)
        with file_lock(self.paths.config / "setup.lock"):
            document = self.settings_document()
            if host is not None:
                document["host"] = host
            if port is not None:
                document["port"] = port
            settings = AdminSettings.from_dict(document)
            created = not self.paths.registry.exists()
            registry = self.default_registry() if created else self.registry().config
            existing = next((item for item in registry["workspaces"]
                             if (self.paths.config / item["root"]).resolve() == project), None)
            added = existing is None
            new_policy = None
            if added:
                identifier, counter = wanted, 2
                identifiers = {item["id"] for item in registry["workspaces"]}
                while identifier in identifiers:
                    suffix = f"-{counter}"
                    identifier = wanted[:48 - len(suffix)].rstrip("-") + suffix
                    counter += 1
                policy_path = self.paths.policies / f"{identifier}.json"
                if not policy_path.exists():
                    atomic_json(policy_path, {
                        "version": 1, "name": "ProUse workspace context",
                        "files": ["README", "README.md", "README.rst", "LICENSE", "CONTRIBUTING.md"],
                        "directories": [{"path": ".", "extensions": sorted(ALLOWED_EXTENSIONS)}],
                    })
                    new_policy = policy_path
                existing = {"id": identifier, "label": label or project.name, "root": str(project),
                            "enabled": True, "default_worker_profile": registry["default_worker_profile"],
                            "context_policy": str(policy_path)}
                registry["workspaces"].append(existing)
                if not registry.get("default_workspace_id"):
                    registry["default_workspace_id"] = identifier
            try:
                Registry(registry, self.paths.config)
            except (OSError, ValueError):
                if new_policy is not None:
                    new_policy.unlink(missing_ok=True)
                raise
            atomic_json(self.paths.registry, registry)
            atomic_json(self.paths.settings, {**document, **asdict(settings)})
        return {"status": "configured", "created": created, "workspace_added": added,
                "workspace_id": existing["id"], "workspace": str(project),
                "registry": str(self.paths.registry), "next_action": "Run `prouse start`."}
