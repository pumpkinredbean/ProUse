# Security model

ProUse is privileged local developer tooling. It gives the model in a chat app the same
kind of access a coding agent such as pi or Claude Code has in a terminal, and it makes
no attempt to be safer than that. This page says what is bounded and what is not.

## Who can use the tools

Only the MCP client that started `prouse mcp serve` can call the tools. The server
speaks MCP over that process's stdin and stdout and opens no network port. Each tool
carries MCP annotations (read-only, destructive, open-world), and the client decides
which calls need your approval. Claude Desktop asks before running a tool unless you
allow it.

Do not bridge the stdio server to a network transport or tunnel. Anyone who reaches it
can run commands as you.

## bash is not sandboxed

`bash` runs commands as your user, with your login-shell environment, network access,
credentials and file permissions. `cwd` must be inside a workspace, but the command can
reach anything you can. Treat allowing `bash` in a chat like typing what the model
suggests into your own terminal. A command runs in its own process group and is stopped
when it times out, when `bash_job` kills it, or when the MCP client disconnects.

## The file tools stay inside registered folders

`read`, `write`, `edit`, `grep`, `find` and `ls` resolve every path, follow symlinks,
and refuse a result outside the registered workspace roots. A symlink inside a
workspace that points outside it is refused. `read` may also open the full output files
of truncated `bash` runs in `PROUSE_HOME/output`. Your home folder and the filesystem
root cannot be registered, and are reported as unavailable if a registry names them.

Inside a workspace there are no further filters: `.env` files, keys and anything else
in the folder can be read and sent to the model provider. Register project folders
whose contents you are willing to share with your chat app.

## Concurrent changes

Writes to the same file are serialized within the server. There are no hash
preconditions: `edit` fails when its `oldText` is no longer in the file, which catches
most changes made in between, and `write` replaces whatever is there. Use version
control for anything you want to be able to undo.

## Prompt injection

File contents, command output and project instructions (`AGENTS.md`, `CLAUDE.md`) are
returned to the model as data, but a model can still follow instructions planted in
them. Review commands before approving them in repositories you do not trust, and do
not connect ProUse to a chat that also browses untrusted content unless you approve
each call.

## Dashboard

The optional dashboard (`prouse start`) manages the workspace list. It cannot read
files or run commands, but adding a workspace widens what the file tools can reach.

- It binds to `127.0.0.1:8848` by default and has no login.
- It answers only requests whose `Host` is one of its own addresses.
- A change must come from the same origin, as JSON, with the per-process token from
  `/api/session`, which blocks other websites from changing your workspaces.
- Its folder browser lists folders inside your home folder only.
- Responses carry a strict Content Security Policy, `X-Content-Type-Options: nosniff`,
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

Binding it to another address with `--host` lets anyone who can reach that address
change your workspace list. Do not expose it beyond a network you trust.

## Source control

Never commit `PROUSE_HOME` contents, credentials, private keys, environment files with
secrets, or bash output files. Run `python scripts/release_check.py` before publishing
a release; it scans the tree for local user paths, private addresses and key-like
strings.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not paste
credentials or exploit details into public issues.
