"""Contract regressions for direct-by-tool-choice execution.

Direct calls share the workspace registry with delegated work.  They must not add an execution
profile, mode picker, session-opening step, runtime selector, or a model/thread/turn surface.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_task_broker_v2 import FAKE
from workspace_registry import Registry, ResolutionError


NATIVE_DIRECT_TOOLS = {
    "exec_command", "get_execution", "write_execution_stdin", "cancel_execution", "workspace_diff", "read_artifact",
}
RETIRED_DIRECT_TOOLS = {"list_execution_profiles", "open_execution", "close_execution"}


class NativeDirectContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        (self.base / "alpha").mkdir()
        (self.base / "alpha" / "read.md").write_text("source\n")
        (self.base / "policy.json").write_text(json.dumps({"version": 1, "files": ["read.md"]}))
        fake = self.base / "fake-codex"; fake.write_text(FAKE); fake.chmod(0o755)
        self.config = {
            "version": 1, "default_workspace_id": "alpha", "default_worker_profile": "standard",
            "codex_bin": str(fake), "model_capabilities": {"test-model": ["high"]},
            "worker_profiles": [{"id": "standard", "model": "test-model", "reasoning_effort": "high"}],
            "workspaces": [{"id": "alpha", "label": "Alpha", "root": "alpha", "enabled": True, "context_policy": "policy.json"}],
        }
        (self.base / "registry.json").write_text(json.dumps(self.config))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    @asynccontextmanager
    async def connection(self):
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("context_server.py")), "--registry", str(self.base / "registry.json")])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session

    def test_normal_workspace_registry_needs_no_direct_setup_and_rejects_retired_keys(self):
        Registry(self.config, self.base)
        for key in ("default_execution_profile", "default_execution_mode", "execution_profiles", "runtimes"):
            with self.subTest(key=key):
                invalid = dict(self.config, **{key: "retired" if key.startswith("default") else []})
                with self.assertRaises(ResolutionError):
                    Registry(invalid, self.base)

    async def test_mcp_exposes_direct_tools_without_profiles_or_open_close_session(self):
        async with self.connection() as session:
            tools = {item.name: item for item in (await session.list_tools()).tools}
        self.assertTrue(NATIVE_DIRECT_TOOLS <= set(tools))
        self.assertFalse(RETIRED_DIRECT_TOOLS & set(tools))
        for name in NATIVE_DIRECT_TOOLS:
            schema = tools[name].inputSchema
            encoded = json.dumps(schema)
            self.assertIn("workspace_id", encoded, f"{name} does not select a registered workspace")
            self.assertNotIn("execution_profile_id", encoded)
            self.assertNotIn("execution_id", encoded)
            self.assertNotIn("runtime_id", encoded)
            self.assertNotIn("worker_model", encoded)
            self.assertNotIn("codex_thread_id", encoded)
            self.assertNotIn("timeout_ms", encoded)
            self.assertNotIn("output_bytes_cap", encoded)

        # Native command/exec protocol fields are an explicit command choice, not a
        # registry/Admin timeout-cap policy.  Read pagination is likewise required to
        # preserve output/artifact bytes beyond one response.
        command_schema = json.dumps(tools["exec_command"].inputSchema)
        for field in ("timeoutMs", "outputBytesCap", "disableTimeout", "disableOutputCap"):
            self.assertIn(field, command_schema)
        get_schema = json.dumps(tools["get_execution"].inputSchema)
        for field in ("output_offset", "output_limit", "stdout_offset", "stderr_offset"):
            self.assertIn(field, get_schema)
        artifact_schema = json.dumps(tools["read_artifact"].inputSchema)
        self.assertIn("offset", artifact_schema)
        self.assertIn("limit", artifact_schema)


if __name__ == "__main__":
    unittest.main()
