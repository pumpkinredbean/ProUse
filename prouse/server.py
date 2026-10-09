"""The ProUse MCP server: pi's coding tools over stdio, scoped to registered workspaces."""
from __future__ import annotations

from pathlib import Path
import signal
import sys
from typing import Annotated, Any, Callable

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from .tools import Result, ToolError, edit as edit_tool, files, search, shell
from .workspaces import Workspaces

PATHS = "Paths are absolute, start with ~, or are relative to the default workspace (see workspaces)."

INSTRUCTIONS = f"""ProUse gives you a coding agent's tools on the user's own computer: read files, run \
commands, edit code and write new files inside the workspaces the user registered.

- Call workspaces at the start, then again with the project's name, and follow the project \
instructions it returns.
- Use read to examine files instead of cat or sed.
- Use edit for precise changes (edits[].oldText must match exactly). Put several separate changes \
to one file in one edit call.
- Use write only for new files or complete rewrites.
- Use bash for builds, tests, git and other commands. A command still running after \
{shell.YIELD_SECONDS:g} seconds continues as a job you can check with bash_job.
- Show file paths clearly when working with files."""


class Replacement(BaseModel):
    oldText: str = Field(description="Exact text for one targeted replacement. It must be unique in the original "
                                     "file and must not overlap with any other edits[].oldText in the same call.")
    newText: str = Field(description="Replacement text for this targeted edit.")


def to_mcp(result: Result) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=result.text)]
    content += [ImageContent(type="image", data=data, mimeType=mime) for data, mime in result.images]
    return CallToolResult(content=content, isError=result.is_error)


def failure(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


def _os_error(exc: OSError) -> str:
    reason = exc.strerror or str(exc)
    return f"{reason}: {exc.filename}" if exc.filename else reason


async def _guard(call: Callable[[], Any]) -> CallToolResult:
    try:
        return to_mcp(await call())
    except ToolError as exc:
        return failure(str(exc))
    except OSError as exc:
        return failure(_os_error(exc))


def _in_thread(function: Callable[..., Result], *args: Any) -> Callable[[], Any]:
    return lambda: anyio.to_thread.run_sync(function, *args)


def create_server(registry: Path, output_dir: Path) -> tuple[FastMCP, shell.Jobs]:
    workspaces = Workspaces(registry, extra_readable=(output_dir,))
    jobs = shell.Jobs(output_dir)
    # WARNING keeps the client's MCP log free of a line per request.
    server = FastMCP("ProUse", instructions=INSTRUCTIONS, log_level="WARNING")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    overwrite = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
    mutate = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
    execute = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

    @server.tool(name="workspaces", annotations=read_only, structured_output=False, description=(
        "List the folders on the user's computer that these tools work in, and load a workspace's project "
        "instructions (AGENTS.md / CLAUDE.md). Call it first in a conversation, then with name=<workspace id> "
        "once you know which project you are working on, and follow the instructions it returns."))
    async def list_workspaces(
        name: Annotated[str | None, Field(description="Workspace id, or any path inside it, whose project "
                                                      "instructions to load")] = None,
    ) -> CallToolResult:
        return await _guard(_in_thread(lambda: Result(workspaces.describe(name))))

    @server.tool(annotations=read_only, structured_output=False, description=(
        "Read the contents of a file. Supports text files and images (jpg, png, gif, webp). Images are sent as "
        "attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use "
        "offset/limit for large files. When you need the full file, continue with offset until complete. " + PATHS))
    async def read(
        path: Annotated[str, Field(description="Path to the file to read")],
        offset: Annotated[int | None, Field(description="Line number to start reading from (1-indexed)")] = None,
        limit: Annotated[int | None, Field(description="Maximum number of lines to read")] = None,
    ) -> CallToolResult:
        return await _guard(_in_thread(files.read, workspaces, path, offset, limit))

    @server.tool(annotations=overwrite, structured_output=False, description=(
        "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically "
        "creates parent directories. Use write only for new files or complete rewrites. " + PATHS))
    async def write(
        path: Annotated[str, Field(description="Path to the file to write")],
        content: Annotated[str, Field(description="Content to write to the file")],
    ) -> CallToolResult:
        return await _guard(_in_thread(files.write, workspaces, path, content))

    @server.tool(annotations=mutate, structured_output=False, description=(
        "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, "
        "non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge "
        "them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to "
        "connect distant changes. When changing several separate places in one file, use one call with several "
        "entries in edits[]. " + PATHS))
    async def edit(
        path: Annotated[str, Field(description="Path to the file to edit")],
        edits: Annotated[list[Replacement], Field(description=(
            "One or more targeted replacements. Each edit is matched against the original file, not "
            "incrementally. Do not include overlapping or nested edits. If two changes touch the same block or "
            "nearby lines, merge them into one edit instead."))],
    ) -> CallToolResult:
        replacements = [item.model_dump() for item in edits]
        return await _guard(_in_thread(edit_tool.edit, workspaces, path, replacements))

    @server.tool(annotations=execute, structured_output=False, description=(
        "Execute a bash command in a workspace and return its stdout and stderr. Output is truncated to the last "
        "2000 lines or 50KB (whichever is hit first); if truncated, the full output is saved to a file you can "
        f"read. A command still running after {shell.YIELD_SECONDS:g} seconds keeps running in the background "
        "as a job: use bash_job to wait for it or stop it. stdin is closed, so commands must not prompt. "
        "Optionally provide a timeout in seconds."))
    async def bash(
        command: Annotated[str, Field(description="Bash command to execute")],
        cwd: Annotated[str | None, Field(description="Working directory (default: the default workspace root). "
                                                     + PATHS)] = None,
        timeout: Annotated[float | None, Field(description="Timeout in seconds (optional, no default timeout)")]
        = None,
    ) -> CallToolResult:
        return await _guard(lambda: shell.bash(workspaces, jobs, command, cwd, timeout))

    @server.tool(annotations=execute, structured_output=False, description=(
        "Check on a bash command that is still running as a background job: wait up to `wait` seconds for it to "
        f"finish (default and maximum {shell.YIELD_SECONDS:g}) and return the output produced since the last "
        "check. Pass kill=true to stop it."))
    async def bash_job(
        job: Annotated[int, Field(description="Job number reported by bash")],
        wait: Annotated[float | None, Field(description="Seconds to wait for the job to finish")] = None,
        kill: Annotated[bool, Field(description="Stop the job instead of waiting")] = False,
    ) -> CallToolResult:
        return await _guard(lambda: shell.bash_job(jobs, job, wait, kill))

    @server.tool(annotations=read_only, structured_output=False, description=(
        "Search file contents for a pattern. Returns matching lines with file paths and line numbers. Respects "
        ".gitignore. Output is truncated to 100 matches or 50KB (whichever is hit first). Long lines are "
        "truncated to 500 chars. " + PATHS))
    async def grep(
        pattern: Annotated[str, Field(description="Search pattern (regex or literal string)")],
        path: Annotated[str | None, Field(description="Directory or file to search (default: the default "
                                                      "workspace root)")] = None,
        glob: Annotated[str | None, Field(description="Filter files by glob pattern, e.g. '*.ts' or "
                                                      "'**/*.spec.ts'")] = None,
        ignoreCase: Annotated[bool, Field(description="Case-insensitive search (default: false)")] = False,
        literal: Annotated[bool, Field(description="Treat pattern as literal string instead of regex "
                                                   "(default: false)")] = False,
        context: Annotated[int | None, Field(description="Number of lines to show before and after each match "
                                                         "(default: 0)")] = None,
        limit: Annotated[int | None, Field(description="Maximum number of matches to return (default: 100)")]
        = None,
    ) -> CallToolResult:
        return await _guard(_in_thread(search.grep, workspaces, pattern, path, glob, ignoreCase, literal,
                                       context, limit))

    @server.tool(annotations=read_only, structured_output=False, description=(
        "Search for files by glob pattern. Returns matching file paths. Respects .gitignore. Output is truncated "
        "to 1000 results or 50KB (whichever is hit first). " + PATHS))
    async def find(
        pattern: Annotated[str, Field(description="Glob pattern to match files, e.g. '*.ts', '**/*.json', or "
                                                  "'src/**/*.spec.ts'")],
        path: Annotated[str | None, Field(description="Directory to search in (default: the default workspace "
                                                      "root)")] = None,
        limit: Annotated[int | None, Field(description="Maximum number of results (default: 1000)")] = None,
    ) -> CallToolResult:
        return await _guard(_in_thread(search.find, workspaces, pattern, path, limit))

    @server.tool(annotations=read_only, structured_output=False, description=(
        "List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. "
        "Includes dotfiles. Output is truncated to 500 entries or 50KB (whichever is hit first). " + PATHS))
    async def ls(
        path: Annotated[str | None, Field(description="Directory to list (default: the default workspace root)")]
        = None,
        limit: Annotated[int | None, Field(description="Maximum number of entries to return (default: 500)")]
        = None,
    ) -> CallToolResult:
        return await _guard(_in_thread(files.ls, workspaces, path, limit))

    return server, jobs


def serve(registry: Path, output_dir: Path) -> None:
    """Run over stdio until the client closes the connection, then stop running jobs."""
    server, jobs = create_server(registry, output_dir)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.run(transport="stdio")
    finally:
        jobs.shutdown()
