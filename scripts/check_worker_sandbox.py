#!/usr/bin/env python3
"""Exercise the installed Codex OS sandbox with synthetic files; no model calls."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from codex_orchestrator import _permission_settings


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--codex-bin', required=True)
    p.add_argument('--scratch', type=Path, required=True)
    args = p.parse_args()
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.scratch) as tmp:
        base = Path(tmp).resolve()
        root = base / 'alpha'
        other = base / 'beta'
        (root / 'allowed').mkdir(parents=True)
        other.mkdir()
        (root / '.state').mkdir()
        (root / '.git').mkdir()
        (root / '.env').write_text('synthetic marker, not a credential')
        (root / '.state/private').write_text('synthetic marker')
        (root / '.git/config').write_text('synthetic marker')
        (other / 'marker').write_text('synthetic marker')
        (root / 'allowed/link').symlink_to(other, target_is_directory=True)
        code = r'''root=$1
other=$2
mode=$3
check() { name=$1; shift; if "$@" 2>/dev/null; then printf 'FAIL:%s\n' "$name"; exit 1; else printf 'PASS:%s\n' "$name"; fi; }
write_file() { printf 'marker' > "$1"; }
if [ "$mode" = workspace_write ]; then
  write_file "$root/allowed/output.txt" || exit 1
  printf 'PASS:allowed_write\n'
else
  check read_only_denies_allowed_write write_file "$root/allowed/output.txt"
fi
check outside_allowed_write write_file "$root/unexpected.txt"
check other_workspace_write write_file "$other/unexpected.txt"
check other_workspace_read /bin/cat "$other/marker"
check symlink_escape_read /bin/cat "$root/allowed/link/marker"
check runtime_read /bin/cat "$root/.state/private"
check secret_read /bin/cat "$root/.env"
check git_metadata_write write_file "$root/.git/config"
'''
        results = {}
        for mode in ('workspace_write', 'read_only'):
            argv = [args.codex_bin, 'sandbox', '-P', 'broker-task', '--cd', str(root)]
            for setting in _permission_settings(
                    root, {'write_mode': mode, 'allowed_paths': ['allowed'],
                           'worker_access': 'workspace_sandbox'}):
                argv += ['--config', setting]
            argv += ['--', '/bin/sh', '-c', code, 'sandbox-check', str(root), str(other), mode]
            proc = subprocess.run(argv, cwd=root, text=True, capture_output=True, timeout=30)
            if proc.returncode:
                print(proc.stderr[-4000:], file=sys.stderr)
                print(proc.stdout[-4000:], file=sys.stderr)
                raise SystemExit(proc.returncode)
            results[mode] = {line.split(':', 1)[1]: True for line in proc.stdout.splitlines() if line.startswith('PASS:')}
            assert len(results[mode]) == 8, proc.stdout
        print(json.dumps({'status': 'passed', 'checks': sum(len(v) for v in results.values()), 'results': results}, indent=2))


if __name__ == '__main__':
    main()
