#!/usr/bin/env python3
"""Exercise the real stdio MCP client/server without any OpenAI API requests."""

import asyncio
import argparse
import hashlib
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent


async def run(registry=None):
    params = StdioServerParameters(
        command=sys.executable,
        args=([str(SCRIPTS / "context_server.py"), "--registry", str(registry)] if registry else
              [str(SCRIPTS / "context_server.py"), "--root", str(ROOT),
               "--policy", str(SCRIPTS.parent / "references/context-policy.json")]),
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            from test_context_server import TOOLS
            assert set(names) == TOOLS
            for tool in listed.tools:
                assert not tool.annotations.openWorldHint
                if tool.name in {"submit_codex_worker_task", "exec_command", "write_execution_stdin",
                                 "cancel_execution", "write_file", "edit_file", "delete_file"}:
                    assert not tool.annotations.readOnlyHint
                    assert tool.annotations.idempotentHint
                else:
                    assert tool.annotations.readOnlyHint
            returned_chars = []

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                assert not response.isError, response
                result = response.structuredContent
                assert result is not None
                assert sum(len(item.text) for item in response.content if item.type == "text") < 100
                returned_chars.append(len(json.dumps(result, ensure_ascii=False)))
                return result

            index = await call("workspace_index", {"query": "src/strategy/market_following.rs"})
            assert len(index["files"]) == 1
            source = index["files"][0]
            listing = await call("list_directory", {"path": "src/strategy", "depth": 1})
            assert any(entry["path"] == source["path"] for entry in listing["entries"])
            globbed = await call("glob_files", {"patterns": ["src/**/*.rs"], "limit": 5})
            assert globbed["files"] and all(item["path"].startswith("src/") for item in globbed["files"])
            search = await call("search_files", {"queries": ["pub fn apply_signal_event", "pub fn initialize_on_first_quote"],
                                                  "path_prefix": source["path"], "expected_view_id": index["view_id"]})
            assert search["total_matches"] == 2
            controller = await call("search_files", {"queries": ["struct FairRepriceController", "impl FairRepriceController"],
                                                     "path_prefix": "src/", "expected_view_id": index["view_id"]})
            assert controller["total_matches"] == 2
            assert {item["path"] for item in controller["matches"]} == {"src/paper.rs"}
            regex = await call("grep_files", {"pattern": r"pub fn \w+", "path_prefix": source["path"], "limit": 5})
            assert regex["total_matches"] >= 2
            read = await call("read_files", {"requests": [{"path": source["path"], "start_line": 1, "end_line": source["lines"], "sha256": source["sha256"]}],
                                              "expected_view_id": index["view_id"]})
            exact = "\n".join(f"{i}: {line}" for i, line in enumerate((ROOT / source["path"]).read_text().splitlines(), 1))
            assert read["files"][0]["text"] == exact
            window = await call("read_file_bytes", {"path": source["path"], "offset": 0, "limit": 64})
            assert window["text"] == (ROOT / source["path"]).read_text()[:64]
            delta = await call("changed_files", {"known": {source["path"]: source["sha256"]}})
            assert delta["unchanged_files"] == 1
            assert not delta["added"] and not delta["modified"] and not delta["deleted"]
            probe_path = ("research_artifacts/2026-09-20/pro_codex_orchestration_v1/"
                          "R011-T1-terminal-provenance-probe/probe.py")
            probe_index = await call("workspace_index", {"query": probe_path})
            assert len(probe_index["files"]) == 1
            probe = probe_index["files"][0]
            probe_read = await call("read_files", {"requests": [{
                "path": probe_path, "start_line": 1, "end_line": 240,
                "sha256": probe["sha256"],
            }], "expected_view_id": probe_index["view_id"]})
            assert probe_read["files"][0]["sha256"] == hashlib.sha256((ROOT / probe_path).read_bytes()).hexdigest()
            assert "module.run_task" in probe_read["files"][0]["text"]
            denied = await session.call_tool("read_files", {"requests": [{"path": "../outside.txt"}]})
            assert denied.isError
            print(json.dumps({"transport": "MCP stdio", "server": initialized.serverInfo.name,
                              "protocol": initialized.protocolVersion, "tools": names,
                              "available_files": index["available_files"], "unavailable_files": index["unavailable_files"],
                              "exact_source_read_sha256": hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest(),
                              "search_hits": search["total_matches"], "directory_prefix_hits": controller["total_matches"],
                              "regex_hits": regex["total_matches"], "glob_hits": len(globbed["files"]),
                              "worker_probe_read_sha256": probe_read["files"][0]["sha256"],
                              "tool_result_characters": returned_chars,
                              "traversal_denied": True, "external_model_calls": 0,
                              "status": "passed"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path)
    asyncio.run(run(parser.parse_args().registry))
