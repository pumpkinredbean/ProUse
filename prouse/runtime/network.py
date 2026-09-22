"""Admin listener addresses and local health checks."""
import json
import socket
import urllib.error
import urllib.request

from ..configuration import AdminSettings


def lan_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            return address if not address.startswith("127.") else None
        except OSError:
            return None


def admin_urls(settings: AdminSettings) -> list[str]:
    if settings.host != "0.0.0.0":
        return [f"http://{settings.host}:{settings.port}/"]
    urls = [f"http://127.0.0.1:{settings.port}/"]
    if address := lan_ip():
        urls.append(f"http://{address}:{settings.port}/")
    return urls


def allowed_hosts(settings: AdminSettings) -> list[str]:
    hosts = {f"localhost:{settings.port}", f"127.0.0.1:{settings.port}"}
    hosts.update(url.removeprefix("http://").rstrip("/") for url in admin_urls(settings))
    return sorted(hosts)


def healthy(settings: AdminSettings, timeout: float = 0.7) -> bool:
    host = "127.0.0.1" if settings.host == "0.0.0.0" else settings.host
    # Local health must not be sent through HTTP_PROXY configured for remote traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{settings.port}/healthz", timeout=timeout) as response:
            return response.status == 200 and json.load(response).get("status") == "ready"
    except (OSError, ValueError, urllib.error.URLError):
        return False
