#!/usr/bin/env python3
"""Local operations console for the existing workspace MCP broker."""
from __future__ import annotations
import argparse
import asyncio
import copy
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from importlib import resources

import jsonschema
from admin_control import administration_lock, admission_paused
from codex_orchestrator import TaskBroker, TERMINAL_STATES, _atomic_json, _now, task_key, TASK_ID_RE
from context_store import ContextStore, ContextError, ALLOWED_EXTENSIONS
from direct_execution import DirectExecutionController, DirectExecutionError
from workspace_registry import Registry, ResolutionError, REGISTRY_SCHEMA, ID_PATTERN
from workspace_discovery import recent_workspaces, WorkspaceDiscoveryError

POLICY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["version"], "properties": {
    "version": {"const": 1}, "name": {"type": "string", "maxLength": 120},
    "files": {"type": "array", "maxItems": 1000, "uniqueItems": True, "items": {"type": "string"}},
        "directories": {"type": "array", "maxItems": 100, "items": {"type": "object", "additionalProperties": False,
        "required": ["path", "extensions"], "properties": {"path": {"type": "string"}, "extensions": {
            "type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(ALLOWED_EXTENSIONS)}}}}}}}

DIRECT_TERMINAL_STATES = {"succeeded", "failed", "scope_violation", "cancelled", "interrupted"}

class AdminError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def revision(bundle):
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Admin:
    def __init__(self, registry_path, runtime=None):
        self.path = Path(registry_path).resolve(strict=True)
        self.registry = Registry.load(self.path)
        self.state = self.registry.state_dir
        self.directory = self.state / 'admin'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime = runtime or {}
        self.capabilities = copy.deepcopy(self.registry.config['model_capabilities'])
        # Only the installed CLI's non-secret catalog fields are inspected.
        config_path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
        try:
            catalog_path = tomllib.loads(config_path.read_text()).get('model_catalog_json')
            catalog = json.loads(Path(catalog_path).read_text()) if catalog_path else {}
            for model in catalog.get('models', []):
                efforts = [e['effort'] for e in model.get('supported_reasoning_levels', [])]
                if model.get('slug') and efforts:
                    self.capabilities[model['slug']] = efforts
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self.pinned = {k: self.registry.config.get(k) for k in ('state_dir', 'codex_bin')}
        self._workspace_candidates_cache = None
        self._workspace_candidates_cached_at = 0.0
        self._snapshot(self.bundle())

    def bundle(self):
        registry = Registry.load(self.path)
        policies = {}
        for wid, workspace in registry.workspaces.items():
            if workspace.get('context_policy'):
                policies[wid] = json.loads(registry._path(workspace['context_policy'], True).read_text())
        return {'config': copy.deepcopy(registry.config), 'policies': policies}

    def audit(self, action, details):
        event = dict(time=_now(), action=action, **details)
        with os.fdopen(os.open(self.directory / 'audit.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def _snapshot(self, bundle):
        rev = revision(bundle)
        path = self.directory / 'revisions' / (rev + '.json')
        if not path.exists():
            _atomic_json(path, {'revision': rev, 'saved_at': _now(), 'bundle': bundle})
        return rev

    def tasks(self):
        result = []
        for path in sorted((self.state / 'workspaces').glob('*/tasks/task_*/state.json')):
            if path.is_symlink():
                continue
            try:
                state = json.loads(path.read_text())
                if not isinstance(state, dict):
                    raise ValueError()
                if state.get('status') == 'running' and not state.get('codex_thread_id'):
                    event_path = path.parent / 'events.jsonl'
                    if event_path.exists():
                        with event_path.open('rb') as handle:
                            for line in handle.read(65536).splitlines():
                                try:
                                    event = json.loads(line)
                                    if event.get('type') == 'thread.started':
                                        state['codex_thread_id'] = event.get('thread_id')
                                        break
                                except (ValueError, AttributeError):
                                    continue
                receipt = TaskBroker._public(state)
                receipt['cancel_requested'] = (path.parent / 'cancel.json').exists()
                result.append(receipt)
            except (OSError, ValueError):
                result.append({'status': 'corrupt', 'workspace_id': path.parents[2].name,
                               'orchestrator_task_id': path.parent.name, 'error': 'Invalid durable receipt'})
        return sorted(result, key=lambda s: s.get('created_at', ''), reverse=True)

    def direct_controller(self):
        """Use the current validated registry; direct sessions never use browser-supplied roots."""
        return DirectExecutionController(Registry.load(self.path))

    def direct_executions(self):
        try:
            controller = self.direct_controller()
            result = controller.list_executions(limit=100)
            receipts = result.get('executions') if isinstance(result, dict) else None
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message'], 503) from None
        if not isinstance(receipts, list) or any(not isinstance(receipt, dict) for receipt in receipts):
            raise AdminError('Direct execution history is unavailable', 503)
        # Keep the operator-facing collection bounded and remove any accidental private root field.
        return [dict(receipt) for receipt in receipts if isinstance(receipt, dict)]

    def _direct_history(self, workspace_id):
        """Find durable direct history without trusting the redacted Admin presentation."""
        directory = self.state / 'workspaces' / workspace_id / 'direct-operations'
        if not directory.is_dir():
            return False
        try:
            return any(path.is_file() and not path.is_symlink() for path in directory.glob('*/receipt.json'))
        except OSError:
            # An unreadable state directory must fail configuration closed.
            return True

    def _direct_active(self):
        """Use operation accounting plus raw durable receipts as a fail-closed guard."""
        current = Registry.load(self.path)
        active = []
        try:
            controller = DirectExecutionController(current)
            # The registry correctly refuses new work in a disabled workspace. Historical operation
            # receipts still need to be visible to Admin, so read their raw durable state below rather
            # than asking the command controller to resolve a disabled workspace.
            for workspace_id, workspace in current.workspaces.items():
                if not workspace.get('enabled'):
                    continue
                for item in controller.active_for_workspace(workspace_id) or []:
                    if isinstance(item, dict):
                        active.append(dict(item, executor_kind='direct'))
                    else:
                        active.append({'workspace_id': workspace_id, 'status': 'active', 'executor_kind': 'direct'})
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message'], 503) from None
        # Cover malformed/unknown state as well: no configuration may discard a live direct operation.
        for path in self.state.glob('workspaces/*/direct-operations/*/receipt.json'):
            if path.is_symlink():
                continue
            workspace_id = path.parents[2].name
            try:
                receipt = json.loads(path.read_text())
                if not isinstance(receipt, dict) or receipt.get('status') not in DIRECT_TERMINAL_STATES:
                    marker = {'workspace_id': workspace_id, 'operation_id': receipt.get('operation_id', path.parent.name) if isinstance(receipt, dict) else path.parent.name,
                              'status': receipt.get('status', 'corrupt') if isinstance(receipt, dict) else 'corrupt',
                              'executor_kind': 'direct'}
                    if marker not in active:
                        active.append(marker)
            except (OSError, ValueError):
                active.append({'workspace_id': workspace_id, 'operation_id': path.parent.name,
                               'status': 'corrupt', 'executor_kind': 'direct'})
        return active

    def active(self):
        # Admission must use durable state, never redacted/truncated presentation receipts.
        active = []
        for path in (self.state / 'workspaces').glob('*/tasks/task_*/state.json'):
            try:
                state = json.loads(path.read_text())
                if not isinstance(state, dict) or not isinstance(state.get('status'), str):
                    raise ValueError()
                if state['status'] not in TERMINAL_STATES:
                    active.append({'workspace_id': path.parents[2].name, 'task_key': path.parent.name,
                                   'status': state['status']})
            except (OSError, ValueError):
                active.append({'workspace_id': path.parents[2].name, 'task_key': path.parent.name,
                               'status': 'corrupt'})
        return active + self._direct_active()

    def validate(self, bundle):
        if not isinstance(bundle, dict) or set(bundle) != {'config', 'policies'} or not isinstance(bundle['policies'], dict):
            raise AdminError('Expected config and workspace policies')
        config = copy.deepcopy(bundle['config'])
        for key, value in self.pinned.items():
            if config.get(key) != value:
                raise AdminError(f'{key} is managed by local startup configuration')
        if not isinstance(config.get('model_capabilities'), dict) or any(
            model not in self.capabilities or not isinstance(efforts, list) or not set(efforts) <= set(self.capabilities[model])
            for model, efforts in config['model_capabilities'].items()):
            raise AdminError('Model capabilities must be supported by the installed CLI catalog')
        try:
            jsonschema.validate(config, REGISTRY_SCHEMA)
            ids = {w['id'] for w in config['workspaces']}
            if set(bundle['policies']) - ids:
                raise AdminError('Policy belongs to an unknown workspace')
            for workspace in config['workspaces']:
                workspace.pop('context_policy', None)
            registry = Registry(config, self.path.parent)
            for wid, policy in bundle['policies'].items():
                jsonschema.validate(policy, POLICY_SCHEMA)
                ContextStore(registry.workspaces[wid]['root'], policy, workspace_id=wid)
            existing = Registry.load(self.path)
            for wid, workspace in existing.workspaces.items():
                history = self.state / 'workspaces' / wid / 'tasks'
                if ((history.exists() and any(history.iterdir())) or self._direct_history(wid)) and (
                    wid not in registry.workspaces or workspace['root'] != registry.workspaces[wid]['root']):
                    raise AdminError('A workspace with task history or direct execution history cannot be deleted or reassigned; disable it instead')
        except (jsonschema.ValidationError, ResolutionError, ContextError, OSError, TypeError, KeyError) as e:
            raise AdminError(str(e) if isinstance(e, (ResolutionError, ContextError)) else 'Invalid configuration or read policy') from None
        return registry

    def apply(self, bundle, expected_revision, action='configuration_applied'):
        with administration_lock(self.state):
            before = self.bundle()
            if revision(before) != expected_revision:
                raise AdminError('Configuration changed in another session. Reload before applying.', 409)
            if self.active():
                raise AdminError('Wait for active tasks to finish or cancel them before changing configuration.', 409)
            checked = self.validate(bundle)
            config = copy.deepcopy(bundle['config'])
            self._snapshot(before)
            for workspace in config['workspaces']:
                workspace['root'] = str(checked.workspaces[workspace['id']]['root'])
                policy = bundle['policies'].get(workspace['id'])
                workspace.pop('context_policy', None)
                if policy is not None:
                    target = self.directory / 'policies' / (revision(policy) + '.json')
                    _atomic_json(target, policy)
                    workspace['context_policy'] = str(target)
            Registry(config, self.path.parent)
            _atomic_json(self.path, config)
            after = self.bundle()
            rev = self._snapshot(after)
            self.audit(action, {'before': expected_revision, 'after': rev})
            return {'status': 'applied', 'revision': rev, 'registry_sha256': Registry.load(self.path).sha256,
                    'message': 'Effective on the next MCP call; running tasks are never rebound.'}

    def rollback(self, target, expected):
        if not isinstance(target, str) or not re.fullmatch('[a-f0-9]{64}', target):
            raise AdminError('Invalid revision')
        path = self.directory / 'revisions' / (target + '.json')
        if not path.is_file():
            raise AdminError('Unknown revision', 404)
        return self.apply(json.loads(path.read_text())['bundle'], expected, 'configuration_restored')

    def cancel(self, workspace_id, task_id):
        if not re.fullmatch(ID_PATTERN, workspace_id or '') or not TASK_ID_RE.fullmatch(task_id or ''):
            raise AdminError('Invalid workspace or task ID')
        with administration_lock(self.state):
            directory = self.state / 'workspaces' / workspace_id / 'tasks' / task_key(task_id)
            state_path = directory / 'state.json'
            if not state_path.is_file():
                raise AdminError('Unknown task', 404)
            state = json.loads(state_path.read_text())
            if state['status'] in TERMINAL_STATES:
                return {'status': 'already_terminal'}
            _atomic_json(directory / 'cancel.json', {'requested_at': _now(), 'source': 'local_admin'})
            self.audit('task_cancel_requested', {'workspace_id': workspace_id, 'orchestrator_task_id': task_id})
            return {'status': 'cancel_requested', 'message': 'Runner will stop its own worker and preserve the scope receipt.'}

    def reconcile(self, workspace_id, task_id):
        if not re.fullmatch(ID_PATTERN, workspace_id or '') or not TASK_ID_RE.fullmatch(task_id or ''):
            raise AdminError('Invalid workspace or task ID')
        with administration_lock(self.state):
            directory = self.state / 'workspaces' / workspace_id / 'tasks' / task_key(task_id)
            state_path = directory / 'state.json'
            if not state_path.is_file():
                raise AdminError('Unknown task', 404)
            with os.fdopen(os.open(directory / 'run.lock', os.O_CREAT | os.O_RDWR, 0o600), 'a+b') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise AdminError('Task runner is still active; use cancellation instead.', 409) from None
                state = json.loads(state_path.read_text())
                if state['status'] in TERMINAL_STATES:
                    return {'status': 'already_terminal'}
                # Never signal a PID from an old receipt. A live PID keeps recovery blocked.
                for key in ('launcher_pid', 'runner_pid', 'worker_pid'):
                    pid = state.get(key)
                    if pid:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            continue
                        raise AdminError('A recorded process still exists; inspect locally before recovery.', 409)
                state.update(status='failed', error='Administrator reconciled an orphaned task; no automatic retry or rollback',
                             updated_at=_now(), completed_at=_now(), reconciliation_required=True)
                _atomic_json(state_path, state)
                self.audit('orphaned_task_reconciled', {'workspace_id': workspace_id, 'orchestrator_task_id': task_id})
                return {'status': 'failed', 'message': 'History preserved. Inspect workspace changes before issuing a new task.'}

    def worker_probe(self, workspace_id, profile_id):
        with administration_lock(self.state):
            if admission_paused(self.state):
                raise AdminError('Resume worker admission before running the probe.', 409)
            registry = Registry.load(self.path)
            workspace, profile = registry.resolve(workspace_id, profile_id)
            task_id = 'ADMIN-health-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
            request = {'workspace_id': workspace['id'], 'worker_profile_id': profile['id'],
                'orchestrator_task_id': task_id, 'objective': 'Return a structured health report with summary Independent worker health probe OK. Do not read or change any project files. Do not run commands. Return no blockers and no deliverables.',
                'allowed_paths': ['README.md'], 'write_mode': 'read_only', 'max_seconds': 120,
                'validation': ['Report that this is a no-write infrastructure health check.']}
            receipt = TaskBroker(registry=registry).submit(request)
            self.audit('worker_probe_submitted', {'workspace_id': workspace['id'], 'orchestrator_task_id': task_id})
            return receipt

    def _direct_operation(self, workspace_id, operation_id):
        if not re.fullmatch(ID_PATTERN, workspace_id or '') or not isinstance(operation_id, str) or not operation_id:
            raise AdminError('Invalid direct workspace or operation ID')

    def direct_cancel(self, workspace_id, operation_id):
        self._direct_operation(workspace_id, operation_id)
        try:
            result = self.direct_controller().cancel_execution(workspace_id, operation_id)
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message']) from None
        self.audit('direct_operation_cancel_requested', {'workspace_id': workspace_id, 'operation_id': operation_id})
        return result

    def direct_get(self, workspace_id, operation_id, output_offset=0, output_limit=16000):
        self._direct_operation(workspace_id, operation_id)
        if type(output_offset) is not int or type(output_limit) is not int:
            raise AdminError('Invalid output cursor')
        try:
            return self.direct_controller().get_execution(workspace_id, operation_id, output_offset, output_limit)
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message']) from None

    def direct_diff(self, workspace_id, operation_id):
        self._direct_operation(workspace_id, operation_id)
        try:
            return self.direct_controller().workspace_diff(workspace_id, operation_id)
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message']) from None

    def direct_diagnostics(self):
        try:
            result = self.direct_controller().diagnostics()
        except DirectExecutionError as e:
            raise AdminError(e.result['error']['message'], 503) from None
        self.audit('direct_runtime_diagnostic', {'status': result.get('status') if isinstance(result, dict) else 'unknown'})
        return result

    def history(self):
        revisions = []
        for path in (self.directory / 'revisions').glob('*.json'):
            data = json.loads(path.read_text())
            revisions.append({'revision': data['revision'], 'saved_at': data['saved_at']})
        audit_path = self.directory / 'audit.jsonl'
        events = [json.loads(line) for line in audit_path.read_text().splitlines()[-100:]] if audit_path.exists() else []
        return {'revisions': sorted(revisions, key=lambda r: r['saved_at'], reverse=True), 'events': list(reversed(events))}

    def runtime_status(self):
        result = {'configured': bool(self.runtime), 'health': False, 'ready': False}
        base = self.runtime.get('health_url')
        if base:
            for path, key in [('/healthz', 'health'), ('/readyz', 'ready')]:
                try:
                    with urllib.request.urlopen(base.rstrip('/') + path, timeout=2) as response:
                        result[key] = response.status == 200
                except OSError:
                    pass
        status_path = self.state / 'mcp-status.json'
        if status_path.exists():
            result['last_mcp_call'] = json.loads(status_path.read_text())
        result['desired_registry_sha256'] = Registry.load(self.path).sha256
        result['operator_url'] = self.runtime.get('operator_url')
        return result

    def workspace_candidates(self, refresh=False):
        """Return registered roots and recent Codex CWDs for an operator-facing picker."""
        if (not refresh and self._workspace_candidates_cache is not None and
                time.monotonic() - self._workspace_candidates_cached_at < 30):
            return copy.deepcopy(self._workspace_candidates_cache)
        registry = Registry.load(self.path)
        registered = {str(workspace['root']): (wid, workspace['label'])
                      for wid, workspace in registry.workspaces.items()}
        candidates = [{'path': path, 'label': label, 'source': 'registered',
                       'registered_workspace_id': wid, 'thread_name': None, 'last_used': None}
                      for path, (wid, label) in registered.items()]
        status = 'ready'
        message = None
        recent = []
        if registry.codex_bin is None:
            status, message = 'unavailable', 'Codex executable is not configured.'
        else:
            try:
                recent = recent_workspaces(registry.codex_bin, self.path.parent)
            except WorkspaceDiscoveryError:
                status, message = 'unavailable', '최근 Codex 작업 폴더를 불러오지 못했습니다. 등록된 경로와 폴더 탐색은 계속 사용할 수 있습니다.'
        for item in recent:
            path = item['path']
            if path in registered:
                continue
            candidates.append(dict(item, source='recent_codex', registered_workspace_id=None))
        browse_roots = []
        visible_paths = [Path(item['path']) for item in candidates]
        for value in [path.parent for path in visible_paths]:
            try:
                value = value.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if value == Path(value.anchor) or not value.is_dir() or str(value) in browse_roots:
                continue
            browse_roots.append(str(value))
        result = {'status': status, 'message': message, 'candidates': candidates[:100],
                  'browse_roots': browse_roots[:20]}
        self._workspace_candidates_cache = copy.deepcopy(result)
        self._workspace_candidates_cached_at = time.monotonic()
        return result

    def browse_workspace_directories(self, path=None, exclude_workspace_id=None):
        discovery = self.workspace_candidates()
        allowed_roots = [Path(value).resolve(strict=True) for value in discovery['browse_roots']]
        if not allowed_roots:
            raise AdminError('No approved workspace browsing roots are available', 404)
        if path is None:
            return {'path': None, 'parent': None, 'can_select': False,
                    'directories': [{'name': root.name or str(root), 'path': str(root)} for root in allowed_roots]}
        if not isinstance(path, str) or not path:
            raise AdminError('Invalid workspace directory')
        try:
            target = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise AdminError('Workspace directory is unavailable', 404) from None
        containing = [root for root in allowed_roots if target == root or target.is_relative_to(root)]
        if not containing or not target.is_dir():
            raise AdminError('Workspace directory is outside the selectable roots', 403)
        base = max(containing, key=lambda root: len(root.parts))
        registry = Registry.load(self.path)
        if exclude_workspace_id is not None and exclude_workspace_id not in registry.workspaces:
            raise AdminError('Unknown workspace ID')
        can_select = target not in (Path(target.anchor), Path.home().resolve())
        directories = []
        try:
            for child in sorted(target.iterdir(), key=lambda item: item.name.casefold()):
                if (child.name.startswith('.') or child.name in {'__pycache__', 'node_modules', 'target', 'venv'}
                        or child.is_symlink()):
                    continue
                try:
                    if child.is_dir():
                        directories.append({'name': child.name, 'path': str(child.resolve(strict=True))})
                except OSError:
                    continue
                if len(directories) >= 200:
                    break
        except OSError:
            raise AdminError('Workspace directory cannot be read', 403) from None
        parent = str(target.parent) if target != base and target.parent.is_relative_to(base) else None
        return {'path': str(target), 'parent': parent, 'can_select': can_select,
                'directories': directories, 'root': str(base)}

    async def probe(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        parameters = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('context_server.py')), '--registry', str(self.path)])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                health = await client.call_tool('registry_health', {})
                workspaces = await client.call_tool('list_workspaces', {})
                return {'status': 'passed', 'tools': [t.name for t in tools.tools],
                        'health': health.structuredContent, 'workspaces': workspaces.structuredContent,
                        'checked_at': _now(), 'transport': 'actual local MCP stdio; same registry and source as tunnel'}

    def restart(self):
        with administration_lock(self.state):
            if self.active():
                raise AdminError('Cannot restart while tasks are active.', 409)
            runtime = self.runtime
            required = {'binary', 'profile_dir', 'profile', 'session_name', 'health_url'}
            if not required <= set(runtime):
                raise AdminError('Dedicated runtime restart is not configured')
            argv = [runtime['binary'], 'run', '--allow-remote-ui', '--profile-dir', runtime['profile_dir'], '--profile', runtime['profile']]
            # Match the whole fixed command. Browser input never selects a process or executable.
            expected = ' '.join(argv)
            lines = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True).splitlines()
            pids = [int(line.strip().split(None, 1)[0]) for line in lines
                    if len(line.strip().split(None, 1)) == 2 and line.strip().split(None, 1)[1] == expected]
            if len(pids) > 1:
                raise AdminError('Multiple dedicated runtimes found; inspect locally before restart', 409)
            if pids:
                os.kill(pids[0], signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        os.kill(pids[0], 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.1)
                else:
                    raise AdminError('Dedicated runtime did not stop cleanly', 409)
            launched = subprocess.run(['tmux', 'new-session', '-d', '-s', runtime['session_name'], shlex.join(argv)], capture_output=True)
            if launched.returncode:
                raise AdminError('Dedicated runtime start failed; inspect local tmux state', 503)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = self.runtime_status()
                if status['ready']:
                    self.audit('dedicated_runtime_restarted', {})
                    return {'status': 'ready', 'runtime': status}
                time.sleep(.25)
            self.audit('dedicated_runtime_restart_unready', {})
            raise AdminError('Dedicated runtime started but is not ready', 503)

    def dispatch(self, method, path, body):
        if method == 'GET':
            if path == '/api/state':
                bundle = self.bundle()
                return {'bundle': bundle, 'revision': revision(bundle), 'capabilities': self.capabilities,
                        'tasks': self.tasks(), 'direct_executions': self.direct_executions(),
                        'direct_diagnostics': self.direct_controller().diagnostics(),
                        'paused': admission_paused(self.state), 'runtime': self.runtime_status()}
            if path.startswith('/api/revisions/'):
                rev = path.rsplit('/', 1)[-1]
                if not re.fullmatch('[a-f0-9]{64}', rev):
                    raise AdminError('Invalid revision')
                target = self.directory / 'revisions' / (rev + '.json')
                if not target.is_file():
                    raise AdminError('Unknown revision', 404)
                return json.loads(target.read_text())
            if path == '/api/history':
                return self.history()
            if path == '/api/schema':
                return {'registry': REGISTRY_SCHEMA, 'policy': POLICY_SCHEMA}
            if path == '/api/workspace-candidates':
                return self.workspace_candidates()
        elif method == 'POST':
            if path == '/api/config/apply':
                return self.apply(body['bundle'], body['expected_revision'])
            if path == '/api/config/rollback':
                return self.rollback(body['revision'], body['expected_revision'])
            if path == '/api/policy/preview':
                registry = self.validate(body['bundle'])
                wid = body['workspace_id']
                policy = body['bundle']['policies'].get(wid)
                if wid not in registry.workspaces or policy is None:
                    raise AdminError('Select a workspace with a read policy')
                return ContextStore(registry.workspaces[wid]['root'], policy, wid).index(limit=50)
            if path == '/api/tasks/cancel':
                return self.cancel(body['workspace_id'], body['task_id'])
            if path == '/api/tasks/reconcile':
                return self.reconcile(body['workspace_id'], body['task_id'])
            if path == '/api/runtime/worker-probe':
                return self.worker_probe(body['workspace_id'], body.get('worker_profile_id'))
            if path == '/api/direct/cancel':
                return self.direct_cancel(body['workspace_id'], body['operation_id'])
            if path == '/api/direct/get':
                return self.direct_get(body['workspace_id'], body['operation_id'], body.get('output_offset', 0), body.get('output_limit', 16000))
            if path == '/api/direct/diff':
                return self.direct_diff(body['workspace_id'], body['operation_id'])
            if path == '/api/runtime/direct-diagnostics':
                return self.direct_diagnostics()
            if path == '/api/admission':
                if type(body.get('paused')) is not bool:
                    raise AdminError('paused must be a boolean')
                with administration_lock(self.state):
                    _atomic_json(self.state / 'admission.json', {'paused': body['paused'], 'updated_at': _now()})
                    self.audit('admission_changed', {'paused': body['paused']})
                return {'status': 'saved', 'paused': body['paused']}
            if path == '/api/runtime/probe':
                probe = asyncio.run(asyncio.wait_for(self.probe(), timeout=25))
                self.audit('mcp_probe', {'status': probe['status']})
                return probe
            if path == '/api/runtime/restart':
                return self.restart()
            if path == '/api/workspace/browse':
                return self.browse_workspace_directories(body.get('path'), body.get('exclude_workspace_id'))
        raise AdminError('Not found', 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, admin, hosts):
        super().__init__(address, Handler)
        self.admin = admin
        self.hosts = set(hosts)
        # Automatic browser request protection; no login or operator access code.
        self.csrf = secrets.token_urlsafe(24)
        (admin.directory / 'access.json').unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Request bodies are never logged.

    def reply(self, value, status=200, content_type='application/json'):
        data = json.dumps(value, ensure_ascii=False).encode() if content_type == 'application/json' else value
        self.send_response(status)
        for key, val in {'Content-Type': content_type, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store',
                         'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                         'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def process(self):
        try:
            host = self.headers.get('Host', '')
            if host not in self.server.hosts:
                raise AdminError('Unapproved host', 403)
            path = self.path.split('?', 1)[0]
            origin = self.headers.get('Origin')
            if self.command == 'POST' and origin != 'http://' + host:
                raise AdminError('Same-origin request required', 403)
            if self.command == 'GET' and path in ('/', '/app.js', '/style.css'):
                filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                content_type = {'/': 'text/html; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8', '/style.css': 'text/css; charset=utf-8'}[path]
                asset = resources.files('prouse_assets').joinpath('admin_ui', filename)
                self.reply(asset.read_bytes(), content_type=content_type)
                return
            if self.command == 'GET' and path == '/healthz':
                self.reply({'status': 'ready'})
                return
            body = {}
            if self.command == 'POST':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise AdminError('JSON content type required', 415)
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 512000:
                    raise AdminError('Invalid body size', 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise AdminError('JSON object required')
            if path == '/api/session' and self.command == 'GET':
                self.reply({'csrf': self.server.csrf, 'access_mode': 'local_no_login'})
                return
            if self.command == 'POST' and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf):
                raise AdminError('Browser request token required; reload the page', 403)
            self.reply(self.server.admin.dispatch(self.command, path, body))
        except AdminError as e:
            self.reply({'error': str(e)}, e.status)
        except (ValueError, KeyError, TypeError, ContextError, ResolutionError):
            self.reply({'error': 'Invalid request or configuration'}, 400)
        except Exception:
            self.reply({'error': 'Local operation failed; inspect runtime/configuration on this machine'}, 503)

    do_GET = process
    do_POST = process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--runtime-config', type=Path)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8848)
    parser.add_argument('--allowed-host', action='append', default=[])
    args = parser.parse_args()
    os.umask(0o077)
    runtime = json.loads(args.runtime_config.read_text()) if args.runtime_config else None
    admin = Admin(args.registry, runtime)
    hosts = [f'localhost:{args.port}', f'127.0.0.1:{args.port}', *args.allowed_host]
    server = Server((args.host, args.port), admin, hosts)
    print(f'ProUse Admin listening on {args.host}:{args.port}; local access without login', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
