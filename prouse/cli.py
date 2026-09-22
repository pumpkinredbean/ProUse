"""Installed ProUse command-line interface."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


class CLIError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def home() -> Path:
    value = os.environ.get("PROUSE_HOME")
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".prouse").resolve()


def paths() -> dict[str, Path]:
    root = home()
    return {
        "home": root,
        "registry": root / "config" / "workspace-registry.json",
        "settings": root / "config" / "settings.json",
        "policies": root / "config" / "policies",
        "state": root / ".state" / "orchestrator",
        "run": root / "run",
        "instance": root / "run" / "instance.json",
        "logs": root / "logs",
        "log": root / "logs" / "admin.log",
    }


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise CLIError(f"Cannot read valid JSON from {path}: {exc}") from None


def emit(value, json_output=False) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, str):
        print(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def slug(path: Path) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")
    if not value or not value[0].isalpha():
        value = "workspace-" + value
    return value[:48].rstrip("-") or "workspace"


def codex_path() -> str | None:
    return shutil.which("codex")


def default_registry() -> dict:
    p = paths()
    config = {
        "version": 1,
        "server_name": "ProUse",
        "state_dir": str(p["state"]),
        "default_worker_profile": "standard",
        "model_capabilities": {"gpt-5-codex": ["high"]},
        "worker_profiles": [{
            "id": "standard", "label": "Standard", "model": "gpt-5-codex",
            "reasoning_effort": "high", "access": "full_access",
        }],
        "workspaces": [],
    }
    found = codex_path()
    if found:
        config["codex_bin"] = found
    return config


def default_policy() -> dict:
    from context_store import ALLOWED_EXTENSIONS
    return {
        "version": 1,
        "name": "ProUse workspace context",
        "files": ["README", "README.md", "README.rst", "LICENSE", "CONTRIBUTING.md"],
        "directories": [{"path": ".", "extensions": sorted(ALLOWED_EXTENSIONS)}],
    }


def load_settings() -> dict:
    return read_json(paths()["settings"], {"host": "127.0.0.1", "port": 8848})


def validate_workspace(value: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CLIError(f"Workspace is not accessible: {value}: {exc}", EXIT_USAGE) from None
    if not path.is_dir() or path == Path(path.anchor) or path == Path.home().resolve():
        raise CLIError("Workspace must be an existing explicit project directory")
    return path


def setup_command(args) -> int:
    p = paths()
    workspace_arg = args.workspace
    if not workspace_arg and not args.no_input and sys.stdin.isatty():
        workspace_arg = input(f"Workspace directory [{Path.cwd()}]: ").strip() or str(Path.cwd())
    if not workspace_arg:
        raise CLIError("setup requires --workspace in non-interactive mode", EXIT_USAGE)
    workspace = validate_workspace(workspace_arg)
    if args.port is not None and not 1 <= args.port <= 65535:
        raise CLIError("--port must be between 1 and 65535", EXIT_USAGE)
    registry = read_json(p["registry"], None)
    created = registry is None
    if registry is None:
        registry = default_registry()
    if not isinstance(registry, dict) or not isinstance(registry.get("workspaces"), list):
        raise CLIError("Existing workspace registry is not a supported object")

    existing = None
    for item in registry["workspaces"]:
        try:
            if Path(item["root"]).resolve() == workspace:
                existing = item
                break
        except (KeyError, OSError, TypeError):
            continue
    added = existing is None
    if added:
        wanted = args.workspace_id or slug(workspace)
        if not ID_RE.fullmatch(wanted):
            raise CLIError("--workspace-id must match [a-z][a-z0-9-]{0,47}", EXIT_USAGE)
        ids = {item.get("id") for item in registry["workspaces"]}
        identifier = wanted
        counter = 2
        while identifier in ids:
            suffix = f"-{counter}"
            identifier = wanted[:48 - len(suffix)].rstrip("-") + suffix
            counter += 1
        policy_path = p["policies"] / f"{identifier}.json"
        if not policy_path.exists():
            atomic_json(policy_path, default_policy())
        registry["workspaces"].append({
            "id": identifier,
            "label": args.label or workspace.name,
            "root": str(workspace),
            "enabled": True,
            "default_worker_profile": registry["default_worker_profile"],
            "context_policy": str(policy_path),
        })
        if not registry.get("default_workspace_id"):
            registry["default_workspace_id"] = identifier
        existing = registry["workspaces"][-1]

    p["home"].mkdir(parents=True, exist_ok=True, mode=0o700)
    p["registry"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p["state"].mkdir(parents=True, exist_ok=True, mode=0o700)
    p["logs"].mkdir(parents=True, exist_ok=True, mode=0o700)
    p["run"].mkdir(parents=True, exist_ok=True, mode=0o700)
    # Validate in memory before replacing an existing user registry.
    from workspace_registry import Registry
    Registry(registry, p["registry"].parent)
    atomic_json(p["registry"], registry)
    settings = read_json(p["settings"], {})
    settings.setdefault("host", "127.0.0.1")
    settings.setdefault("port", 8848)
    if args.host is not None:
        settings["host"] = args.host
    if args.port is not None:
        settings["port"] = args.port
    atomic_json(p["settings"], settings)

    result = {
        "status": "configured",
        "created": created,
        "workspace_added": added,
        "workspace_id": existing["id"],
        "workspace": str(workspace),
        "registry": str(p["registry"]),
        "admin_url": admin_urls(settings)[0],
        "next_action": "Run `prouse admin` to start the dashboard.",
    }
    if args.json:
        emit(result, True)
    else:
        print(f"Workspace ready: {existing['id']} ({workspace})")
        print(f"Configuration: {p['registry']}")
        print("Next: prouse admin")
    return EXIT_OK


def process_fingerprint(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        value = " ".join(result.stdout.split())
        return hashlib.sha256(value.encode()).hexdigest() if result.returncode == 0 and value else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def instance_record(clean_stale=True) -> tuple[dict | None, str | None]:
    p = paths()
    record = read_json(p["instance"], None)
    if not isinstance(record, dict):
        return None, None
    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        reason = "invalid instance state"
    else:
        current = process_fingerprint(pid) if process_alive(pid) else None
        if current and current == record.get("fingerprint") and record.get("home") == str(p["home"]):
            return record, None
        reason = "stale instance state"
    if clean_stale:
        p["instance"].unlink(missing_ok=True)
    return None, reason


def health(settings: dict, timeout=0.7) -> bool:
    host = settings.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    try:
        with urllib.request.urlopen(f"http://{host}:{int(settings['port'])}/healthz", timeout=timeout) as response:
            return response.status == 200 and json.loads(response.read()).get("status") == "ready"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        value = sock.getsockname()[0]
        return value if not value.startswith("127.") else None
    except OSError:
        return None
    finally:
        sock.close()


def admin_urls(settings: dict) -> list[str]:
    host, port = settings.get("host", "127.0.0.1"), int(settings.get("port", 8848))
    if host == "0.0.0.0":
        values = [f"http://127.0.0.1:{port}/"]
        address = lan_ip()
        if address:
            values.append(f"http://{address}:{port}/")
        return values
    return [f"http://{host}:{port}/"]


class Tee:
    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, value):
        self.stream.write(value)
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self):
        self.stream.flush()
        self.log.flush()


def allowed_hosts(settings: dict) -> list[str]:
    port = int(settings["port"])
    values = [f"localhost:{port}", f"127.0.0.1:{port}"]
    address = lan_ip()
    if address:
        values.append(f"{address}:{port}")
    host = settings.get("host")
    if host not in (None, "0.0.0.0", "127.0.0.1", "localhost"):
        values.append(f"{host}:{port}")
    return sorted(set(values))


def emit_runtime(result: dict, json_output=False) -> None:
    if json_output:
        emit(result, True)
        return
    print("ProUse Admin: " + result["status"].replace("_", " "))
    for url in result.get("admin_urls", []):
        print(f"Dashboard: {url}")
    if result.get("admin_ready") or result.get("healthy"):
        print("Logs: prouse logs -f    Stop: prouse stop")
        print("MCP: prouse mcp check    Client config: prouse mcp config")
    else:
        print("Next: prouse admin")


def start_background(settings: dict, args) -> int:
    p = paths()
    p["logs"].mkdir(parents=True, exist_ok=True, mode=0o700)
    # The child owns its session and redirects every standard stream, so it survives
    # the installing agent's terminal without requiring a login service.
    with p["log"].open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "prouse.cli", "start", "--host", settings["host"],
             "--port", str(settings["port"])],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=p["home"],
            env=dict(os.environ, PROUSE_HOME=str(p["home"]), PYTHONUNBUFFERED="1",
                     _PROUSE_LOG_REDIRECTED=str(p["log"])),
        )
    deadline = time.monotonic() + args.start_timeout
    while time.monotonic() < deadline:
        result, code = status_value()
        if code == EXIT_OK:
            emit_runtime(result, args.json)
            return EXIT_OK
        if process.poll() is not None:
            raise CLIError(f"Admin could not start (exit {process.returncode}). Run `prouse logs` or inspect {p['log']}")
        time.sleep(0.1)
    # Only terminate the exact child we spawned, never an unrelated listener.
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise CLIError(f"Admin did not become ready within {args.start_timeout:g}s. Run `prouse logs`.")


def start_command(args) -> int:
    p = paths()
    if not p["registry"].is_file():
        raise CLIError("ProUse is not configured. Run `prouse setup --workspace /path/to/project`.", EXIT_USAGE)
    record, stale = instance_record()
    settings = load_settings()
    if record:
        settings.update(host=record["host"], port=record["port"])
        result = {"status": "already_running", "pid": record["pid"], "healthy": health(settings),
                  "admin_urls": admin_urls(settings)}
        emit_runtime(result, getattr(args, "json", False))
        return EXIT_OK if result["healthy"] else EXIT_ERROR
    if args.host is not None:
        settings["host"] = args.host
    if args.port is not None:
        settings["port"] = args.port
    if not 1 <= int(settings["port"]) <= 65535:
        raise CLIError("--port must be between 1 and 65535", EXIT_USAGE)
    if getattr(args, "background", False):
        return start_background(settings, args)

    p["run"].mkdir(parents=True, exist_ok=True, mode=0o700)
    with (p["run"] / "admin.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CLIError("Another Admin is starting for this PROUSE_HOME. Run `prouse status`.") from None
        return run_admin(settings)


def run_admin(settings: dict) -> int:
    p = paths()

    from admin_server import Admin, Server
    p["logs"].mkdir(parents=True, exist_ok=True, mode=0o700)
    p["run"].mkdir(parents=True, exist_ok=True, mode=0o700)
    log = None
    old_out, old_err = sys.stdout, sys.stderr
    if os.environ.get("_PROUSE_LOG_REDIRECTED") != str(p["log"]):
        log = p["log"].open("a", encoding="utf-8")
        sys.stdout, sys.stderr = Tee(sys.stdout, log), Tee(sys.stderr, log)
    server = None
    wrote = False
    try:
        admin = Admin(p["registry"])
        try:
            server = Server((settings["host"], int(settings["port"])), admin, allowed_hosts(settings))
        except OSError as exc:
            raise CLIError(f"Cannot start Admin on {settings['host']}:{settings['port']}: {exc}") from None
        record = {"pid": os.getpid(), "home": str(p["home"]), "host": settings["host"],
                  "port": int(settings["port"]), "started_at": time.time()}
        record["fingerprint"] = process_fingerprint(os.getpid())
        atomic_json(p["instance"], record)
        wrote = True
        urls = admin_urls(settings)
        print("ProUse Admin is running in the foreground.")
        for url in urls:
            print(f"Dashboard: {url}")
        print("Press Ctrl-C to stop. Logs: " + str(p["log"]))

        def terminate(_signum, _frame):
            raise KeyboardInterrupt

        old_term = signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("Stopping ProUse Admin.")
        finally:
            signal.signal(signal.SIGTERM, old_term)
        return EXIT_OK
    finally:
        if server is not None:
            server.server_close()
        if wrote:
            current = read_json(p["instance"], {})
            if current.get("pid") == os.getpid():
                p["instance"].unlink(missing_ok=True)
        sys.stdout, sys.stderr = old_out, old_err
        if log:
            log.close()


def status_value() -> tuple[dict, int]:
    p = paths()
    configured = p["registry"].is_file()
    record, stale = instance_record()
    settings = load_settings()
    if record:
        settings.update(host=record["host"], port=record["port"])
    running = record is not None
    ready = running and health(settings)
    result = {
        "local_installation": {"status": "ready", "version": "0.2.0"},
        "status": "running" if ready else ("starting_or_unhealthy" if running else "stopped"),
        "configured": configured,
        "running": running,
        "admin_ready": ready,
        "pid": record.get("pid") if record else None,
        "admin_urls": admin_urls(settings) if configured else [],
        "mcp": {"transport": "stdio", "command": mcp_command(), "handshake": "client_owned_not_tested"},
        "client_connection": "not_verified",
    }
    if stale:
        result["recovered"] = stale
    return result, EXIT_OK if ready else EXIT_NOT_RUNNING


def status_command(args) -> int:
    result, code = status_value()
    emit_runtime(result, args.json)
    return code


def stop_command(args) -> int:
    p = paths()
    record, stale = instance_record()
    if not record:
        result = {"status": "already_stopped", "detail": stale}
        emit(result, getattr(args, "json", False))
        return EXIT_OK
    pid = int(record["pid"])
    # Fingerprint and PROUSE_HOME ownership were checked by instance_record.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        p["instance"].unlink(missing_ok=True)
        emit({"status": "stopped", "pid": pid}, getattr(args, "json", False))
        return EXIT_OK
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not process_alive(pid) or process_fingerprint(pid) != record["fingerprint"]:
            p["instance"].unlink(missing_ok=True)
            emit({"status": "stopped", "pid": pid}, getattr(args, "json", False))
            return EXIT_OK
        time.sleep(0.05)
    raise CLIError(f"Owned ProUse process {pid} did not stop within {args.timeout:g}s")


def restart_command(args) -> int:
    # A JSON invocation must return exactly one result, including for restart.
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        stop_command(args)
    return start_command(args)


def logs_command(args) -> int:
    path = paths()["log"]
    if not path.exists():
        raise CLIError(f"No log exists yet at {path}", EXIT_NOT_RUNNING)
    with path.open(encoding="utf-8", errors="replace") as handle:
        if not args.follow:
            lines = handle.readlines()
            sys.stdout.writelines(lines[-args.lines:])
            return EXIT_OK
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.2)
        except KeyboardInterrupt:
            return EXIT_OK


def doctor_command(args) -> int:
    p = paths()
    checks = [{"name": "local_installation", "status": "ok", "detail": "ProUse 0.2.0"}]
    fatal = False
    if p["registry"].is_file():
        try:
            from workspace_registry import Registry
            registry = Registry.load(p["registry"])
            checks.append({"name": "configuration", "status": "ok", "workspaces": len(registry.workspaces)})
        except Exception as exc:
            checks.append({"name": "configuration", "status": "error", "detail": str(exc)})
            fatal = True
    else:
        checks.append({"name": "configuration", "status": "error", "detail": "Run prouse setup"})
        fatal = True
    executable = codex_path()
    checks.append({"name": "codex", "status": "ok" if executable else "warning",
                   "detail": executable or "Not found; context and Admin remain available, worker execution does not"})
    status, _ = status_value()
    checks.append({"name": "admin_service", "status": "ok" if status["admin_ready"] else "warning",
                   "detail": status["status"]})
    checks.append({"name": "mcp_handshake", "status": "not_tested",
                   "detail": "stdio MCP is started and owned by its client"})
    checks.append({"name": "client_connection", "status": "not_verified",
                   "detail": "Client authorization must be confirmed in that client"})
    result = {"status": "error" if fatal else "ok", "checks": checks, "home": str(p["home"])}
    emit(result, args.json)
    return EXIT_ERROR if fatal else EXIT_OK


def mcp_command() -> dict:
    # PATH can contain another ProUse installation. Use this interpreter's entry
    # point so the handoff and probe exercise the command the operator invoked.
    executable = Path(sys.executable).parent / "prouse"
    if executable.is_file():
        command = {"command": str(executable), "args": ["mcp", "serve"]}
    else:
        command = {"command": sys.executable, "args": ["-m", "prouse.cli", "mcp", "serve"]}
    command["env"] = {"PROUSE_HOME": str(home())}
    return command


async def probe_mcp(args) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    spec = mcp_command()
    params = StdioServerParameters(command=spec["command"], args=spec["args"],
                                   env=dict(os.environ, **spec["env"]))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            workspaces = await session.call_tool("list_workspaces", {})
            if workspaces.isError or not workspaces.structuredContent:
                raise CLIError("MCP initialized but list_workspaces failed")
            eligible = [item for item in workspaces.structuredContent["workspaces"]
                        if item.get("enabled") and item.get("context_available")]
            if args.workspace_id:
                eligible = [item for item in eligible if item["workspace_id"] == args.workspace_id]
            if not eligible:
                raise CLIError("MCP initialized, but no selected workspace has readable context")
            workspace_id = eligible[0]["workspace_id"]
            response = await session.call_tool("list_directory", {"workspace_id": workspace_id, "limit": 1})
            if response.isError:
                raise CLIError(f"MCP initialized, but the context read for {workspace_id} failed")
            return {"status": "ok", "handshake": "verified", "server": initialized.serverInfo.name,
                    "tools": len(listed.tools), "workspace_id": workspace_id,
                    "workspace_read": "verified", "client_connection": "not_verified"}


def mcp_check_command(args) -> int:
    def detail(exc):
        if isinstance(exc, BaseExceptionGroup):
            return "; ".join(detail(item) for item in exc.exceptions)
        return str(exc) or type(exc).__name__

    async def check():
        async with asyncio.timeout(args.timeout):
            return await probe_mcp(args)
    try:
        result = asyncio.run(check())
    except TimeoutError:
        raise CLIError(f"MCP check timed out after {args.timeout:g}s") from None
    except Exception as exc:
        raise CLIError(f"MCP check failed: {detail(exc)}. Run `prouse doctor --json`.") from None
    emit(result, args.json)
    return EXIT_OK


def mcp_serve_command(args) -> int:
    registry = args.registry or paths()["registry"]
    if not registry.is_file():
        raise CLIError("ProUse is not configured; run `prouse setup --workspace ...`", EXIT_USAGE)
    from context_server import create_server
    from workspace_registry import Registry
    configured = Registry.load(registry)
    if args.check_config:
        emit({"status": "valid", "registry_sha256": configured.sha256,
              "workspaces": configured.list_workspaces()}, True)
        return EXIT_OK
    create_server(configured, registry_path=registry).run(transport="stdio")
    return EXIT_OK


def service_kind(requested: str | None = None) -> str:
    if requested:
        return requested
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise CLIError("User services are supported on macOS launchd and Linux systemd --user only")


def service_identity() -> str:
    return hashlib.sha256(str(home()).encode()).hexdigest()[:10]


def service_spec(kind: str) -> tuple[Path, bytes | str, list[list[str]]]:
    p = paths()
    command = [sys.executable, "-m", "prouse.cli", "start"]
    identity = service_identity()
    env = {"PROUSE_HOME": str(p["home"])}
    if kind == "launchd":
        label = f"dev.prouse.admin.{identity}"
        target = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        value = {
            "Label": label, "ProgramArguments": command, "RunAtLoad": True,
            "KeepAlive": False, "EnvironmentVariables": env,
            "StandardOutPath": str(p["log"]), "StandardErrorPath": str(p["log"]),
        }
        content = plistlib.dumps(value)
        domain = f"gui/{os.getuid()}"
        actions = [["launchctl", "bootstrap", domain, str(target)]]
        return target, content, actions
    if kind == "systemd":
        name = f"prouse-{identity}.service"
        target = Path.home() / ".config" / "systemd" / "user" / name
        def quote(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        content = "\n".join([
            "[Unit]", "Description=ProUse Admin", "After=network.target", "",
            "[Service]", "Type=simple", f"Environment={quote('PROUSE_HOME=' + str(p['home']))}",
            "ExecStart=" + " ".join(quote(value) for value in command), "Restart=on-failure", "RestartSec=2", "",
            "[Install]", "WantedBy=default.target", "",
        ])
        actions = [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "--now", name]]
        return target, content, actions
    raise CLIError(f"Unknown service manager: {kind}")


def run_actions(actions: list[list[str]], dry_run: bool) -> list[dict]:
    results = []
    for argv in actions:
        if dry_run:
            results.append({"argv": argv, "status": "not_run"})
            continue
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        results.append({"argv": argv, "exit_code": completed.returncode,
                        "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()})
        if completed.returncode:
            raise CLIError(f"Service command failed ({completed.returncode}): {' '.join(argv)}: {completed.stderr.strip()}")
    return results


def service_command(args) -> int:
    kind = service_kind(args.manager)
    target, content, install_actions = service_spec(kind)
    identity = service_identity()
    if args.action == "install":
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        actions = run_actions(install_actions, args.dry_run)
        emit({"status": "not_run" if args.dry_run else "installed", "manager": kind,
              "path": str(target), "actions": actions}, args.json)
        return EXIT_OK
    if kind == "launchd":
        label = f"dev.prouse.admin.{identity}"
        domain = f"gui/{os.getuid()}"
        status_argv = ["launchctl", "print", f"{domain}/{label}"]
        uninstall_actions = [["launchctl", "bootout", domain, str(target)]]
    else:
        name = f"prouse-{identity}.service"
        status_argv = ["systemctl", "--user", "status", name, "--no-pager"]
        uninstall_actions = [["systemctl", "--user", "disable", "--now", name],
                             ["systemctl", "--user", "daemon-reload"]]
    if args.action == "status":
        if args.dry_run:
            result = {"status": "not_run", "manager": kind, "installed": target.exists(), "argv": status_argv}
            emit(result, args.json)
            return EXIT_OK
        completed = subprocess.run(status_argv, capture_output=True, text=True, check=False)
        result = {"status": "running" if completed.returncode == 0 else "not_running",
                  "manager": kind, "installed": target.exists(), "exit_code": completed.returncode,
                  "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
        emit(result, args.json)
        return EXIT_OK if completed.returncode == 0 else EXIT_NOT_RUNNING
    if not target.exists() and not args.dry_run:
        emit({"status": "already_uninstalled", "manager": kind, "path": str(target), "actions": []}, args.json)
        return EXIT_OK
    actions = run_actions(uninstall_actions, args.dry_run)
    if not args.dry_run:
        target.unlink(missing_ok=True)
    emit({"status": "uninstalled" if not args.dry_run else "not_run", "manager": kind,
          "path": str(target), "actions": actions}, args.json)
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="prouse", description="ProUse local MCP and Admin control",
        epilog="Get started: prouse setup --workspace .   Then: prouse admin")
    root.add_argument("--version", action="version", version="ProUse 0.2.0")
    commands = root.add_subparsers(dest="command")

    setup = commands.add_parser("setup", help="configure an approved workspace")
    setup.add_argument("--workspace")
    setup.add_argument("--workspace-id")
    setup.add_argument("--label")
    setup.add_argument("--no-input", action="store_true")
    setup.add_argument("--host")
    setup.add_argument("--port", type=int)
    setup.add_argument("--json", action="store_true")

    start = commands.add_parser("start", help="run Admin in the foreground")
    start.add_argument("--host")
    start.add_argument("--port", type=int)
    start.add_argument("--json", action="store_true")
    start.add_argument("--background", action="store_true", help="start and return after the health check")
    start.add_argument("--start-timeout", type=float, default=15)

    admin = commands.add_parser("admin", help="start the dashboard in the background")
    admin.add_argument("--host")
    admin.add_argument("--port", type=int)
    admin.add_argument("--foreground", dest="background", action="store_false",
                       help="keep Admin attached to this terminal")
    admin.add_argument("--start-timeout", type=float, default=15)
    admin.add_argument("--json", action="store_true")

    for name in ("stop", "restart"):
        item = commands.add_parser(name, help=("stop the owned Admin process" if name == "stop" else "stop then run Admin in the foreground"))
        item.add_argument("--timeout", type=float, default=10)
        item.add_argument("--host")
        item.add_argument("--port", type=int)
        item.add_argument("--json", action="store_true")
        if name == "restart":
            item.add_argument("--background", action="store_true")
            item.add_argument("--start-timeout", type=float, default=15)

    status = commands.add_parser("status", help="show local readiness")
    status.add_argument("--json", action="store_true")
    logs = commands.add_parser("logs", help="show Admin logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=100)
    doctor = commands.add_parser("doctor", help="diagnose installation and capabilities")
    doctor.add_argument("--json", action="store_true")

    mcp = commands.add_parser("mcp", help="stdio MCP commands")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_commands.add_parser("serve", help="run client-owned stdio MCP")
    serve.add_argument("--registry", type=Path)
    serve.add_argument("--check-config", action="store_true")
    mcp_commands.add_parser("config", help="print the local MCP client JSON configuration")
    check = mcp_commands.add_parser("check", help="verify a real stdio handshake and workspace read")
    check.add_argument("--workspace-id")
    check.add_argument("--timeout", type=float, default=20)
    check.add_argument("--json", action="store_true")

    service = commands.add_parser("service", help="manage an isolated user login service")
    service.add_argument("action", choices=("install", "status", "uninstall"))
    service.add_argument("--manager", choices=("launchd", "systemd"))
    service.add_argument("--dry-run", action="store_true")
    service.add_argument("--json", action="store_true")
    return root


def no_args(root: argparse.ArgumentParser) -> int:
    if not paths()["registry"].is_file():
        if sys.stdin.isatty() and sys.stdout.isatty():
            return setup_command(argparse.Namespace(workspace=None, workspace_id=None, label=None,
                                 no_input=False, host=None, port=None, json=False))
        root.print_help(sys.stderr)
        print("\nNot configured. Run: prouse setup --workspace /path/to/project --no-input", file=sys.stderr)
        return EXIT_USAGE
    result, code = status_value()
    emit_runtime(result)
    return code


def main(argv=None) -> int:
    root = parser()
    args = root.parse_args(argv)
    try:
        if args.command is None:
            return no_args(root)
        handlers = {
            "setup": setup_command, "start": start_command, "admin": start_command, "stop": stop_command,
            "restart": restart_command, "status": status_command, "logs": logs_command,
            "doctor": doctor_command, "service": service_command,
        }
        if args.command == "mcp":
            if args.mcp_command == "config":
                emit({"mcpServers": {"prouse": mcp_command()}}, True)
                return EXIT_OK
            if args.mcp_command == "check":
                return mcp_check_command(args)
            return mcp_serve_command(args)
        return handlers[args.command](args)
    except (CLIError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, CLIError) else EXIT_ERROR
        if getattr(args, "json", False):
            emit({"status": "error", "error": str(exc), "exit_code": code}, True)
        else:
            print(f"prouse: {exc}", file=sys.stderr)
        return code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
