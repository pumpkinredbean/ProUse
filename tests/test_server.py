import json
import re
import unittest

from mcp.shared.memory import create_connected_server_and_client_session

from support import WorkspaceCase

from prouse.server import create_server

TOOLS = {"workspaces", "read", "write", "edit", "bash", "bash_job", "grep", "find", "ls"}


class ServerTests(WorkspaceCase, unittest.IsolatedAsyncioTestCase):
    def run(self, result=None):
        # The in-memory session must be opened and closed in the task that runs the test body.
        test = getattr(self, self._testMethodName)

        async def connected():
            server, jobs = create_server(self.registry, self.output)
            try:
                async with create_connected_server_and_client_session(server) as self.session:
                    await test()
            finally:
                jobs.shutdown()

        setattr(self, self._testMethodName, connected)
        return super().run(result)

    async def call(self, name, arguments=None):
        result = await self.session.call_tool(name, arguments or {})
        return result, "\n".join(item.text for item in result.content if item.type == "text")

    async def test_lists_the_pi_tool_set(self):
        listed = (await self.session.list_tools()).tools
        self.assertEqual({tool.name for tool in listed}, TOOLS)
        hints = {tool.name: tool.annotations.readOnlyHint for tool in listed}
        self.assertTrue(all(hints[name] for name in ("workspaces", "read", "grep", "find", "ls")))
        self.assertFalse(any(hints[name] for name in ("write", "edit", "bash", "bash_job")))
        edit = next(tool for tool in listed if tool.name == "edit")
        self.assertIn("oldText", json.dumps(edit.inputSchema))

    async def test_workspaces_then_file_round_trip(self):
        self.write("AGENTS.md", "Run the tests with make test.\n")
        result, text = await self.call("workspaces")
        self.assertFalse(result.isError)
        self.assertIn("make test", text)
        result, text = await self.call("write", {"path": "notes/todo.txt", "content": "one\ntwo\n"})
        self.assertEqual(text, "Successfully created notes/todo.txt (8 bytes)")
        result, text = await self.call("edit", {"path": "notes/todo.txt",
                                                "edits": [{"oldText": "two", "newText": "three"}]})
        self.assertFalse(result.isError, text)
        result, text = await self.call("read", {"path": "notes/todo.txt"})
        self.assertEqual(text, "one\nthree\n")
        _, text = await self.call("ls", {"path": "notes"})
        self.assertEqual(text, "todo.txt")
        _, text = await self.call("find", {"pattern": "*.txt"})
        self.assertEqual(text, "notes/todo.txt")
        _, text = await self.call("grep", {"pattern": "three"})
        self.assertEqual(text, "notes/todo.txt:2: three")

    async def test_edits_sent_as_a_json_string_are_accepted(self):
        self.write("a.txt", "alpha\n")
        result, text = await self.call("edit", {"path": "a.txt",
                                                "edits": json.dumps([{"oldText": "alpha", "newText": "beta"}])})
        self.assertFalse(result.isError, text)
        self.assertEqual(self.text("a.txt"), "beta\n")

    async def test_failures_are_error_results_the_model_can_read(self):
        result, text = await self.call("read", {"path": "missing.txt"})
        self.assertTrue(result.isError)
        self.assertEqual(text, "File not found: missing.txt")
        result, text = await self.call("read", {"path": "/etc/hostname"})
        self.assertTrue(result.isError)
        self.assertIn("outside the registered workspaces", text)
        result, text = await self.call("bash", {"command": "exit 4"})
        self.assertTrue(result.isError)
        self.assertEqual(text, "Command exited with code 4")

    async def test_images_are_returned_as_image_content(self):
        from test_files import PNG
        self.write("pixel.png", PNG)
        result = await self.session.call_tool("read", {"path": "pixel.png"})
        self.assertEqual([item.type for item in result.content], ["text", "image"])
        self.assertEqual(result.content[1].mimeType, "image/png")

    async def test_full_bash_output_can_be_read_back(self):
        _, text = await self.call("bash", {"command": "seq 1 3000"})
        path = re.search(r"Full output: (.+)\]$", text).group(1)
        result, text = await self.call("read", {"path": path, "offset": 1, "limit": 2})
        self.assertFalse(result.isError, text)
        self.assertTrue(text.startswith("1\n2\n"))


if __name__ == "__main__":
    unittest.main()
