"""First-run setup: register a project folder."""
from pathlib import Path
import sys

from ..command import CommandContext, json_flag, listener_flags
from ..errors import CLIError, ExitCode


def register(commands) -> None:
    parser = commands.add_parser("setup", help="register a project folder; keeps existing workspaces")
    parser.add_argument("--workspace", help="existing project directory")
    parser.add_argument("--workspace-id")
    parser.add_argument("--label")
    parser.add_argument("--default", action="store_true", help="make it the default workspace")
    parser.add_argument("--no-input", action="store_true", help="never prompt for input")
    listener_flags(parser)
    json_flag(parser)
    parser.set_defaults(handler=execute)


def execute(context: CommandContext, args) -> int:
    workspace = args.workspace
    if not workspace and not args.no_input and not context.output.json_mode and sys.stdin.isatty():
        workspace = input(f"Workspace directory [{Path.cwd()}]: ").strip() or str(Path.cwd())
    if not workspace:
        raise CLIError("setup requires --workspace in non-interactive mode", ExitCode.USAGE)
    context.config.settings().override(args.host, args.port)  # Validate before writing anything.
    result = context.config.add(workspace, workspace_id=args.workspace_id, label=args.label,
                                make_default=args.default)
    if args.host is not None or args.port is not None:
        context.config.save_settings(host=args.host, port=args.port)
    if context.output.json_mode:
        context.output.result(result)
    else:
        context.output.line(f"Workspace ready: {result['workspace_id']} ({result['workspace']})")
        context.output.line(f"Configuration: {result['registry']}")
        context.output.line("Next: add the output of `prouse mcp config` to your MCP client, then restart it.")
    return ExitCode.OK
