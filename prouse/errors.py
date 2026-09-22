"""Public exit codes and actionable errors shared by CLI operations."""
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    NOT_RUNNING = 3


class CLIError(RuntimeError):
    def __init__(self, message: str, code: int = ExitCode.ERROR):
        super().__init__(message)
        self.code = int(code)


def error_detail(error: BaseException) -> str:
    """Keep useful errors visible through asyncio/AnyIO task groups."""
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(error_detail(item) for item in error.exceptions)
    return str(error) or type(error).__name__
