# Connecting private workspace context

ProUse can expose a deliberately bounded view of a local workspace to an MCP
client without copying the entire repository into every conversation.

## Boundary

The workspace registry is local operator configuration. It contains local roots
and is never returned by the public MCP tools. The MCP surface returns stable
workspace IDs, relative paths, hashes and bounded file content.

A context policy is an allowlist, not a convenience glob. Keep it narrow. A directory entry
selects the listed extensions plus well-known extension-less files such as `Dockerfile`,
`Makefile`, `Justfile`, `LICENSE` and `.gitignore`; use `.` as the path to select the workspace
root itself.

## Setup

1. Copy `workspace-registry.example.json` into your local `.state/` directory.
2. Register an explicit project root. Do not register `/` or your home
   directory.
3. Copy `context-policy.example.json` to a local policy file and list only the
   files/directories the model needs.
4. Start `context_server.py --registry <registry>`.
5. Connect that stdio server using the MCP mechanism supported by your client.

If a remote MCP endpoint is required, place an authenticated transport/tunnel
in front of the MCP process. Keep tunnel credentials and generated state
outside this repository.

## What a remote client can learn

The public context tools expose:

- workspace ID and label;
- approved relative paths, directory listings and glob matches;
- file hashes, bounded line ranges and exact byte windows (after whole-file safety/hash verification,
  with byte-window reads limited to files no larger than 32 MiB);
- literal and regex search hits with line numbers and optional context lines;
- deterministic view IDs used to reject stale or cross-workspace evidence.

The file tools can create, overwrite, exactly edit, or delete only paths allowed by that same
policy. Callers should pass the last observed SHA256 as `expected_sha256`; stale content is
rejected instead of silently overwritten. Fixed path, secret-content, symlink, hardlink, binary,
and size checks also apply to mutations and cannot be disabled by policy.

Directory listing and glob discovery cover only paths inside the approved policy plus the parent
directories needed to reach them; files outside the policy are never listed or read. They do not
return workspace roots or operator credentials.

## What context access does not authorize

Accessing workspace context or its file tools does not grant permission to commit, push, deploy, modify
credentials, change approvals, operate wallets, place trades or perform other
external side effects. Operational authority still comes from the user.

## Local state

Keep registry state, policies containing private project names, receipts,
runtime configuration and tunnel profiles under `.state/` or another ignored
operator-owned directory.
