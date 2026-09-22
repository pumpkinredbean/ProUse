"""Top-level commands for the owned Admin process."""
import argparse

from ..command import CommandContext, json_flag, listener_flags, positive_seconds
from ..errors import ExitCode


def register(commands) -> None:
    for name, help_text, handler in (
        ("start", "start in the background and wait for readiness", start),
        ("run", "run in the foreground until interrupted", run),
        ("restart", "restart in the background and wait for readiness", restart),
        ("stop", "stop the process owned by this instance", stop),
        ("status", "show instance readiness and connection details", status),
    ):
        parser = commands.add_parser(name, help=help_text)
        json_flag(parser)
        if name in ("start", "run", "restart"):
            listener_flags(parser)
        if name in ("start", "restart"):
            parser.add_argument("--start-timeout", type=positive_seconds, default=15, metavar="SECONDS")
            parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
        if name in ("stop", "restart"):
            parser.add_argument("--timeout", type=positive_seconds, default=10, metavar="SECONDS")
        parser.set_defaults(handler=handler)


def start(context: CommandContext, args) -> int:
    result = context.runtime.start(host=args.host, port=args.port, timeout=args.start_timeout)
    context.output.runtime(context.with_connections(result))
    return ExitCode.OK


def run(context: CommandContext, args) -> int:
    context.runtime.run(host=args.host, port=args.port, on_ready=context.output.runtime)
    return ExitCode.OK


def restart(context: CommandContext, args) -> int:
    result = context.runtime.restart(host=args.host, port=args.port, timeout=args.timeout,
                                     start_timeout=args.start_timeout)
    context.output.runtime(context.with_connections(result))
    return ExitCode.OK


def stop(context: CommandContext, args) -> int:
    context.output.runtime(context.runtime.stop(timeout=args.timeout))
    return ExitCode.OK


def status(context: CommandContext, args) -> int:
    result = context.status()
    context.output.runtime(result)
    return ExitCode.OK if result["admin_ready"] else ExitCode.NOT_RUNNING
