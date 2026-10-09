# CLI design

ProUse is an installed command with a repeatable setup step, a stdio MCP server that
the MCP client starts, and an optional dashboard process per `PROUSE_HOME`. The README
and agent guide use the same commands.

## Command model

```text
prouse
  setup                 register a project; preserve existing configuration
  workspace
    list                registered workspaces and whether each is usable
    add                 register another project folder
    default             choose the workspace relative paths resolve against
    remove              unregister a workspace; its files are not touched
  mcp
    config              JSON configuration for the current installation
    check               real initialization, tool discovery, workspace read
    serve               stdio server whose lifecycle belongs to the client
  start                 start the dashboard detached, verify readiness, return
  run                   dashboard in the foreground for a terminal or service manager
  restart               stop the owned dashboard, then start and verify readiness
  stop                  stop only this instance's dashboard
  status                report dashboard readiness and MCP client details
  logs                  show recent dashboard output; -f also follows new output
  doctor                diagnose installation and configuration
  service
    install             opt in to starting the dashboard at login
    status              inspect the OS service
    uninstall           disable and remove it
```

The MCP server needs only `setup` and `mcp config`: the client starts `mcp serve`
itself, and the server reads the registry on each tool call. The dashboard is an
optional local page for the same workspace management.

`prouse` with no command displays help and performs no setup. Human output gives
the result and next action. `--json` emits structured results and errors, including
argument-validation failures. It works before the command or on a result-producing
subcommand. `logs` is a text stream; `mcp serve` owns stdout for the MCP protocol.

Exit codes: `0` success, `1` operation failure, `2` invalid usage, `3` stopped or
not ready, and `130` interrupted. `doctor` preserves warnings independently of
fatal errors. Dashboard readiness never implies an MCP client's connection.

`start` and `restart` return after the owned child passes its HTTP health check.
They serialize lifecycle changes with a per-instance lock. A separate lock is
held for the foreground server's entire lifetime. Process receipts include the
instance home and a process fingerprint; `stop` does not select a process by name
or port. Startup failure terminates only the child created by that invocation.

Existing configuration paths and process receipts are retained. The old `admin`
verb and `--background` flag remain compatibility inputs, but onboarding and help
teach `start` and `run`. Service definitions execute `run` so the OS manager owns
the foreground process.

## Source hierarchy

```text
prouse/
  cli.py                  entry point, composition, argument/error boundary
  command.py              CLI context, shared flags, human/JSON rendering
  commands/               setup, workspace, lifecycle, diagnostics, mcp, service adapters
  configuration.py        registry changes, dashboard settings, repeatable setup
  workspaces.py           registry reading, path resolution, project instructions
  server.py               MCP server and tool definitions
  tools/                  read/write/ls, edit, grep/find, bash, output truncation
  state.py                instance paths, atomic JSON, file locks
  errors.py               actionable errors and public exit codes
  diagnostics.py          installation and capability checks
  runtime/
    admin.py              start/run/stop/restart and readiness
    dashboard.py          dashboard HTTP API and request protections
    process.py            process identity and ownership receipts
    network.py            listener URLs, allowed hosts, health probes
    logs.py               bounded tail and foreground log capture
  integrations/
    mcp.py                stdio serving, client config, handshake probe
    services.py           launchd/systemd definitions and actions
prouse_assets/admin_ui/   dashboard page
```

Commands translate CLI arguments into explicit operations and render their results.
Configuration, runtime, and integration modules do not import `argparse`, print
user-facing results, or depend on command handlers. The foreground runtime accepts
an `on_ready` callback. Paths and output streams are supplied through the command
context; operation code does not rediscover the selected home during an invocation.
Heavy server/MCP imports occur when those operations run, keeping help independent
of runtime initialization. The tools in `prouse/tools/` take a `Workspaces` object and
return plain results; `server.py` is the only place that knows about MCP.

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

Regression coverage exercises public behavior: the tools' contracts (ported from
pi's tests where they exist), workspace scoping, repeat setup, concurrent startup,
background survival, foreground ownership, restart, invalid JSON arguments,
stale/foreign receipts, startup failure, service definitions, the dashboard API and
its request protections, and real MCP calls over stdio. CI also installs the wheel
outside the checkout before checking its lifecycle.
