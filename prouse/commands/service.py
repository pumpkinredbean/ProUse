"""Explicit login-autostart command group."""
from ..command import CommandContext, json_flag
from ..integrations.services import manage


def register(commands) -> None:
    parser = commands.add_parser("service", help="manage optional login autostart")
    json_flag(parser)
    subcommands = parser.add_subparsers(dest="service_action", required=True)
    for name, description in (("install", "enable a user login service"),
                              ("status", "inspect the user service"),
                              ("uninstall", "disable and remove the user service")):
        command = subcommands.add_parser(name, help=description)
        command.add_argument("--manager", choices=("launchd", "systemd"))
        command.add_argument("--dry-run", action="store_true", help="show actions without applying them")
        json_flag(command)
        command.set_defaults(handler=execute)


def execute(context: CommandContext, args) -> int:
    result, code = manage(context.paths, args.service_action, manager=args.manager, dry_run=args.dry_run)
    context.output.result(result)
    return code
