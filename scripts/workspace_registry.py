"""Admin-owned configuration and deterministic execution-context resolution.

No model calls, fuzzy matching, environment expansion, or remote configuration writes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

import jsonschema

from context_store import ContextStore

ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
ID = {"type": "string", "pattern": ID_PATTERN}
PROFILE = {"type": "object", "additionalProperties": False,
           "required": ["id", "model", "reasoning_effort"], "properties": {
               "id": ID, "label": {"type": "string", "minLength": 1, "maxLength": 120},
               "model": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$"},
               "reasoning_effort": {"enum": EFFORTS}}}
WORKSPACE = {"type": "object", "additionalProperties": False,
             "required": ["id", "label", "root", "enabled"], "properties": {
                 "id": ID, "label": {"type": "string", "minLength": 1, "maxLength": 120},
                 "root": {"type": "string", "minLength": 1}, "enabled": {"type": "boolean"},
                 "default_worker_profile": ID,
                 "context_policy": {"type": "string", "minLength": 1}}}
REGISTRY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Approved workspace and worker profile registry", "type": "object",
    "additionalProperties": False,
    "required": ["version", "workspaces", "worker_profiles", "model_capabilities", "default_worker_profile"],
    "properties": {
        "version": {"const": 1}, "server_name": {"type": "string", "minLength": 1},
        "default_workspace_id": ID, "default_worker_profile": ID,
        "state_dir": {"type": "string", "minLength": 1},
        "codex_bin": {"type": "string", "minLength": 1},
        "workspaces": {"type": "array", "maxItems": 100, "items": WORKSPACE},
        "worker_profiles": {"type": "array", "minItems": 1, "maxItems": 100, "items": PROFILE},
        "model_capabilities": {"type": "object", "minProperties": 1,
            "additionalProperties": {"type": "array", "minItems": 1, "uniqueItems": True,
                                     "items": {"enum": EFFORTS}}},
    },
}


class ResolutionError(ValueError):
    def __init__(self, code: str, message: str, candidates=None):
        super().__init__(message)
        self.result = {"status": "error", "error": {"code": code, "message": message}}
        if candidates is not None:
            self.result["candidates"] = candidates


class Registry:
    def __init__(self, config: dict[str, Any], base: Path):
        try:
            jsonschema.Draft202012Validator(REGISTRY_SCHEMA).validate(config)
        except jsonschema.ValidationError:
            raise ResolutionError("invalid_configuration", "Registry does not match its schema") from None
        self.config = config
        self.base = base.resolve(strict=True)
        self.sha256 = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.profiles = {}
        for profile in config["worker_profiles"]:
            pid = profile["id"]
            if pid in self.profiles:
                raise ResolutionError("invalid_configuration", "Duplicate worker profile ID")
            if profile["reasoning_effort"] not in config["model_capabilities"].get(profile["model"], []):
                raise ResolutionError("invalid_configuration", "Unsupported model/effort in configured profile")
            self.profiles[pid] = dict(profile)
        if config["default_worker_profile"] not in self.profiles:
            raise ResolutionError("invalid_configuration", "Unknown global worker profile")
        self.state_dir = self._path(config.get("state_dir", ".state/orchestrator"))
        if ".state" not in self.state_dir.parts:
            raise ResolutionError("invalid_configuration", "Private state_dir must be inside a .state directory")
        self.workspaces = {}
        self.stores = {}
        self.root_identity = {}
        resolved = []
        for item in config["workspaces"]:
            wid = item["id"]
            if wid in self.workspaces:
                raise ResolutionError("invalid_configuration", "Duplicate workspace ID")
            root = self._path(item["root"], must_exist=True)
            if not root.is_dir() or root in (Path(root.anchor), Path.home().resolve()):
                raise ResolutionError("invalid_configuration", "Workspace must be an explicit project directory")
            if item.get("default_worker_profile", config["default_worker_profile"]) not in self.profiles:
                raise ResolutionError("invalid_configuration", "Unknown workspace default profile")
            self.workspaces[wid] = dict(item, root=root)
            resolved.append((wid, root))
            stat = root.stat()
            self.root_identity[wid] = (stat.st_dev, stat.st_ino)
            if item.get("context_policy"):
                try:
                    policy = json.loads(self._path(item["context_policy"], True).read_text())
                    self.stores[wid] = ContextStore(root, policy, workspace_id=wid)
                except (OSError, ValueError, KeyError, TypeError):
                    raise ResolutionError("invalid_configuration", "Invalid workspace context policy") from None
        # Aliased and nested registrations are valid. Writers whose canonical roots overlap
        # share one lock, while unrelated workspace roots can still run concurrently.
        writer_locks = self.state_dir / "writer-locks"
        for wid, root in resolved:
            overlapping = [other for _, other in resolved
                           if root.is_relative_to(other) or other.is_relative_to(root)]
            anchor = min(overlapping, key=lambda value: (len(value.parts), str(value)))
            key = hashlib.sha256(str(anchor).encode()).hexdigest()[:32]
            self.workspaces[wid]["writer_lock"] = writer_locks / (key + ".lock")
        default = config.get("default_workspace_id")
        if default is not None and (default not in self.workspaces or not self.workspaces[default]["enabled"]):
            raise ResolutionError("invalid_configuration", "Default workspace must be registered and enabled")
        self.codex_bin = self._path(config["codex_bin"], True) if config.get("codex_bin") else None

    def _path(self, value: str, must_exist=False) -> Path:
        try:
            p = Path(value)
            if "$" in value or "~" in value:
                raise ValueError()
            return (p if p.is_absolute() else self.base / p).resolve(strict=must_exist)
        except (OSError, ValueError, RuntimeError):
            raise ResolutionError("invalid_configuration", "Configured local path is unavailable or invalid") from None

    @classmethod
    def load(cls, path: Path):
        try:
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("duplicate JSON key")
                    obj[key] = value
                return obj
            config = json.loads(path.read_text(), object_pairs_hook=unique)
        except (OSError, ValueError):
            raise ResolutionError("invalid_configuration", "Cannot read a valid registry JSON") from None
        return cls(config, path.resolve().parent)

    def list_workspaces(self):
        return [self.inspect(wid) for wid in sorted(self.workspaces)]

    def inspect(self, wid):
        if wid not in self.workspaces:
            raise ResolutionError("unknown_workspace", "Unknown workspace ID")
        item = self.workspaces[wid]
        return {"workspace_id": wid, "label": item["label"], "enabled": item["enabled"],
                "default_worker_profile": item.get("default_worker_profile", self.config["default_worker_profile"]),
                "context_available": wid in self.stores,
                "policy_sha256": self.stores[wid].policy_hash if wid in self.stores else None}

    def workspace(self, wid=None):
        if wid is None:
            wid = self.config.get("default_workspace_id")
        if wid is None:
            eligible = [w for w in self.list_workspaces() if w["enabled"]]
            if len(eligible) != 1:
                raise ResolutionError("ambiguous_workspace" if eligible else "workspace_required",
                                      "Select an approved workspace ID; ask the user in normal text if needed",
                                      [{"workspace_id": w["workspace_id"], "label": w["label"]} for w in eligible])
            wid = eligible[0]["workspace_id"]
        if not isinstance(wid, str) or wid not in self.workspaces:
            raise ResolutionError("unknown_workspace", "Unknown workspace ID")
        item = self.workspaces[wid]
        if not item["enabled"]:
            raise ResolutionError("disabled_workspace", "Workspace is disabled")
        try:
            stat = item["root"].stat()
            if item["root"].resolve(strict=True) != item["root"] or (stat.st_dev, stat.st_ino) != self.root_identity[wid]:
                raise OSError()
        except OSError:
            raise ResolutionError("workspace_unavailable", "Approved workspace root has changed or is unavailable") from None
        return item

    def resolve(self, workspace_id=None, worker_profile_id=None):
        workspace = self.workspace(workspace_id)
        pid = worker_profile_id if worker_profile_id is not None else workspace.get(
            "default_worker_profile", self.config["default_worker_profile"])
        if not isinstance(pid, str) or pid not in self.profiles:
            raise ResolutionError("unknown_worker_profile", "Select an admin-configured worker profile")
        return workspace, self.profiles[pid]


    def context(self, workspace_id=None):
        workspace = self.workspace(workspace_id)
        if workspace["id"] not in self.stores:
            raise ResolutionError("context_unavailable", "No context policy configured for this workspace")
        return self.stores[workspace["id"]]
