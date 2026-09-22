"""Workspace setup command."""
from pathlib import Path
import sys

from ..command import CommandContext, json_flag, listener_flags
from ..errors import CLIError, ExitCode


def register(commands) -> None:
    parser = commands.add_parser("setup", help="register a workspace and preserve existing settings")
    parser.add_argument("--workspace", help="existing project directory")
    parser.add_argument("--workspace-id")
    parser.add_argument("--label")
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
    result = context.config.setup(workspace, workspace_id=args.workspace_id, label=args.label,
                                  host=args.host, port=args.port)
    if context.output.json_mode:
        context.output.result(result)
    else:
        context.output.line(f"Workspace ready: {result['workspace_id']} ({result['workspace']})")
        context.output.line(f"Configuration: {result['registry']}")
        context.output.line("Next: prouse start")
    return ExitCode.OK
