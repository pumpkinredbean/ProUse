# MCP tools

ProUse exposes the tool set of the [pi](https://github.com/earendil-works/pi) coding
agent over MCP, with the same names, parameters and output limits, plus two tools
for working inside a chat app: `workspaces` and `bash_job`. Every result is plain
text (images are attached as MCP image content), so it reaches the model in any MCP
client. Failures are returned as tool errors whose text says what to do next.

## Paths

A path is absolute, starts with `~`, or is relative to the default workspace. A
leading `@` is ignored. The file tools (`read`, `write`, `edit`, `grep`, `find`, `ls`)
resolve the path, follow symlinks, and refuse anything that does not end up inside a
registered workspace. `read` can also open the saved output of a truncated `bash` run.
When several workspaces are registered and none is the default, relative paths are
refused; use absolute paths. Results show paths the same way: relative to the default
workspace when inside it, absolute otherwise, so they can be passed back as is.

The registry is re-read when it changes, so workspaces added or removed with the CLI
or the dashboard apply on the next tool call.

## Output limits

Text results are capped at 2000 lines or 50KB, whichever comes first. `read`, `grep`,
`find` and `ls` keep the beginning and say how to continue; `bash` keeps the end, where
errors and test summaries are, and saves the full output to a file the model can
`read`. A truncation notice always ends the result in square brackets.

## workspaces

| Parameter | Type | Meaning |
| --- | --- | --- |
| `name` | string, optional | Workspace id, label, or any path inside a workspace |

Lists the registered workspaces with their roots and marks the default and any that are
unavailable (folder missing, or the home folder or filesystem root). With `name`, or
when only one workspace is registered, it also returns that project's instructions:
the `AGENTS.override.md`, `AGENTS.md` or `CLAUDE.md` in the workspace root and in each
parent folder, outermost first, the same files pi and other coding agents load. The
server instructions tell the model to call it first.

## read

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` | string | File to read |
| `offset` | integer, optional | First line to return, 1-based |
| `limit` | integer, optional | Maximum number of lines |

Returns the file's text as is, without line numbers. A truncated or limited read ends
with `[Showing lines A-B of N. Use offset=B+1 to continue.]` or
`[K more lines in file. Use offset=… to continue.]`. A single line over 50KB returns a
`sed | head -c` command instead. JPEG, PNG, GIF and WebP files are attached as images;
an image over 700KB is shrunk with `sips` on macOS, or refused with a suggestion
elsewhere. Other binary files and text files over 32MB are refused with a `bash`
alternative.

## write

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` | string | File to create or overwrite |
| `content` | string | Complete new content |

Creates parent folders as needed and writes the content exactly, without newline
conversion. Returns whether the file was created or overwritten, and its size.

## edit

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` | string | File to edit |
| `edits` | array of `{oldText, newText}` | One or more replacements |

Every `oldText` is matched against the original file, not against the result of the
previous edit, and must match exactly one place. Edits must not overlap. When the exact
text is not found, matching is retried after normalizing Unicode (NFKC), trailing
whitespace, curly quotes, dashes and special spaces; only the lines an edit touches are
rewritten, and every other byte is kept. CRLF line endings and a UTF-8 byte order mark
are preserved. Either all edits apply or none do. Errors name the failing edit: not
found, found several times, overlapping, or no change. Writes to the same file are
serialized.

## bash

| Parameter | Type | Meaning |
| --- | --- | --- |
| `command` | string | Command for `bash -c` |
| `cwd` | string, optional | Working directory inside a workspace (default: the default workspace root) |
| `timeout` | number, optional | Seconds before the command is stopped (no default) |

Runs the command with stdout and stderr combined, stdin closed, `GIT_EDITOR=true`, and
the user's login-shell environment, so tools installed through Homebrew, nvm, pyenv
and similar are on `PATH`. The command runs in its own process group; a timeout stops
the whole group.

A command that finishes within 45 seconds returns its output and, if it failed, its exit
code as an error. A command still running then keeps running as a background job: the
result is the output so far followed by
`[Still running after 45s as job N. Call bash_job with job=N …]`. This keeps every call
inside the roughly 60 second limit chat apps such as Claude Desktop put on a tool call.
`PROUSE_BASH_YIELD_SECONDS` changes the 45 seconds.

`cwd` must be inside a workspace, but the command itself is not confined. See the
[security model](security-model.md).

## bash_job

| Parameter | Type | Meaning |
| --- | --- | --- |
| `job` | integer | Job number from `bash` |
| `wait` | number, optional | Seconds to wait for the job to finish (default and maximum 45) |
| `kill` | boolean, optional | Stop the job instead of waiting |

Returns the output produced since the last check, then either the same "still running"
notice or `[Job N finished after Ts …]` with the exit status. Jobs belong to the MCP
server process: they stop when the client closes the connection.

## grep

| Parameter | Type | Meaning |
| --- | --- | --- |
| `pattern` | string | Regular expression, or literal text with `literal` |
| `path` | string, optional | Folder or file to search (default: the default workspace root) |
| `glob` | string, optional | Only files matching this glob, e.g. `*.ts` or `**/*.spec.ts` |
| `ignoreCase` | boolean, optional | Case-insensitive search |
| `literal` | boolean, optional | Treat the pattern as plain text |
| `context` | integer, optional | Lines of context before and after each match |
| `limit` | integer, optional | Maximum matches (default 100) |

Returns `path:line: text` for matches and `path-line- text` for context lines. Uses
ripgrep when it is installed, respecting `.gitignore` and including hidden files except
`.git`. Without ripgrep it falls back to `git ls-files` or a directory walk with Python
regular expressions. Lines are cut at 500 characters.

## find

| Parameter | Type | Meaning |
| --- | --- | --- |
| `pattern` | string | Glob, e.g. `*.ts`, `**/*.json` or `src/**/*.spec.ts` |
| `path` | string, optional | Folder to search (default: the default workspace root) |
| `limit` | integer, optional | Maximum results (default 1000) |

Returns matching file paths, sorted. A pattern without `/` matches file names at any
depth; a pattern with `/` is matched at any depth below the searched folder, as in pi.
Respects `.gitignore` like `grep`.

## ls

| Parameter | Type | Meaning |
| --- | --- | --- |
| `path` | string, optional | Folder to list (default: the default workspace root) |
| `limit` | integer, optional | Maximum entries (default 500) |

Returns entries sorted case-insensitively, with `/` after folders, including dotfiles.
