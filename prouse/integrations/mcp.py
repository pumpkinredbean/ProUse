"""Client configuration, real handshake probes, and stdio serving."""
import asyncio
import os
from pathlib import Path
import sys

from ..configuration import Configuration
from ..errors import CLIError, error_detail
from ..state import InstancePaths

EXPECTED_TOOLS = {"workspaces", "read", "write", "edit", "bash", "bash_job", "grep", "find", "ls"}


def command(paths: InstancePaths) -> dict:
    # Use this installation, even if PATH contains another ProUse version.
    executable = Path(sys.executable).parent / "prouse"
    spec = ({"command": str(executable), "args": ["mcp", "serve"]} if executable.is_file()
            else {"command": sys.executable, "args": ["-m", "prouse.cli", "mcp", "serve"]})
    return {**spec, "env": {"PROUSE_HOME": str(paths.home)}}


async def probe(paths: InstancePaths) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    spec = command(paths)
    params = StdioServerParameters(command=spec["command"], args=spec["args"],
                                   env=dict(os.environ, **spec["env"]))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            if not EXPECTED_TOOLS <= names:
                raise CLIError("MCP initialized but tools are missing: " + ", ".join(sorted(EXPECTED_TOOLS - names)))
            listing = await session.call_tool("workspaces", {})
            if listing.isError:
                raise CLIError("MCP initialized but workspaces failed: " + _text(listing))
            response = await session.call_tool("ls", {"limit": 1})
            if response.isError:
                raise CLIError("MCP initialized, but listing the default workspace failed: " + _text(response))
            return {"status": "ok", "handshake": "verified", "server": initialized.serverInfo.name,
                    "tools": len(listed.tools), "workspace_read": "verified", "client_connection": "not_verified"}


def _text(result) -> str:
    return " ".join(getattr(item, "text", "") for item in result.content).strip()


def check(paths: InstancePaths, *, timeout: float = 20) -> dict:
    Configuration(paths).workspaces()

    async def bounded_probe():
        async with asyncio.timeout(timeout):
            return await probe(paths)

    try:
        return asyncio.run(bounded_probe())
    except TimeoutError:
        raise CLIError(f"MCP check timed out after {timeout:g}s") from None
    except CLIError:
        raise
    except Exception as exc:
        raise CLIError(f"MCP check failed: {error_detail(exc)}. Run `prouse doctor`.") from None


def serve(paths: InstancePaths) -> None:
    from ..server import serve as run_server

    run_server(paths.registry, paths.output)
