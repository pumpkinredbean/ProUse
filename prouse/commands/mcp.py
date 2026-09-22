"""Client-owned MCP command group."""
from pathlib import Path

from ..command import CommandContext, json_flag, positive_seconds
from ..errors import ExitCode
from ..integrations import mcp


def register(commands) -> None:
    parser = commands.add_parser("mcp", help="configure and verify client-owned stdio MCP")
    json_flag(parser)
    subcommands = parser.add_subparsers(dest="mcp_action", required=True)
    serve = subcommands.add_parser("serve", help="run the stdio server for an MCP client")
    serve.add_argument("--registry", type=Path)
    serve.add_argument("--check-config", action="store_true")
    serve.set_defaults(handler=serve_mcp)
    config = subcommands.add_parser("config", help="print MCP client JSON for this installation")
    json_flag(config)
    config.set_defaults(handler=client_config)
    check = subcommands.add_parser("check", help="verify initialization, discovery, and a workspace read")
    check.add_argument("--workspace-id")
    check.add_argument("--timeout", type=positive_seconds, default=20, metavar="SECONDS")
    json_flag(check)
    check.set_defaults(handler=check_mcp)


def client_config(context: CommandContext, args) -> int:
    context.output.result({"mcpServers": {"prouse": mcp.command(context.paths)}}, force_json=True)
    return ExitCode.OK


def check_mcp(context: CommandContext, args) -> int:
    context.output.result(mcp.check(context.paths, workspace_id=args.workspace_id, timeout=args.timeout))
    return ExitCode.OK


def serve_mcp(context: CommandContext, args) -> int:
    result = mcp.serve(context.paths, registry_path=args.registry, check_config=args.check_config)
    if result is not None:
        context.output.result(result, force_json=True)
    return ExitCode.OK
