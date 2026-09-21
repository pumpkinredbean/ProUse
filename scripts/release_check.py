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
    "CHANGELOG.md",
    ".gitignore",
    "requirements.txt",
    ".github/workflows/ci.yml",
    "examples/workspace-registry.example.json",
    "examples/workspace-registry.schema.json",
    "examples/context-policy.example.json",
    "docs/configuration.md",
    "docs/mcp-tools.md",
    "docs/direct-execution.md",
    "docs/security-model.md",
    "scripts/context_server.py",
    "scripts/context_store.py",
    "scripts/admin_server.py",
    "scripts/direct_execution.py",
    "scripts/codex_orchestrator.py",
    "scripts/test_context_store.py",
    "scripts/test_context_server.py",
]

FORBIDDEN_PUBLIC_PATHS = [
    "SKILL.md",
    "agents/openai.yaml",
    "references/private-context.md",
    "references/exchange-protocol.md",
]

PRIVATE_PATTERNS = {
    "local macOS user path": re.compile(r"/Users/[^/<\\s]+/"),
    "local Linux user path": re.compile(r"/home/[^/<\\s]+/"),
    "private IPv4": re.compile(r"\\b(?:10|192\\.168|172\\.(?:1[6-9]|2\\d|3[01]))\\.\\d{1,3}\\.\\d{1,3}\\b"),
    "internal Whaleturn brand": re.compile(r"\\bwhaleturn\\b", re.I),
    "internal plugin URL": re.compile(r"plugin://", re.I),
    "shared ChatGPT conversation URL": re.compile(r"chatgpt\\.com/share/", re.I),
    "internal advisor state": re.compile(r"ADVISOR_STATE\\.md|Secure MCP Tunnel|aside repl", re.I),
    "OpenAI-style secret": re.compile(r"\\bsk-[A-Za-z0-9_-]{16,}\\b"),
    "AWS-style access key": re.compile(r"\\bAKIA[0-9A-Z]{16}\\b"),
    "private key block": re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
}

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".state", "dist", "build", "artifacts"}


def fail(message: str) -> None:
    print("FAIL: " + message)
    raise SystemExit(1)


def main() -> None:
    missing = [rel for rel in REQUIRED if not (ROOT / rel).is_file()]
    if missing:
        fail("missing required release files: " + ", ".join(missing))

    present_forbidden = [rel for rel in FORBIDDEN_PUBLIC_PATHS if (ROOT / rel).exists()]
    if present_forbidden:
        fail("internal-only paths are present: " + ", ".join(present_forbidden))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tagline = "Use Pro models across your local workspaces."
    if readme.splitlines()[0].strip() != "# ProUse":
        fail("README title is not ProUse")
    if tagline not in readme:
        fail("README is missing the canonical tagline")

    admin = (ROOT / "scripts/admin_server.py").read_text(encoding="utf-8")
    if "default='127.0.0.1'" not in admin and 'default="127.0.0.1"' not in admin:
        fail("Admin server is not localhost-only by default")

    registry = json.loads((ROOT / "examples/workspace-registry.example.json").read_text(encoding="utf-8"))
    if registry.get("server_name") != "ProUse":
        fail("registry example server_name is not ProUse")

    schema = json.loads((ROOT / "examples/workspace-registry.schema.json").read_text(encoding="utf-8"))
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        fail("registry schema is missing the expected JSON Schema dialect")

    policy = json.loads((ROOT / "examples/context-policy.example.json").read_text(encoding="utf-8"))
    if policy.get("version") != 1 or policy.get("name") != "example-context":
        fail("context policy example is not generic version 1")

    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".venv") or part.endswith("-venv") for part in rel.parts):
            continue
        if rel == Path("scripts/release_check.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in PRIVATE_PATTERNS.items():
            if pattern.search(text):
                hits.append(f"{rel}: {label}")

    if hits:
        fail("public-tree privacy scan found:\n  " + "\n  ".join(sorted(set(hits))))

    print("ProUse release check: OK")
    print("Public tree privacy scan: clean")
    print("Admin default bind: 127.0.0.1")
    print("Registry schema artifact: present")


if __name__ == "__main__":
    main()
