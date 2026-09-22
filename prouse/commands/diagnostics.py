"""Diagnostic and log command adapters."""
from .. import diagnostics
from ..command import CommandContext, json_flag, line_count
from ..errors import ExitCode
from ..runtime.logs import lines


def register(commands) -> None:
    doctor = commands.add_parser("doctor", help="diagnose installation and capabilities")
    json_flag(doctor)
    doctor.set_defaults(handler=inspect)
    logs = commands.add_parser("logs", help="show recent logs; optionally follow new output")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=line_count, default=100)
    logs.set_defaults(handler=show_logs)


def inspect(context: CommandContext, args) -> int:
    result = diagnostics.inspect(context.config, context.runtime)
    if context.output.json_mode:
        context.output.result(result)
    else:
        for item in result["checks"]:
            detail = item.get("detail", f"{item.get('workspaces', 0)} workspaces")
            context.output.line(f"{item['status']:12} {item['name']}: {detail}")
    return ExitCode.ERROR if result["status"] == "error" else ExitCode.OK


def show_logs(context: CommandContext, args) -> int:
    try:
        for line in lines(context.paths.log, args.lines, args.follow):
            context.output.stream.write(line)
            context.output.stream.flush()
    except KeyboardInterrupt:
        pass
    return ExitCode.OK
