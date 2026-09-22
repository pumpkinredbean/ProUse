"""Read-only checks; warnings do not pretend to verify a remote MCP client."""
from . import __version__
from .configuration import Configuration, codex_path
from .errors import CLIError
from .runtime.admin import AdminRuntime


def inspect(config: Configuration, runtime: AdminRuntime) -> dict:
    checks = [{"name": "local_installation", "status": "ok", "detail": f"ProUse {__version__}"}]
    try:
        registry = config.registry()
        config.settings()
        checks.append({"name": "configuration", "status": "ok", "workspaces": len(registry.workspaces)})
    except (CLIError, OSError, ValueError) as exc:
        checks.append({"name": "configuration", "status": "error", "detail": str(exc)})
    executable = codex_path()
    checks.append({"name": "codex", "status": "ok" if executable else "warning",
                   "detail": executable or "Not found; context and Admin remain available, worker execution does not"})
    try:
        status = runtime.status()
        checks.append({"name": "admin_service", "status": "ok" if status["admin_ready"] else "warning",
                       "detail": status["status"]})
    except (CLIError, OSError, ValueError) as exc:
        checks.append({"name": "admin_service", "status": "error", "detail": str(exc)})
    checks.extend([
        {"name": "mcp_handshake", "status": "not_tested", "detail": "Run prouse mcp check"},
        {"name": "client_connection", "status": "not_verified",
         "detail": "Client authorization must be confirmed in that client"},
    ])
    return {"status": "error" if any(item["status"] == "error" for item in checks) else "ok",
            "checks": checks, "home": str(config.paths.home)}
