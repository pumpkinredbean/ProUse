"""Owned Admin lifecycle, independent of argument parsing and presentation."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Callable

from .. import __version__
from ..configuration import AdminSettings, Configuration
from ..errors import CLIError
from ..state import file_lock
from .logs import capture
from .network import admin_urls, allowed_hosts, healthy
from .process import InstanceFile, ProcessRecord


class AdminRuntime:
    def __init__(self, config: Configuration):
        self.config = config
        self.paths = config.paths
        self.instance = InstanceFile(self.paths)

    def status(self) -> dict:
        record, stale = self.instance.current()
        settings = record.settings if record else self.config.settings()
        ready = record is not None and healthy(settings)
        result = {
            "local_installation": {"status": "ready", "version": __version__},
            "status": "running" if ready else ("starting_or_unhealthy" if record else "stopped"),
            "configured": self.paths.registry.is_file(),
            "running": record is not None,
            "admin_ready": ready,
            "pid": record.pid if record else None,
            "admin_urls": admin_urls(settings) if self.paths.registry.is_file() else [],
        }
        if stale:
            result["recovered"] = stale
        return result

    def start(self, *, host: str | None = None, port: int | None = None,
              timeout: float = 15) -> dict:
        with file_lock(self.paths.run / "lifecycle.lock"):
            return self._start(host, port, timeout)

    def _start(self, host: str | None, port: int | None, timeout: float) -> dict:
        self.config.registry()
        settings = self.config.settings().override(host, port)
        record, _ = self.instance.current()
        if record:
            if (host is not None and host != record.host) or (port is not None and port != record.port):
                raise CLIError("ProUse is already running at another address. Use `prouse restart` to change it.")
            result = self.status()
            if not result["admin_ready"]:
                raise CLIError("The owned ProUse process is not healthy. Run `prouse logs` or `prouse restart`.")
            return {**result, "status": "already_running"}

        self.paths.prepare_runtime()
        with self.paths.log.open("a", encoding="utf-8") as output:
            child = subprocess.Popen(
                [sys.executable, "-m", "prouse.cli", "run", "--host", settings.host,
                 "--port", str(settings.port)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=self.paths.home,
                env=dict(os.environ, PROUSE_HOME=str(self.paths.home), PYTHONUNBUFFERED="1",
                         _PROUSE_LOG_REDIRECTED=str(self.paths.log)),
            )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise CLIError(f"ProUse could not start (exit {child.returncode}). Run `prouse logs`.")
                record, _ = self.instance.current()
                if record and record.pid == child.pid and healthy(record.settings):
                    return self.status()
                time.sleep(0.1)
            raise CLIError(f"ProUse did not become ready within {timeout:g}s. Run `prouse logs`.")
        except BaseException:
            # Startup owns this child only. Never signal an unrelated port listener.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            self.instance.current()  # Recover a stale receipt if startup was interrupted.
            raise

    def run(self, *, host: str | None = None, port: int | None = None,
            on_ready: Callable[[dict], None]) -> None:
        self.config.registry()
        settings = self.config.settings().override(host, port)
        try:
            with file_lock(self.instance.lock, blocking=False):
                # Support receipts written by earlier CLI versions as well.
                record, _ = self.instance.current()
                if record:
                    raise CLIError("ProUse is already running. Run `prouse status` or `prouse stop`.")
                self._serve(settings, on_ready)
        except BlockingIOError:
            raise CLIError("ProUse is already running or starting. Run `prouse status`.") from None

    def _serve(self, settings: AdminSettings, on_ready: Callable[[dict], None]) -> None:
        from admin_server import Admin, Server

        self.paths.prepare_runtime()
        with capture(self.paths.log):
            admin = Admin(self.paths.registry)
            try:
                server = Server((settings.host, settings.port), admin, allowed_hosts(settings))
            except OSError as exc:
                raise CLIError(f"Cannot start Admin on {settings.host}:{settings.port}: {exc}") from None
            record = None

            def terminate(_signum, _frame):
                raise KeyboardInterrupt

            old_term = signal.signal(signal.SIGTERM, terminate)
            try:
                record = ProcessRecord.capture(self.paths, settings)
                self.instance.write(record)
                on_ready({"status": "running", "pid": record.pid, "admin_urls": admin_urls(settings),
                          "mode": "foreground", "log": str(self.paths.log)})
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGTERM, old_term)
                server.server_close()
                if record:
                    self.instance.remove(record)

    def stop(self, *, timeout: float = 10) -> dict:
        with file_lock(self.paths.run / "lifecycle.lock"):
            return self._stop(timeout)

    def _stop(self, timeout: float) -> dict:
        record, stale = self.instance.current()
        if not record:
            return {"status": "already_stopped", "detail": stale}
        # Verify ownership immediately before signalling, including PID reuse.
        if record.is_current():
            try:
                os.kill(record.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not record.is_current():
                self.instance.current()
                return {"status": "stopped", "pid": record.pid}
            time.sleep(0.05)
        raise CLIError(f"Owned ProUse process {record.pid} did not stop within {timeout:g}s")

    def restart(self, *, host: str | None = None, port: int | None = None,
                timeout: float = 10, start_timeout: float = 15) -> dict:
        # Invalid replacement configuration must not stop a healthy process.
        self.config.registry()
        self.config.settings().override(host, port)
        with file_lock(self.paths.run / "lifecycle.lock"):
            self._stop(timeout)
            return self._start(host, port, start_timeout)
