#!/usr/bin/env python3
"""Copy terminal v1 single-root receipts into an approved v2 workspace namespace.

Admin-only offline operation. Originals remain untouched; never resumes a worker.
"""
import argparse
import json
import os
from pathlib import Path
import shutil

from codex_orchestrator import (TaskBroker, TERMINAL_STATES, OrchestratorError, _atomic_json,
                                _read_json, _json_bytes, _sha256, upgrade_legacy_request, task_key)
from workspace_registry import Registry


def migrate(registry, workspace_id, source_state):
    workspace, profile = registry.resolve(workspace_id)
    broker = TaskBroker(registry=registry)._selected(workspace_id)
    migrated = []
    for source in sorted((source_state / 'tasks').glob('task_*')):
        request = _read_json(source / 'request.json')
        state = _read_json(source / 'state.json')
        if state.get('status') not in TERMINAL_STATES:
            raise OrchestratorError('Stop migration: a legacy task is not terminal')
        if request.get('worker_model') != profile['model']:
            raise OrchestratorError('Legacy worker model does not match migration profile')
        normalized = upgrade_legacy_request(request, workspace_id, profile)
        destination, request_path, state_path = broker._paths(normalized['orchestrator_task_id'])
        if source.name != task_key(normalized['orchestrator_task_id']):
            raise OrchestratorError('Legacy task directory identity mismatch')
        request_hash = _sha256(_json_bytes(normalized))
        if destination.exists():
            if _read_json(state_path).get('request_sha256') != request_hash:
                raise OrchestratorError('Migration destination already contains different history')
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = destination.with_name('.' + destination.name + '.migration')
        if staging.exists():
            raise OrchestratorError('Incomplete migration staging directory requires local inspection')
        shutil.copytree(source, staging, symlinks=True)
        for path in staging.rglob('*'):
            if path.is_symlink():
                raise OrchestratorError('Legacy task state contains an unexpected symlink')
            path.chmod(0o700 if path.is_dir() else 0o600)
        staging.chmod(0o700)
        state.update(protocol_version=2, workspace_id=workspace_id, workspace_root=str(workspace['root']),
                     worker_profile_id=profile['id'], worker_model=profile['model'],
                     worker_reasoning_effort=profile['reasoning_effort'], resolved_model=profile['model'],
                     resolved_reasoning_effort=profile['reasoning_effort'], actual_model=None,
                     actual_reasoning_effort=None, legacy_receipt=True,
                     legacy_request_sha256=state['request_sha256'], request_sha256=request_hash)
        # Historical terminal status is evidence, not retroactive v2 validation.
        _atomic_json(staging / 'request.json', normalized)
        _atomic_json(staging / 'state.json', state)
        staging.rename(destination)
        migrated.append(normalized['orchestrator_task_id'])
    return {'status': 'migrated', 'workspace_id': workspace_id, 'task_ids': migrated,
            'original_state_preserved': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry', required=True, type=Path)
    p.add_argument('--workspace-id', required=True)
    p.add_argument('--source-state', required=True, type=Path)
    args = p.parse_args()
    os.umask(0o077)
    print(json.dumps(migrate(Registry.load(args.registry), args.workspace_id, args.source_state.resolve(strict=True)), indent=2))


if __name__ == '__main__':
    main()
