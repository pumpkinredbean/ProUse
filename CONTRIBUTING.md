# Contributing

Contributions are welcome. Keep changes focused, testable, and free of machine-specific
state or credentials.

ProUse follows the tool contracts of the [pi](https://github.com/earendil-works/pi)
coding agent. When changing a tool, check what pi does first and keep the names,
parameters, limits and notices compatible unless an MCP client needs something else;
say why in the pull request when it does.

See [CLI design](docs/cli-design.md) for the command hierarchy, lifecycle contract,
module responsibilities, and the reference projects behind the design. Add command
adapters under `prouse/commands/`; keep operation logic in configuration, runtime,
or integration modules rather than growing the entry point. Tool logic lives in
`prouse/tools/` and does not import MCP; `prouse/server.py` wires it up.

## Development setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install [ripgrep](https://github.com/BurntSushi/ripgrep) to run the search tests
against it; they also run against the built-in fallback.

## Before opening a pull request

Run the public validation suite:

```bash
python -m compileall -q prouse tests
python -m unittest discover -s tests -p 'test_*.py'
python scripts/release_check.py
```

When changing MCP tool behavior, add or update tests covering the public contract in
`tests/`. When changing the registry format, update `docs/configuration.md` and
`examples/workspace-registry.example.json`, and keep older registries readable.

Do not commit `PROUSE_HOME` contents, credentials, private keys, or output containing
data from a private workspace.
