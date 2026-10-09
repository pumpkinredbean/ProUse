"""The coding tools ProUse serves over MCP, modelled on pi's built-in tool set."""
from __future__ import annotations

from dataclasses import dataclass, field


class ToolError(Exception):
    """A failure the model should read and act on; it becomes an error tool result."""


@dataclass
class Result:
    text: str
    is_error: bool = False
    images: list[tuple[str, str]] = field(default_factory=list)  # (base64 data, MIME type)
