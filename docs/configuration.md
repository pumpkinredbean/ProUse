# Configuration

ProUse keeps everything for one instance under `PROUSE_HOME`, which defaults to
`~/.prouse`. Reinstalling or updating ProUse does not touch it. Set `PROUSE_HOME` to
another absolute directory for a separate instance, such as a test setup; the MCP
client configuration printed by `prouse mcp config` passes it to the server.

```text
~/.prouse/
  config/workspace-registry.json   registered workspaces
  config/settings.json             dashboard host and port (optional)
  output/                          full output of truncated bash runs, kept for 3 days
  run/                             dashboard process receipt and locks
  logs/admin.log                   dashboard log
```

## Workspaces

Use the CLI or the dashboard rather than editing the registry by hand:

```bash
prouse setup --workspace /path/to/project      # first run; repeatable
prouse workspace add /path/to/other --label "Other project"
prouse workspace default other
prouse workspace list --json
prouse workspace remove other
```

The first workspace becomes the default. `--default` on `setup` or `workspace add`
makes a new one the default. A workspace id is derived from the folder name unless
`--workspace-id` (`setup`) or `--id` (`workspace add`) gives one; ids match
`[a-z][a-z0-9_-]{0,63}`. Adding a folder that is already registered keeps its id and
re-enables it. Your home folder and the filesystem root cannot be registered.

The registry format is version 2:

```json
{
  "version": 2,
  "default_workspace_id": "app",
  "workspaces": [
    {"id": "app", "label": "My app", "root": "/srv/projects/app", "enabled": true}
  ]
}
```

- `root` is absolute, starts with `~`, or is relative to the `config` folder.
- `enabled: false`, set from the dashboard, keeps an entry but hides it from the tools.
- `default_workspace_id` is the workspace relative paths resolve against. When it is
  missing and exactly one workspace is enabled, that one is the default.

A workspace whose folder no longer exists stays registered and is reported as
unavailable by `prouse workspace list`, `prouse doctor` and the `workspaces` tool.

The MCP server re-reads the registry when the file changes, so changes apply on the next
tool call without restarting the MCP client.

### Registries from ProUse 0.1

A version 1 registry keeps working. Its worker profiles, model settings and context
policy references are ignored, because the tools no longer use Codex or per-workspace
file policies. The next change through the CLI or the dashboard rewrites it as version 2
with only the workspaces. Old `.state/` folders and context policy files are no longer
read and can be deleted.

## Dashboard settings

`config/settings.json` holds where the dashboard listens:

```json
{"host": "127.0.0.1", "port": 8848}
```

`prouse setup --host H --port P` saves them; `prouse start --host H --port P`
overrides them for one run. The MCP server does not use these settings.

## Environment variables

| Variable | Effect |
| --- | --- |
| `PROUSE_HOME` | Instance folder (default `~/.prouse`) |
| `PROUSE_BASH_YIELD_SECONDS` | Seconds a `bash` call waits before returning a running command as a job (default 45) |
| `PROUSE_SHELL_SNAPSHOT` | `0` skips reading the login shell's environment for `bash`, `grep` and `find` |
| `PROUSE_RG` | Path to the ripgrep executable; an unusable path selects the built-in search |

Set them in the `env` of the MCP client configuration for the MCP server, or in your
shell for CLI commands.

## Private state

Do not commit a live registry, the `output/` folder, logs, or anything else from
`PROUSE_HOME`. Bash output files can contain anything a command printed.
