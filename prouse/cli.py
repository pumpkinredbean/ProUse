"""Installed entry point: compose commands, parse arguments, and report failures."""
import subprocess
import sys

from . import __version__, commands
from .command import ArgumentParser, CommandContext, Output, json_flag
from .errors import CLIError, ExitCode
from .state import InstancePaths


def parser() -> ArgumentParser:
    root = ArgumentParser(prog="prouse", description="ProUse local workspace and MCP control",
        epilog="Get started: prouse setup --workspace .   Then: prouse start")
    root.add_argument("--version", action="version", version=f"ProUse {__version__}")
    json_flag(root)
    root.set_defaults(json=False)
    commands.register(root.add_subparsers(dest="command", metavar="COMMAND"))
    return root


def normalize_legacy_args(argv: list[str]) -> list[str]:
    """Keep existing installs working without advertising the old admin verb."""
    argv = list(argv)
    position = 1 if argv[:1] == ["--json"] else 0
    if argv[position:position + 1] == ["admin"]:
        argv[position] = "run" if "--foreground" in argv else "start"
        argv = [value for value in argv if value != "--foreground"]
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = normalize_legacy_args(sys.argv[1:] if argv is None else argv)
    output = Output(json_mode="--json" in argv)
    root = parser()
    try:
        args = root.parse_args(argv)
        output.json_mode = args.json
        if args.command is None:
            if args.json:
                raise CLIError("A command is required. Run `prouse --help`.", ExitCode.USAGE)
            root.print_help()
            return ExitCode.OK
        context = CommandContext(InstancePaths.from_environment(), output)
        return int(args.handler(context, args))
    except (CLIError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, CLIError) else ExitCode.ERROR
        output.error(str(exc), code)
        return int(code)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
