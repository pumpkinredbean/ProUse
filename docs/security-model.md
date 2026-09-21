# Security model

ProUse should be treated like privileged local developer tooling. Its security model is
primarily about making authority explicit and keeping source access bounded and auditable.

## Workspace boundary

Only operator-registered workspace roots are addressable through MCP. Context operations
accept stable workspace IDs, not arbitrary absolute roots.

A per-workspace policy further limits readable and writable source paths. The context
layer validates relative paths, refuses traversal, opens filesystem entries without
following symlinks, and only exposes approved regular files. Built-in filters also reject
secret-like paths/content and private runtime state.

## Evidence and stale writes

Reads return SHA-256 evidence for exact file bytes when hashing is available. Mutation
tools accept `expected_sha256`; callers should use it when replacing or deleting an
existing file so unseen concurrent changes fail closed.

Workspace-level view IDs provide freshness context for paginated/discovery operations,
while per-file hashes remain the exact content identity.

## Execution boundary

Direct execution is deliberately more powerful than the context reader. Commands run in
the operator's host environment and may inherit normal PATH, HOME, network access, and OS
permissions. Treat access to `exec_command` as access to privileged local execution.

Delegated workers are a separate path with explicit worker profiles and provenance. A
failure in one path is not silently converted into the other.

## Admin surface

The Admin UI binds to `127.0.0.1` by default and is separate from the MCP tool surface.
Do not expose it directly to the public Internet. If remote access is necessary, place it
behind authentication and a network boundary you control.

## Source-control hygiene

Never commit:

- `.state/` runtime data or live registry/policy files;
- credentials, tokens, private keys, tunnel profiles, or environment files containing secrets;
- execution receipts or generated artifacts containing private project data.

Run `python scripts/release_check.py` before publishing a release candidate.

## Vulnerability reports

Use GitHub private vulnerability reporting for the repository when reporting a security
issue that may contain exploit details or secrets. Do not paste credentials into public
issues.
