# Roadmap

Deferred work that is deliberately not part of the current release. Nothing here carries a date;
each item states the concrete gap and the decision it needs.

## Localize the Admin UI

The operator interface is Korean-only: `scripts/admin_ui/app.js` carries roughly 2,700 Korean
characters of labels, buttons and notices, and `scripts/admin_server.py` returns Korean error
messages. The skill, README and `docs/` are English, so a non-Korean operator can install ProUse
but cannot read its Admin UI.

Decision needed: translate the strings to English, or add a locale layer so the UI ships both
languages. Deferred on 2026-09-22.

## Keep the release tree and the private tree from drifting

The public tree is produced by stripping internal files from the private working tree, so the two
layouts diverge: the public tree keeps `docs/` and `examples/`, while the private tree keeps
`references/` and the `SKILL.md` links into it. Every change has to be applied twice, and the
schema-artifact test had to accept both layouts.

Decision needed: generate the public tree from the private one with a script, or maintain the
public tree as a branch that is merged deliberately.
