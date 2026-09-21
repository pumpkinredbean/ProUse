# Contributing

Contributions are welcome. Keep changes focused, testable, and free of machine-specific
state or credentials.

## Development setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Before opening a pull request

Run the public validation suite:

```bash
python -m compileall -q prouse scripts
python -m unittest discover -s scripts -p 'test_*.py'
python scripts/release_check.py
```

When changing the workspace registry contract, update
`examples/workspace-registry.schema.json` and its regression test. When changing MCP tool
behavior, add or update tests covering the public contract.

Do not commit `.state/`, live context policies, receipts, credentials, private keys, or
artifacts containing data from a private workspace.
