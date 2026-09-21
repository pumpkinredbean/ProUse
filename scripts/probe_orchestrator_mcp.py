#!/usr/bin/env python3
"""Explicit, opt-in real MCP/independent-worker probe. Writes one approved marker file."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from workspace_registry import Registry
from codex_orchestrator import _safe_relative


async def run(args):
    registry = Registry.load(args.registry)
    workspace, profile = registry.resolve(args.workspace_id)
    relative = _safe_relative(args.output_relative)
    output = workspace['root'] / relative
    if output.exists():
        raise ValueError('Probe output already exists; inspect prior receipt instead of overwriting')
    if not output.parent.is_dir():
        raise ValueError('Probe output parent must already exist')
    expected = b'workspace orchestration probe ok\n'
    params = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).with_name('context_server.py')), '--registry', str(args.registry.resolve())],
            cwd=str(workspace['root']))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            discovery = {'server': initialized.serverInfo.model_dump(), 'protocol': initialized.protocolVersion,
                         'tools': [t.model_dump() for t in listed.tools]}
            args.artifact_dir.mkdir(parents=True, exist_ok=True)
            (args.artifact_dir / 'mcp-discovery.json').write_text(json.dumps(discovery, indent=2) + '\n')

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                if response.isError or response.structuredContent is None:
                    raise RuntimeError(f'MCP {name} failed: {response.structuredContent}')
                return response.structuredContent

            healthy = await call('registry_health', {})
            resolved = await call('resolve_execution_context', {'workspace_id': args.workspace_id})
            assert healthy['status'] == 'ready'
            request = {'workspace_id': args.workspace_id, 'worker_profile_id': profile['id'],
                       'orchestrator_task_id': args.task_id,
                       'objective': f'Create exactly {relative} containing exactly the single line "workspace orchestration probe ok" followed by a newline. This is only a mechanical orchestration probe. Use available system shell tools (for example /bin/sh and printf), then read the file back. Do not run project-wide tests or use Python/SDKs. Make no other changes and return the required JSON result.',
                       'allowed_paths': [relative], 'deliverables': [relative],
                       'validation': ['Read the marker back using system shell tools and report exact-byte validation with exit code.'],
                       'source_refs': [], 'write_mode': 'workspace_write', 'max_seconds': 180}
            (args.artifact_dir / 'worker-probe-request.json').write_text(json.dumps(request, indent=2) + '\n')
            submitted = await call('submit_codex_worker_task', {'request': request})
            print(json.dumps({'event': 'submitted', 'workspace_id': submitted['workspace_id'], 'task_id': args.task_id,
                              'worker_profile_id': submitted['worker_profile_id']}), flush=True)
            deadline = time.monotonic() + 240
            while True:
                receipt = await call('get_codex_worker_task', {'workspace_id': args.workspace_id,
                                      'orchestrator_task_id': args.task_id, 'wait_seconds': 10})
                if receipt['status'] in {'succeeded', 'failed', 'scope_violation', 'timed_out'}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Worker remains pending; poll its durable task ID, do not resubmit')
            # The worker's manifest is complete before we write the public verification receipt.
            checks = {'succeeded': receipt['status'] == 'succeeded', 'workspace': receipt['workspace_id'] == args.workspace_id,
                      'profile': receipt['worker_profile_id'] == profile['id'], 'actual_model': receipt.get('actual_model') == profile['model'],
                      'actual_effort': receipt.get('actual_reasoning_effort') == profile['reasoning_effort'],
                      'thread_id': bool(receipt.get('codex_thread_id')), 'no_unexpected_changes': receipt.get('unexpected_changed_paths') == [],
                      'expected_changed_path': receipt.get('changed_paths') == [relative],
                      'exact_output': output.exists() and output.read_bytes() == expected}
            report = {'transport': 'actual MCP stdio; same registry/server command as dedicated tunnel runtime',
                      'resolved_context': resolved, 'checks': checks, 'receipt': receipt,
                      'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest() if output.exists() else None}
            (args.artifact_dir / 'worker-probe-receipt.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2), flush=True)
            if not all(checks.values()):
                raise SystemExit(1)
            duplicate = await call('submit_codex_worker_task', {'request': request})
            assert duplicate['duplicate_submission'] and duplicate['codex_thread_id'] == receipt['codex_thread_id']
            report['idempotent_retry_same_thread'] = True
            (args.artifact_dir / 'worker-probe-receipt.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--output-relative', required=True)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
