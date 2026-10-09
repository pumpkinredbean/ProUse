"""Read-only checks. Warnings never claim an MCP client is connected."""
from __future__ import annotations

from . import __version__
from .configuration import Configuration
from .errors import CLIError
from .runtime.admin import AdminRuntime
from .tools.search import ripgrep
from .tools.shell import _bash


def inspect(config: Configuration, runtime: AdminRuntime) -> dict:
    checks = [{"name": "installation", "status": "ok", "detail": f"ProUse {__version__}"}]
    try:
        listing = config.listing()
        ready = [item for item in listing["workspaces"] if item["status"] == "ready"]
        if ready:
            checks.append({"name": "workspaces", "status": "ok",
                           "detail": f"{len(ready)} ready, default {listing['default'] or 'none'}"})
        else:
            checks.append({"name": "workspaces", "status": "error",
                           "detail": "none ready; run `prouse setup --workspace /path/to/project`"})
        for item in listing["workspaces"]:
            if item["status"] == "unavailable":
                checks.append({"name": "workspaces", "status": "warning",
                               "detail": f"{item['id']} is unavailable ({item['reason']}): {item['root']}"})
    except (CLIError, OSError, ValueError) as exc:
        checks.append({"name": "workspaces", "status": "error", "detail": str(exc)})
    try:
        config.settings()
    except (CLIError, OSError, ValueError) as exc:
        checks.append({"name": "settings", "status": "error", "detail": str(exc)})
    rg = ripgrep()
    checks.append({"name": "ripgrep", "status": "ok" if rg else "warning",
                   "detail": rg or "not found; grep and find use a slower built-in search"})
    checks.append({"name": "bash", "status": "ok", "detail": _bash()})
    try:
        status = runtime.status()
        checks.append({"name": "dashboard", "status": "ok" if status["admin_ready"] else "info",
                       "detail": status["admin_urls"][0] if status["admin_ready"]
                       else "not running (optional); start it with `prouse start`"})
    except (CLIError, OSError, ValueError) as exc:
        checks.append({"name": "dashboard", "status": "error", "detail": str(exc)})
    checks.append({"name": "mcp_handshake", "status": "info", "detail": "run `prouse mcp check` to test it"})
    return {"status": "error" if any(item["status"] == "error" for item in checks) else "ok",
            "checks": checks, "home": str(config.paths.home)}
