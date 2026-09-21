# Security

ProUse provides scoped file access **and privileged local command execution**. Treat access
to a running ProUse instance similarly to access to a developer account on the host.

## Supported release

Security fixes are applied to the latest release line. Until the first stable release,
users should run the newest tagged version or an explicitly reviewed commit.

## Safe defaults

- The Admin UI binds to `127.0.0.1` by default.
- Workspaces must be explicitly registered.
- Context reads and writes are bounded by a per-workspace policy.
- The context layer rejects traversal, unsafe links/file types, private runtime state, and
  secret-like paths/content.
- Existing-file writes can be bound to an observed SHA-256 with `expected_sha256`.
- Direct execution and delegated workers are separate, explicit paths.
- Runtime state belongs below `.state/` and is ignored by Git.

## Operator responsibilities

- Do not register your home directory or filesystem root as a workspace.
- Do not expose the Admin UI directly to the public Internet.
- Protect any remote MCP transport with authentication and network controls you trust.
- Review direct commands before allowing high-impact external side effects.
- Keep credentials, live policies, receipts, and private project artifacts out of source control.

See [docs/security-model.md](docs/security-model.md) for the detailed boundary model.

## Reporting a vulnerability

Please use GitHub private vulnerability reporting for this repository when available.
Do not open a public issue containing credentials, exploit details, or private workspace data.
