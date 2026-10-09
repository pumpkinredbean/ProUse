# Security

ProUse gives the model in a chat app file access to your registered project folders
**and an unsandboxed `bash` tool that runs commands as you**. Treat access to a ProUse
MCP server like access to your terminal.

## Supported release

Security fixes are applied to the latest release line. Until the first stable release,
users should run the newest tagged version or an explicitly reviewed commit.

## Defaults

- The MCP server speaks stdio only and opens no network port.
- The file tools only reach registered workspaces, after resolving symlinks. Your home
  folder and the filesystem root cannot be registered.
- Every tool carries MCP annotations so the client can ask for approval before
  destructive or open-world calls.
- The optional dashboard binds to `127.0.0.1`, checks the `Host` header, and accepts
  changes only from its own page with a per-process token.

## Your responsibilities

- Register project folders only, and only ones whose contents you are willing to send to
  your chat app's model provider.
- Review `bash` commands before approving them, especially in repositories you do not
  trust: file contents and command output can carry prompt injections.
- Do not bridge the MCP server to a network transport or tunnel, and do not expose the
  dashboard beyond a network you trust.
- Keep `PROUSE_HOME`, credentials and command output out of source control.

See the [security model](docs/security-model.md) for the details.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting for this repository when available.
Do not open a public issue containing credentials, exploit details, or private workspace data.
