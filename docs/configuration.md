# Configuration

For normal installations, let the CLI create and validate the registry without erasing
existing entries:

```bash
prouse setup --workspace "/absolute/path/to/project" --no-input --json
```

The default registry is `~/.prouse/config/workspace-registry.json`, runtime state stays
under `~/.prouse/.state/`, and `PROUSE_HOME` relocates the whole instance. The lower-level
schema details below remain useful for Admin-managed advanced configuration.

ProUse separates **which projects exist** from **which files are exposed**.
The workspace registry is private operator configuration; each workspace can point to a
separate context policy.

## Workspace registry

Start from `examples/workspace-registry.example.json`. The JSON Schema is available at
`examples/workspace-registry.schema.json`.

Important fields:

- `server_name`: display name used by the MCP server.
- `state_dir`: private runtime state directory. Keep it below a `.state` directory.
- `default_worker_profile`: fallback worker profile ID.
- `model_capabilities`: explicit model-to-reasoning-effort allowlist.
- `worker_profiles`: named worker configurations exposed to orchestration.
- `workspaces`: approved project roots.
- `codex_bin`: optional explicit Codex executable path.

Each workspace entry contains a stable `id`, human-readable `label`, absolute or
registry-relative `root`, `enabled`, optional `default_worker_profile`, and optional
`context_policy`.

Relative configured paths are resolved from the directory containing the registry file.
Environment-variable and `~` expansion are intentionally not performed.

## Context policy

A version 1 context policy has a name plus explicit files and/or approved directories.
For example:

```json
{
  "version": 1,
  "name": "application-source",
  "files": [
    "README.md",
    "pyproject.toml"
  ],
  "directories": [
    {
      "path": "src",
      "extensions": [".py", ".md", ".json"]
    }
  ]
}
```

`path: "."` selects the workspace root. Directory rules are still constrained by the
built-in allowed extension/filename set; a policy cannot turn the context layer into an
arbitrary filesystem reader.

The same policy bounds `read_files`, search/glob operations, and file mutation tools.
A path that cannot be read under the policy cannot be edited through the context tools.

## Private state

Do not commit live registry files, live context policies, execution receipts, credentials,
or generated runtime state. The default `.gitignore` excludes `.state/`, virtualenvs,
credential-like file types, logs, build output, and common IDE/test artifacts.

## Validation

Validate before starting the MCP server:

```bash
python scripts/context_server.py \
  --registry .state/workspace-registry.json \
  --check-config
```

To inspect the runtime registry schema:

```bash
python scripts/context_server.py --print-schema
```
