# Changelog

## Unreleased

- Add `prouse admin` and `start/restart --background` with verified startup, a
  per-instance process lock, and usable agent-terminal lifecycle behavior.
- Bootstrap missing uv/Python prerequisites, refresh same-version installations,
  and provide copyable agent prompts with source/ref-aware installation instructions.
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
