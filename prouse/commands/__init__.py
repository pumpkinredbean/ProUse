"""Command families register parsers and adapt CLI input to application operations."""
from . import diagnostics, lifecycle, mcp, service, setup


def register(commands) -> None:
    for family in (setup, lifecycle, diagnostics, mcp, service):
        family.register(commands)
