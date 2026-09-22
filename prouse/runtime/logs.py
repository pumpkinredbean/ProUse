"""Bounded log reads and foreground output capture."""
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import os
from pathlib import Path
import sys
import time
from typing import Iterator, TextIO

from ..errors import CLIError, ExitCode


class Tee:
    def __init__(self, stream: TextIO, log: TextIO):
        self.stream, self.log = stream, log

    def write(self, value: str) -> int:
        self.stream.write(value)
        self.log.write(value)
        self.flush()
        return len(value)

    def flush(self) -> None:
        self.stream.flush()
        self.log.flush()


@contextmanager
def capture(path: Path):
    """Server diagnostics use stderr; stdout remains available for CLI results."""
    if os.environ.get("_PROUSE_LOG_REDIRECTED") == str(path):
        yield
        return
    with path.open("a", encoding="utf-8") as log:
        stream = Tee(sys.stderr, log)
        with redirect_stdout(stream), redirect_stderr(stream):
            yield


def lines(path: Path, count: int, follow: bool = False) -> Iterator[str]:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise CLIError(f"No log exists yet at {path}. Run `prouse start`.", ExitCode.NOT_RUNNING) from None
    with handle:
        yield from deque(handle, maxlen=count)
        while follow:
            line = handle.readline()
            if line:
                yield line
            else:
                time.sleep(0.2)
