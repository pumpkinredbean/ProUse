"""Per-instance launchd/systemd user-service definitions and operations."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from ..configuration import Configuration
from ..errors import CLIError, ExitCode
from ..state import InstancePaths


def manager_kind(requested: str | None = None) -> str:
    if requested:
        return requested
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise CLIError("User services require macOS launchd or Linux systemd --user")


@dataclass(frozen=True)
class ServiceDefinition:
    manager: str
    path: Path
    content: bytes
    install: list[list[str]]
    status: list[str]
    stop: list[list[str]]
    after_remove: list[list[str]]


def definition(paths: InstancePaths, manager: str) -> ServiceDefinition:
    identity = hashlib.sha256(str(paths.home).encode()).hexdigest()[:10]
    command = [sys.executable, "-m", "prouse.cli", "run"]
    if manager == "launchd":
        label = f"dev.prouse.admin.{identity}"
        target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        content = plistlib.dumps({
            "Label": label, "ProgramArguments": command, "RunAtLoad": True,
            "KeepAlive": False,
            "EnvironmentVariables": {"PROUSE_HOME": str(paths.home),
                                     "_PROUSE_LOG_REDIRECTED": str(paths.log)},
            "StandardOutPath": str(paths.log), "StandardErrorPath": str(paths.log),
        })
        domain = f"gui/{os.getuid()}"
        return ServiceDefinition(manager, target, content,
            [["launchctl", "bootstrap", domain, str(target)]],
            ["launchctl", "print", f"{domain}/{label}"],
            [["launchctl", "bootout", domain, str(target)]], [])
    if manager == "systemd":
        name = f"prouse-{identity}.service"
        target = Path.home() / ".config" / "systemd" / "user" / name

        def quote(value: str, *, exec_argument: bool = False) -> str:
            value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            value = value.replace("\n", "\\n").replace("\r", "\\r")
            if exec_argument:
                value = value.replace("$", "$$")
            return '"' + value + '"'

        content = "\n".join([
            "[Unit]", "Description=ProUse Admin", "After=network.target", "",
            "[Service]", "Type=simple", f"Environment={quote('PROUSE_HOME=' + str(paths.home))}",
            "ExecStart=" + " ".join(quote(value, exec_argument=True) for value in command),
            "Restart=on-failure", "RestartSec=2", "",
            "[Install]", "WantedBy=default.target", "",
        ]).encode()
        return ServiceDefinition(manager, target, content,
            [["systemctl", "--user", "daemon-reload"],
             ["systemctl", "--user", "enable", "--now", name]],
            ["systemctl", "--user", "status", name, "--no-pager"],
            [["systemctl", "--user", "disable", "--now", name]],
            [["systemctl", "--user", "daemon-reload"]])
    raise CLIError(f"Unknown service manager: {manager}", ExitCode.USAGE)


def run_actions(actions: list[list[str]], dry_run: bool) -> list[dict]:
    results = []
    for argv in actions:
        if dry_run:
            results.append({"argv": argv, "status": "not_run"})
            continue
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        results.append({"argv": argv, "exit_code": completed.returncode,
                        "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()})
        if completed.returncode:
            raise CLIError(f"Service command failed ({completed.returncode}): {' '.join(argv)}: {completed.stderr.strip()}")
    return results


def manage(paths: InstancePaths, action: str, *, manager: str | None = None,
           dry_run: bool = False) -> tuple[dict, int]:
    spec = definition(paths, manager_kind(manager))
    common = {"manager": spec.manager, "path": str(spec.path)}
    if action == "status":
        if dry_run:
            return {**common, "status": "not_run", "installed": spec.path.exists(), "argv": spec.status}, ExitCode.OK
        completed = subprocess.run(spec.status, capture_output=True, text=True, timeout=30, check=False)
        code = ExitCode.OK if completed.returncode == 0 else ExitCode.NOT_RUNNING
        return {**common, "status": "running" if code == 0 else "not_running",
                "installed": spec.path.exists(), "exit_code": completed.returncode,
                "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}, code
    if action == "install":
        if not dry_run:
            Configuration(paths).registry()
            paths.prepare_runtime()
            spec.path.parent.mkdir(parents=True, exist_ok=True)
            spec.path.write_bytes(spec.content)
        actions = run_actions(spec.install, dry_run)
        return {**common, "status": "not_run" if dry_run else "installed", "actions": actions}, ExitCode.OK
    if action == "uninstall":
        if not spec.path.exists() and not dry_run:
            return {**common, "status": "already_uninstalled", "actions": []}, ExitCode.OK
        actions = run_actions(spec.stop, dry_run)
        if not dry_run:
            spec.path.unlink(missing_ok=True)
        actions.extend(run_actions(spec.after_remove, dry_run))
        return {**common, "status": "not_run" if dry_run else "uninstalled", "actions": actions}, ExitCode.OK
    raise CLIError(f"Unknown service action: {action}", ExitCode.USAGE)
