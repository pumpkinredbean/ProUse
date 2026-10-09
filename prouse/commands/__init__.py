"""Command families register parsers and adapt CLI input to application operations."""
from . import diagnostics, lifecycle, mcp, service, setup, workspace


def register(commands) -> None:
    for family in (setup, workspace, lifecycle, mcp, service, diagnostics):
        family.register(commands)
