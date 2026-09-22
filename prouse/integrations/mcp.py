"""Client configuration, real handshake probes, and stdio serving."""
import asyncio
import os
from pathlib import Path
import sys

from ..configuration import Configuration
from ..errors import CLIError, error_detail
from ..state import InstancePaths


def command(paths: InstancePaths) -> dict:
    # Use this installation, even if PATH contains another ProUse version.
    executable = Path(sys.executable).parent / "prouse"
    spec = ({"command": str(executable), "args": ["mcp", "serve"]} if executable.is_file()
            else {"command": sys.executable, "args": ["-m", "prouse.cli", "mcp", "serve"]})
    return {**spec, "env": {"PROUSE_HOME": str(paths.home)}}


async def probe(paths: InstancePaths, workspace_id: str | None) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    spec = command(paths)
    params = StdioServerParameters(command=spec["command"], args=spec["args"],
                                   env=dict(os.environ, **spec["env"]))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            workspaces = await session.call_tool("list_workspaces", {})
            if workspaces.isError or not workspaces.structuredContent:
                raise CLIError("MCP initialized but list_workspaces failed")
            eligible = [item for item in workspaces.structuredContent["workspaces"]
                        if item.get("enabled") and item.get("context_available")
                        and (workspace_id is None or item["workspace_id"] == workspace_id)]
            if not eligible:
                raise CLIError("MCP initialized, but no selected workspace has readable context")
            selected = eligible[0]["workspace_id"]
            response = await session.call_tool("list_directory", {"workspace_id": selected, "limit": 1})
            if response.isError:
                raise CLIError(f"MCP initialized, but the context read for {selected} failed")
            return {"status": "ok", "handshake": "verified", "server": initialized.serverInfo.name,
                    "tools": len(listed.tools), "workspace_id": selected,
                    "workspace_read": "verified", "client_connection": "not_verified"}


def check(paths: InstancePaths, *, workspace_id: str | None = None, timeout: float = 20) -> dict:
    Configuration(paths).registry()

    async def bounded_probe():
        async with asyncio.timeout(timeout):
            return await probe(paths, workspace_id)

    try:
        return asyncio.run(bounded_probe())
    except TimeoutError:
        raise CLIError(f"MCP check timed out after {timeout:g}s") from None
    except Exception as exc:
        raise CLIError(f"MCP check failed: {error_detail(exc)}. Run `prouse doctor --json`.") from None


def serve(paths: InstancePaths, *, registry_path: Path | None = None, check_config: bool = False) -> dict | None:
    from context_server import create_server
    from workspace_registry import Registry

    configured = Registry.load(registry_path) if registry_path else Configuration(paths).registry()
    if check_config:
        return {"status": "valid", "registry_sha256": configured.sha256,
                "workspaces": configured.list_workspaces()}
    create_server(configured, registry_path=registry_path or paths.registry).run(transport="stdio")
    return None
