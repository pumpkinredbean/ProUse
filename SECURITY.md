# Security

ProUse provides local workspace access and command execution.

- Admin binds to 127.0.0.1 by default.
- Register explicit project roots rather than broad filesystem locations.
- Keep .state/, credentials, tokens, private keys, receipts, and local policies out of source control.
- Treat the direct command path as privileged local shell access.
- Do not expose the Admin UI directly to the public Internet.
