from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = [
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".gitignore",
    "requirements.txt",
    "pyproject.toml",
    "install.sh",
    ".github/workflows/ci.yml",
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/context_server.py",
    "scripts/context_store.py",
    "scripts/admin_server.py",
    "scripts/advisor_wait.py",
    "CHANGELOG.md",
    "docs/configuration.md",
    "docs/mcp-tools.md",
    "docs/direct-execution.md",
    "docs/security-model.md",
    "docs/install-for-agents.md",
    "examples/workspace-registry.example.json",
    "examples/workspace-registry.schema.json",
    "examples/context-policy.example.json",
    "scripts/check_worker_sandbox.py",
    "scripts/test_advisor_wait.py",
    "scripts/test_cli.py",
    "prouse/cli.py",
    "prouse_assets/admin_ui/index.html",
]

# Live runtime state and credentials never belong in the public tree. The advisor skill
# (SKILL.md and agents/) is part of the product and is required above.
FORBIDDEN_PUBLIC_PATHS = [
    "PUBLISHING.md",
    ".state",
]

PRIVATE_PATTERNS = {
    "local user path": re.compile(r"/Users/[^/\s]+"),
    "private IPv4": re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "legacy Whaleturn brand": re.compile(r"\bwhaleturn\b", re.I),
    "obvious OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
}

EXCLUDED = {Path("scripts/release_check.py")}


def fail(message: str) -> None:
    print("FAIL: " + message)
    raise SystemExit(1)


def main() -> None:
    missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
    if missing:
        fail("missing required release files: " + ", ".join(missing))

    present_forbidden = [rel for rel in FORBIDDEN_PUBLIC_PATHS if (ROOT / rel).exists()]
    if present_forbidden:
        fail("private runtime paths are present: " + ", ".join(present_forbidden))

    readme = (ROOT / "README.md").read_text()
    tagline = "Use Pro models across your local workspaces."
    if "# ProUse" not in readme:
        fail("README title is not ProUse")
    if tagline not in readme:
        fail("README is missing the canonical tagline")

    skill = (ROOT / "SKILL.md").read_text()
    if "name: pro-advisor" not in skill:
        fail("compatibility skill ID pro-advisor was changed unexpectedly")

    admin = (ROOT / "scripts/admin_server.py").read_text()
    if "default='127.0.0.1'" not in admin and 'default="127.0.0.1"' not in admin:
        fail("Admin server is not localhost-only by default")

    packaging = (ROOT / "pyproject.toml").read_text()
    if 'prouse = "prouse.cli:main"' not in packaging:
        fail("installed prouse console entry point is missing")

    agent_install = (ROOT / "docs/install-for-agents.md").read_text()
    for command in ("prouse setup", "prouse start", "prouse status --json", "prouse mcp serve"):
        if command not in agent_install:
            fail("agent installation document is missing " + command)

    registry = json.loads((ROOT / "examples/workspace-registry.example.json").read_text())
    if registry.get("server_name") != "ProUse":
        fail("registry example server_name is not ProUse")

    policy = json.loads((ROOT / "examples/context-policy.example.json").read_text())
    if policy.get("name") != "example-context":
        fail("context policy example is not generic")

    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if rel in EXCLUDED:
            continue
        if any(part in {".git", ".venv", "__pycache__", ".state"} for part in rel.parts):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for label, pattern in PRIVATE_PATTERNS.items():
            if pattern.search(text):
                hits.append(f"{rel}: {label}")

    if hits:
        fail("public-tree privacy scan found:\n  " + "\n  ".join(sorted(set(hits))))

    print("ProUse release check: OK")
    print("Canonical tagline: " + tagline)
    print("Public tree privacy scan: clean")
    print("Admin default bind: 127.0.0.1")
    print("Compatibility skill ID: pro-advisor")


if __name__ == "__main__":
    main()
