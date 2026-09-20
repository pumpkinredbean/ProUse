"""Shared local administration lock and admission control; no remote mutation tools."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path


@contextmanager
def administration_lock(state_dir):
    directory = Path(state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(directory / 'administration.lock', os.O_CREAT | os.O_RDWR, 0o600), 'a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def admission_paused(state_dir):
    path = Path(state_dir) / 'admission.json'
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text()).get('paused') is not False
    except (ValueError, OSError, AttributeError):
        return True
