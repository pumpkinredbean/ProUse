# CLI design

ProUse is an installed command with a repeatable setup step and one owned local
Admin process per `PROUSE_HOME`. The README and agent guide use the same commands.

## Command model

```text
prouse
  setup                 register a project; preserve existing configuration
  start                 detach, verify readiness, return
  run                   foreground process for a terminal or service manager
  restart               stop the owned process, then start and verify readiness
  stop                  stop only this instance
  status                report local readiness
  logs                  show recent output; -f also follows new output
  doctor                diagnose installation and configuration
  mcp
    config              JSON configuration for the current installation
    check               real initialization, tool discovery, workspace read
    serve               stdio server whose lifecycle belongs to the client
  service
    install             opt in to login autostart
    status              inspect the OS service
    uninstall           disable and remove it
```

`prouse` with no command displays help and performs no setup. Human output gives
the result and next action. `--json` emits structured results and errors, including
argument-validation failures. It works before the command or on a result-producing
subcommand. `logs` is a text stream; `mcp serve` owns stdout for the MCP protocol.

Exit codes: `0` success, `1` operation failure, `2` invalid usage, `3` stopped or
not ready, and `130` interrupted. `doctor` preserves warnings independently of
fatal errors. Admin readiness never implies an external client's authorization.

`start` and `restart` return after the owned child passes its HTTP health check.
They serialize lifecycle changes with a per-instance lock. A separate lock is
held for the foreground server's entire lifetime. Process receipts include the
instance home and a process fingerprint; `stop` does not select a process by name
or port. Startup failure terminates only the child created by that invocation.

Existing configuration paths and process receipts are retained. The old `admin`
verb and `--background` flag remain compatibility inputs, but onboarding and help
teach `start` and `run`. Service definitions execute `run` so the OS manager owns
the foreground process. Existing services should be uninstalled and installed
again when upgrading from the old foreground `start` behavior.

## Source hierarchy

```text
prouse/
  cli.py                  entry point, composition, argument/error boundary
  command.py              CLI context, shared flags, human/JSON rendering
  commands/               setup, lifecycle, diagnostics, mcp, service adapters
  configuration.py        validated settings and repeatable workspace setup
  state.py                instance paths, atomic JSON, file locks
  errors.py               actionable errors and public exit codes
  diagnostics.py          installation and capability checks
  runtime/
    admin.py              start/run/stop/restart and readiness
    process.py            process identity and ownership receipts
    network.py            listener URLs, allowed hosts, health probes
    logs.py               bounded tail and foreground log capture
  integrations/
    mcp.py                stdio serving, client config, handshake probe
    services.py           launchd/systemd definitions and actions
```

Commands translate CLI arguments into explicit operations and render their results.
Configuration, runtime, and integration modules do not import `argparse`, print
user-facing results, or depend on command handlers. The foreground runtime accepts
an `on_ready` callback. Paths and output streams are supplied through the command
context; operation code does not rediscover the selected home during an invocation.
Heavy server/MCP imports occur when those operations run, keeping help independent
of runtime initialization. Existing `scripts/` modules remain the workspace/MCP
backend; this change does not duplicate or relocate that backend.

## Reference projects and adaptations

- [Caddy's command line](https://caddyserver.com/docs/command-line) separates
  background `start`, foreground `run`, and `stop`. ProUse adopts that lifecycle
  vocabulary and adds an explicit readiness result for one-shot agent installers.
- [GitHub CLI's project layout](https://github.com/cli/cli/blob/trunk/docs/project-layout.md)
  and [command development guide](https://github.com/cli/cli/blob/trunk/docs/command-development.md)
  distinguish command wiring, options, and execution, with shared dependencies
  provided to commands. ProUse uses small Python command families and a context
  object, without adding a framework or a plugin system.
- [Supabase's local development flow](https://supabase.com/docs/guides/local-development/cli/getting-started)
  makes initialization, startup, status, and shutdown explicit CLI operations.
  ProUse keeps these daily actions at the top level and groups MCP handoff and
  optional OS service management beneath their own nouns.

Regression coverage exercises public behavior: repeat setup, concurrent startup,
background survival, foreground ownership, restart, invalid JSON arguments,
stale/foreign receipts, startup failure, service definitions, and real MCP reads.
CI also installs the wheel outside the checkout before checking its lifecycle.
