# Changelog

## Unreleased

### Changed

- Replace the MCP tools with the coding tool set of the pi agent: `read`, `write`,
  `edit`, `bash`, `grep`, `find` and `ls`, plus `workspaces` (registered folders and
  `AGENTS.md` / `CLAUDE.md` project instructions) and `bash_job`. Results are plain
  text truncated to 2000 lines or 50KB with continuation notices, so every MCP client
  passes them to the model.
- `bash` runs commands directly with the user's login-shell environment. A command still
  running after 45 seconds continues as a background job, which keeps each tool call
  inside chat apps' time limits.
- Tools take absolute, `~`-based or default-workspace-relative paths instead of
  workspace ids and policy-relative paths.
- The registry is version 2 and holds only workspaces. Version 1 registries are still
  read and are rewritten the next time a workspace changes.
- The Admin UI is now a small dashboard for managing workspaces and copying the MCP
  client configuration; `prouse start` is optional.
- Add `prouse workspace list|add|default|remove`.

### Removed

- Codex worker delegation, direct `argv` execution with receipts and diffs, context
  policies, secret-path filtering, SHA-256 write preconditions, `delete_file`, and the
  `pro-advisor` skill. Codex is no longer required.

### Earlier unreleased changes

- Return each MCP tool result as JSON text alongside `structuredContent`, so clients
  that only pass text content to the model (such as Claude Desktop) can read results.
- Make `prouse start` and `prouse restart` start in the background by default;
  use `prouse run` for foreground execution and service managers.
- Separate CLI command families, configuration, runtime lifecycle, MCP, and
  service-manager integrations. Validate machine-readable errors consistently
  and serialize concurrent lifecycle operations.
- Bootstrap missing uv/Python prerequisites, refresh same-version installations,
  and provide copyable agent prompts with official installation instructions.
- Add `prouse mcp config` with the selected instance environment and `prouse mcp check`
  for a real handshake, tool discovery, and policy-scoped workspace read.
- Add the installable `prouse` command, repeatable `uv tool` bootstrap, preserved
  first-run setup, foreground Admin lifecycle, diagnostics/logs, and isolated
  launchd/systemd-user service definitions.
- Add client-owned `prouse mcp serve`, packaged Admin/configuration assets, explicit
  local/MCP/client readiness, agent installation guidance, and CLI/package regression
  coverage.

All notable public changes to ProUse are documented here.

## 0.1.0 - 2026-09-21

Initial open-source release candidate.

### Added

- Multi-workspace MCP registry with explicit worker profiles.
- Policy-scoped listing, globbing, literal/regex search, line reads, byte-window reads,
  and change detection.
- Policy-scoped `write_file`, `edit_file`, and `delete_file` with optimistic SHA-256 checks.
- Independent Codex worker delegation and direct host command execution.
- Durable task/execution receipts, diffs, and artifact reads.
- Local Admin UI and public configuration/schema examples.
- Public regression suite and release/privacy validation.
