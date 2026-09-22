"""CLI-only context, argument validation, and human/JSON output."""
import argparse
import json
import math
import sys
from typing import TextIO

from .configuration import Configuration
from .errors import CLIError, ExitCode
from .integrations import mcp
from .runtime.admin import AdminRuntime
from .state import InstancePaths


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIError(f"{message}. Run `prouse --help`.", ExitCode.USAGE)


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number of seconds") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite, positive number of seconds")
    return number


def line_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print a machine-readable result")


def listener_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="override the configured listen address")
    parser.add_argument("--port", type=int, help="override the configured listen port")


class Output:
    def __init__(self, *, json_mode: bool = False, stream: TextIO | None = None,
                 errors: TextIO | None = None):
        self.json_mode = json_mode
        self.stream = stream if stream is not None else sys.stdout
        self.errors = errors if errors is not None else sys.stderr

    def line(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def result(self, value: dict, *, force_json: bool = False) -> None:
        if self.json_mode or force_json:
            self.line(json.dumps(value, ensure_ascii=False, sort_keys=True))
        else:
            for key, item in value.items():
                self.line(f"{key}: {item}")

    def runtime(self, value: dict) -> None:
        if self.json_mode:
            self.result(value)
            return
        self.line("ProUse: " + value["status"].replace("_", " "))
        if value.get("running") or value.get("mode") == "foreground":
            for url in value.get("admin_urls", []):
                self.line(f"Dashboard: {url}")
            if value.get("mode") == "foreground":
                self.line("Press Ctrl-C to stop.")
            self.line("Logs: prouse logs -f    Stop: prouse stop")
            self.line("MCP: prouse mcp check    Client config: prouse mcp config")
        elif value["status"] in ("stopped", "already_stopped"):
            self.line("Next: prouse start")

    def error(self, message: str, code: int) -> None:
        if self.json_mode:
            self.result({"status": "error", "error": message, "exit_code": int(code)})
        else:
            print(f"prouse: {message}", file=self.errors)


class CommandContext:
    def __init__(self, paths: InstancePaths, output: Output):
        self.paths = paths
        self.output = output
        self.config = Configuration(paths)
        self.runtime = AdminRuntime(self.config)

    def status(self) -> dict:
        return self.with_connections(self.runtime.status())

    def with_connections(self, result: dict) -> dict:
        return {**result,
                "mcp": {"transport": "stdio", "command": mcp.command(self.paths),
                        "handshake": "client_owned_not_tested"},
                "client_connection": "not_verified"}
