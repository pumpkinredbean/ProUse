"""List, add and remove registered workspaces."""
from ..command import CommandContext, json_flag
from ..errors import ExitCode


def register(commands) -> None:
    parser = commands.add_parser("workspace", help="list, add, or remove registered workspaces")
    json_flag(parser)
    actions = parser.add_subparsers(dest="workspace_action", required=True)
    listing = actions.add_parser("list", help="show registered workspaces")
    json_flag(listing)
    listing.set_defaults(handler=show)
    add = actions.add_parser("add", help="register a project folder")
    add.add_argument("path")
    add.add_argument("--id", dest="workspace_id")
    add.add_argument("--label")
    add.add_argument("--default", action="store_true", help="make it the default workspace")
    json_flag(add)
    add.set_defaults(handler=add_workspace)
    remove = actions.add_parser("remove", help="unregister a workspace (files are not touched)")
    remove.add_argument("workspace_id")
    json_flag(remove)
    remove.set_defaults(handler=remove_workspace)
    default = actions.add_parser("default", help="choose the workspace relative paths resolve against")
    default.add_argument("workspace_id")
    json_flag(default)
    default.set_defaults(handler=set_default)


def show(context: CommandContext, args) -> int:
    result = context.config.listing()
    if context.output.json_mode:
        context.output.result(result)
        return ExitCode.OK
    if not result["workspaces"]:
        context.output.line("No workspaces yet. Run `prouse workspace add /path/to/project`.")
    for item in result["workspaces"]:
        notes = ["default"] if item["default"] else []
        if item["status"] == "disabled":
            notes.append("disabled")
        elif item["status"] == "unavailable":
            notes.append(f"unavailable: {item['reason']}")
        marker = f" ({', '.join(notes)})" if notes else ""
        context.output.line(f"{item['id']}{marker}: {item['root']}")
    return ExitCode.OK


def add_workspace(context: CommandContext, args) -> int:
    result = context.config.add(args.path, workspace_id=args.workspace_id, label=args.label,
                                make_default=args.default)
    if context.output.json_mode:
        context.output.result(result)
    else:
        marker = " (default)" if result["default"] else ""
        context.output.line(f"Workspace ready: {result['workspace_id']}{marker} ({result['workspace']})")
    return ExitCode.OK


def remove_workspace(context: CommandContext, args) -> int:
    result = context.config.remove(args.workspace_id)
    if context.output.json_mode:
        context.output.result(result)
    else:
        context.output.line(f"Removed workspace {args.workspace_id}; its files were not touched.")
    return ExitCode.OK


def set_default(context: CommandContext, args) -> int:
    result = context.config.set_default(args.workspace_id)
    if context.output.json_mode:
        context.output.result(result)
    else:
        context.output.line(f"Default workspace: {args.workspace_id}")
    return ExitCode.OK
