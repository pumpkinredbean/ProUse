import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from prouse.configuration import Configuration
from prouse.runtime.dashboard import Dashboard, Server
from prouse.state import InstancePaths


class DashboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.user_home = self.base / "user"
        self.project = self.user_home / "code" / "app"
        self.project.mkdir(parents=True)
        (self.user_home / "code" / ".hidden").mkdir()
        home = mock.patch("pathlib.Path.home", return_value=self.user_home)
        home.start()
        self.addCleanup(home.stop)
        self.config = Configuration(InstancePaths(self.base / "prouse-home"))
        self.server = Server(("127.0.0.1", 0), Dashboard(self.config), [])
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        self.server.hosts = {self.host}
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.csrf = self.request("GET", "/api/session")[1]["csrf"]

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, timeout=10)
        self.addCleanup(connection.close)
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        sent = {"Host": self.host}
        if method == "POST":
            sent.update({"Origin": f"http://{self.host}", "Content-Type": "application/json",
                         "X-CSRF-Token": self.csrf})
        sent.update(headers or {})
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        payload = response.read()
        if response.getheader("Content-Type") == "application/json":
            payload = json.loads(payload)
        return response.status, payload, response

    def post(self, path, body, **headers):
        return self.request("POST", path, body, headers)

    def test_serves_the_page_with_strict_headers(self):
        status, page, response = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"ProUse", page)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.request("GET", "/app.js")[0], 200)
        status, _, response = self.request("GET", "/icon.svg")
        self.assertEqual((status, response.getheader("Content-Type")), (200, "image/svg+xml"))
        self.assertEqual(self.request("GET", "/healthz")[1], {"status": "ready"})

    def test_workspace_lifecycle(self):
        status, state, _ = self.request("GET", "/api/state")
        self.assertEqual((status, state["workspaces"]), (200, []))
        self.assertEqual(state["mcp_config"]["mcpServers"]["prouse"]["args"], ["mcp", "serve"])
        self.assertEqual(state["home"], str(self.user_home))
        status, added, _ = self.post("/api/workspaces/add", {"path": str(self.project), "label": "My app"})
        self.assertEqual((status, added["workspace_id"], added["default"]), (200, "app", True))
        status, _, _ = self.post("/api/workspaces/update", {"id": "app", "enabled": False, "label": "Renamed"})
        self.assertEqual(status, 200)
        workspace = self.request("GET", "/api/state")[1]["workspaces"][0]
        self.assertEqual((workspace["label"], workspace["status"]), ("Renamed", "disabled"))
        status, error, _ = self.post("/api/workspaces/update", {"id": "app", "default": True})
        self.assertEqual(status, 400)
        self.assertIn("disabled", error["error"])
        self.assertEqual(self.post("/api/workspaces/remove", {"id": "app"})[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[1]["workspaces"], [])
        self.assertTrue(self.project.is_dir())

    def test_invalid_workspace_requests_explain_the_problem(self):
        status, error, _ = self.post("/api/workspaces/add", {"path": str(self.base / "missing")})
        self.assertEqual(status, 400)
        self.assertIn("Workspace is not accessible", error["error"])
        status, error, _ = self.post("/api/workspaces/add", {"path": str(self.user_home)})
        self.assertEqual(status, 400)
        status, error, _ = self.post("/api/workspaces/add", {"path": 3})
        self.assertEqual((status, error["error"]), (400, "path must be a non-empty string"))
        status, error, _ = self.post("/api/workspaces/update", {"id": "app", "enabled": "yes"})
        self.assertEqual((status, error["error"]), (400, "enabled must be true or false"))

    def test_browsing_stays_inside_the_home_folder(self):
        status, listing, _ = self.post("/api/browse", {})
        self.assertEqual((status, listing["path"], listing["can_select"]), (200, str(self.user_home), False))
        self.assertEqual(listing["directories"], [{"name": "code", "path": str(self.user_home / "code")}])
        status, listing, _ = self.post("/api/browse", {"path": str(self.user_home / "code")})
        self.assertEqual([item["name"] for item in listing["directories"]], ["app"])
        self.assertEqual((listing["parent"], listing["can_select"]), (str(self.user_home), True))
        self.assertEqual(self.post("/api/browse", {"path": str(self.base)})[0], 403)
        self.assertEqual(self.post("/api/browse", {"path": "relative"})[0], 400)
        self.assertEqual(self.post("/api/browse", {"path": str(self.user_home / "nope")})[0], 404)

    def test_mcp_check_failure_is_reported_in_the_page(self):
        status, result, _ = self.post("/api/mcp/check", {})
        self.assertEqual((status, result["status"]), (200, "failed"))
        self.assertIn("prouse setup --workspace", result["error"])

    def test_browser_protections(self):
        body = {"path": str(self.project)}
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post("/api/workspaces/add", body, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/workspaces/add", body, **{"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.post("/api/workspaces/add", body, **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("/api/workspaces/add", b"x" * 70000)[0], 413)
        self.assertEqual(self.post("/api/workspaces/add", b"[1]")[0], 400)
        self.assertEqual(self.request("GET", "/api/unknown")[0], 404)
        self.assertFalse(self.config.paths.registry.exists())


if __name__ == "__main__":
    unittest.main()
