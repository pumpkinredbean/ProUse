"""The optional local dashboard: a small web page for managing registered workspaces.

It has no login and binds to 127.0.0.1 by default. Requests must use an approved Host
header (against DNS rebinding); changes must be same-origin JSON POSTs that carry the
per-process CSRF token from /api/session. Request bodies are never logged.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import json
from pathlib import Path
import secrets
import socketserver

from .. import __version__
from ..configuration import Configuration
from ..errors import CLIError
from ..integrations import mcp

ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                      "frame-ancestors 'none'; base-uri 'none'"}
MAX_BODY_BYTES = 64 * 1024
MAX_BROWSE_ENTRIES = 300
SKIPPED_DIRECTORIES = {"__pycache__", "node_modules", "target", "venv"}


class DashboardError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _text(body: dict, key: str, *, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DashboardError(f"{key} must be a non-empty string")
    return value.strip()


def _flag(body: dict, key: str) -> bool | None:
    value = body.get(key)
    if value is not None and not isinstance(value, bool):
        raise DashboardError(f"{key} must be true or false")
    return value


class Dashboard:
    """The JSON API behind the page. Changes go through Configuration, exactly like the CLI."""

    def __init__(self, config: Configuration):
        self.config = config

    def state(self) -> dict:
        return {**self.config.listing(), "version": __version__, "home": str(Path.home().resolve()),
                "mcp_config": {"mcpServers": {"prouse": mcp.command(self.config.paths)}}}

    def browse(self, path: str | None) -> dict:
        """List subfolders under the home folder so a project can be picked without typing its path."""
        home = Path.home().resolve()
        requested = Path(path).expanduser() if path else home
        if not requested.is_absolute():
            raise DashboardError("Use an absolute folder path")
        try:
            target = requested.resolve(strict=True)
        except (OSError, RuntimeError):
            raise DashboardError("Folder not found", 404) from None
        if target != home and home not in target.parents:
            raise DashboardError("Folder browsing is limited to your home folder; type other paths directly", 403)
        if not target.is_dir():
            raise DashboardError("Not a folder")
        directories = []
        try:
            children = sorted(target.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            raise DashboardError("Folder cannot be read", 403) from None
        for child in children:
            if child.name.startswith(".") or child.name in SKIPPED_DIRECTORIES:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    directories.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
            if len(directories) >= MAX_BROWSE_ENTRIES:
                break
        return {"path": str(target), "parent": str(target.parent) if target != home else None,
                "can_select": target != home, "directories": directories}

    def dispatch(self, method: str, path: str, body: dict) -> dict:
        if method == "GET" and path == "/api/state":
            return self.state()
        if method == "POST":
            if path == "/api/workspaces/add":
                return self.config.add(_text(body, "path"), workspace_id=_text(body, "id", required=False),
                                       label=_text(body, "label", required=False),
                                       make_default=bool(_flag(body, "default")))
            if path == "/api/workspaces/update":
                return self.config.update(_text(body, "id"), label=_text(body, "label", required=False),
                                          enabled=_flag(body, "enabled"), make_default=bool(_flag(body, "default")))
            if path == "/api/workspaces/remove":
                return self.config.remove(_text(body, "id"))
            if path == "/api/browse":
                return self.browse(_text(body, "path", required=False))
            if path == "/api/mcp/check":
                try:
                    return mcp.check(self.config.paths)
                except CLIError as exc:
                    return {"status": "failed", "error": str(exc)}
        raise DashboardError("Not found", 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], dashboard: Dashboard, hosts: list[str]):
        super().__init__(address, Handler)
        self.dashboard = dashboard
        self.hosts = set(hosts)
        self.csrf = secrets.token_urlsafe(24)

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host's FQDN, a reverse DNS query that can stall
        # startup for seconds on macOS. Nothing here uses server_name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *args) -> None:
        pass  # Request bodies are never logged.

    def reply(self, value, status: int = 200, content_type: str = "application/json") -> None:
        data = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
        self.send_response(status)
        for key, item in {"Content-Type": content_type, "Content-Length": str(len(data)), **HEADERS}.items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(data)

    def process(self) -> None:
        try:
            host = self.headers.get("Host", "")
            if host not in self.server.hosts:
                raise DashboardError("Unapproved host", 403)
            path = self.path.split("?", 1)[0]
            if self.command == "POST" and self.headers.get("Origin") != "http://" + host:
                raise DashboardError("Same-origin request required", 403)
            if self.command == "GET" and path in ASSETS:
                name, content_type = ASSETS[path]
                asset = resources.files("prouse_assets").joinpath("admin_ui", name)
                self.reply(asset.read_bytes(), content_type=content_type)
                return
            if self.command == "GET" and path == "/healthz":
                self.reply({"status": "ready"})
                return
            if self.command == "GET" and path == "/api/session":
                self.reply({"csrf": self.server.csrf})
                return
            body = {}
            if self.command == "POST":
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise DashboardError("JSON content type required", 415)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise DashboardError("Invalid body size", 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise DashboardError("JSON object required")
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
                    raise DashboardError("Browser request token required; reload the page", 403)
            self.reply(self.server.dashboard.dispatch(self.command, path, body))
        except DashboardError as exc:
            self.reply({"error": str(exc)}, exc.status)
        except CLIError as exc:
            self.reply({"error": str(exc)}, 400)
        except (ValueError, TypeError):
            self.reply({"error": "Invalid request"}, 400)
        except Exception:
            self.reply({"error": "Local operation failed; run `prouse doctor` on this computer"}, 500)

    do_GET = process
    do_POST = process
