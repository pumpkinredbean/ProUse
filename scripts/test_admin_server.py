import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from admin_server import Admin, AdminError, Server, revision
from admin_control import administration_lock, admission_paused
from codex_orchestrator import TaskBroker, _atomic_json
from test_task_broker_v2 import FAKE
from workspace_registry import Registry


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        (self.base / 'alpha/allowed').mkdir(parents=True)
        (self.base / 'alpha/read.md').write_text('approved bytes\n')
        (self.base / 'policy.json').write_text(json.dumps({'version': 1, 'files': ['read.md']}))
        fake = self.base / 'codex'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.path = self.base / 'registry.json'
        _atomic_json(self.path, {'version': 1, 'default_workspace_id': 'alpha', 'default_worker_profile': 'standard',
            'model_capabilities': {'test-model': ['high']}, 'codex_bin': str(fake),
            'worker_profiles': [{'id': 'standard', 'model': 'test-model', 'reasoning_effort': 'high'}],
            'workspaces': [{'id': 'alpha', 'label': 'Alpha', 'root': 'alpha', 'enabled': True, 'context_policy': 'policy.json'}]})
        self.admin = Admin(self.path)
        self.broker = TaskBroker(registry=Registry.load(self.path))
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            self.admin.cancel('alpha', job)
            self.broker.get(job, 10, 'alpha')
        self.tmp.cleanup()

    def submit(self, task_id='task', objective='hold'):
        self.broker.submit({'workspace_id': 'alpha', 'orchestrator_task_id': task_id, 'objective': objective,
                            'allowed_paths': ['allowed'], 'max_seconds': 60})
        self.jobs.append(task_id)
        return self.broker._selected('alpha')._paths(task_id)[0]

    def started(self, directory):
        deadline = time.monotonic() + 5
        while not (directory / 'worker-started').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)

    def test_validate_and_apply_persists_private_policy_revision(self):
        draft = self.admin.bundle()
        before = revision(draft)
        draft['config']['workspaces'][0]['label'] = 'Updated'
        self.assertEqual(self.admin.validate(draft).workspaces['alpha']['label'], 'Updated')
        result = self.admin.apply(draft, before)
        self.assertNotEqual(result['revision'], before)
        current = Registry.load(self.path)
        self.assertEqual(current.inspect('alpha')['label'], 'Updated')
        self.assertTrue(Path(current.workspaces['alpha']['context_policy']).is_file())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.base / 'policy.json').read_text()), {'version': 1, 'files': ['read.md']})
        self.assertEqual(self.admin.history()['events'][0]['action'], 'configuration_applied')

    def test_optimistic_concurrency_does_not_overwrite_newer_configuration(self):
        draft = self.admin.bundle()
        rev = revision(draft)
        draft['config']['workspaces'][0]['label'] = 'First'
        self.admin.apply(draft, rev)
        draft['config']['workspaces'][0]['label'] = 'Second'
        with self.assertRaisesRegex(AdminError, 'another session'):
            self.admin.apply(draft, rev)
        self.assertEqual(Registry.load(self.path).inspect('alpha')['label'], 'First')

    def test_rollback_restores_policy_bytes_and_defaults(self):
        before = self.admin.bundle()
        original = revision(before)
        changed = copy.deepcopy(before)
        changed['policies']['alpha']['files'] = ['allowed/output.md']
        result = self.admin.apply(changed, original)
        self.admin.rollback(original, result['revision'])
        self.assertEqual(self.admin.bundle()['policies'], before['policies'])

    def test_invalid_configuration_leaves_original_bytes_untouched(self):
        before = self.path.read_bytes()
        draft = self.admin.bundle()
        rev = revision(draft)
        draft['config']['workspaces'].append(copy.deepcopy(draft['config']['workspaces'][0]))
        with self.assertRaises(AdminError): self.admin.apply(draft, rev)
        self.assertEqual(self.path.read_bytes(), before)

    def test_read_policy_cannot_grant_secrets_traversal_or_unknown_extensions(self):
        for policy in ({'version': 1, 'files': ['../escape.md']}, {'version': 1, 'files': ['.env']},
                       {'version': 1, 'directories': [{'path': 'allowed', 'extensions': ['.key']}]},
                       {'version': 1, 'files': ['read.md'], 'bypass': True}):
            draft = self.admin.bundle(); draft['policies']['alpha'] = policy
            with self.subTest(policy=policy), self.assertRaises(AdminError): self.admin.validate(draft)

    def test_policy_preview_reads_real_approved_bytes(self):
        result = self.admin.dispatch('POST', '/api/policy/preview', {'bundle': self.admin.bundle(), 'workspace_id': 'alpha'})
        self.assertEqual(result['workspace_id'], 'alpha')
        self.assertEqual(result['files'][0]['path'], 'read.md')
        self.assertEqual(len(result['files'][0]['sha256']), 64)

    def test_unknown_model_and_effort_rejected(self):
        for field, value in [('model', 'made-up-model'), ('reasoning_effort', 'unsupported')]:
            draft = self.admin.bundle(); draft['config']['worker_profiles'][0][field] = value
            with self.assertRaises(AdminError): self.admin.validate(draft)

    def test_runtime_paths_cannot_be_reconfigured_by_browser(self):
        for field, value in [('state_dir', '/tmp/.state/moved'), ('codex_bin', '/bin/sh')]:
            draft = self.admin.bundle(); draft['config'][field] = value
            with self.assertRaisesRegex(AdminError, 'startup'): self.admin.validate(draft)

    def test_apply_blocked_while_worker_active(self):
        self.submit()
        draft = self.admin.bundle()
        with self.assertRaisesRegex(AdminError, 'active tasks'): self.admin.apply(draft, revision(draft))

    def test_cancel_running_worker_produces_terminal_receipt_and_scope(self):
        directory = self.submit(); self.started(directory)
        self.assertEqual(self.admin.cancel('alpha', 'task')['status'], 'cancel_requested')
        result = self.broker.get('task', 5, 'alpha')
        self.assertEqual(result['status'], 'cancelled', result)
        self.assertEqual(result['unexpected_changed_paths'], [])
        self.assertNotEqual(result['exit_code'], 0)
        self.assertTrue((directory / 'manifest-after.json').exists())

    def test_cancel_queued_worker_without_waiting_for_running_job(self):
        directory = self.submit('first'); self.started(directory)
        self.submit('second')
        self.admin.cancel('alpha', 'second')
        result = self.broker.get('second', 5, 'alpha')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.broker.get('first', workspace_id='alpha')['status'], 'running')

    def test_cancellation_is_idempotent_and_never_restarts_terminal_task(self):
        self.submit(objective='write')
        self.broker.get('task', 5, 'alpha')
        self.assertEqual(self.admin.cancel('alpha', 'task')['status'], 'already_terminal')

    def test_task_history_prevents_workspace_reassignment_or_deletion(self):
        self.submit(objective='write'); self.broker.get('task', 5, 'alpha')
        (self.base / 'beta').mkdir()
        draft = self.admin.bundle(); draft['config']['workspaces'][0]['root'] = 'beta'
        with self.assertRaisesRegex(AdminError, 'task history'): self.admin.validate(draft)
        draft = self.admin.bundle(); draft['config']['workspaces'] = []; draft['policies'] = {}; del draft['config']['default_workspace_id']
        with self.assertRaisesRegex(AdminError, 'task history'): self.admin.validate(draft)

    def direct_operation(self, status='succeeded'):
        path = self.admin.state / 'workspaces/alpha/direct-operations/op_123/receipt.json'
        _atomic_json(path, {'executor_kind': 'direct', 'operation_id': 'direct-123',
                            'workspace_id': 'alpha', 'status': status,
                            'worker_model': None, 'codex_thread_id': None,
                            'created_at': '2026-09-20T00:00:00+00:00'})
        return path

    def test_direct_history_prevents_workspace_reassignment(self):
        self.direct_operation()
        draft = self.admin.bundle()
        (self.base / 'beta').mkdir()
        draft['config']['workspaces'][0]['root'] = 'beta'
        with self.assertRaisesRegex(AdminError, 'direct execution history'):
            self.admin.validate(draft)

    def test_live_direct_operation_blocks_apply_from_raw_state(self):
        self.direct_operation('running')
        draft = self.admin.bundle()
        with self.assertRaisesRegex(AdminError, 'active tasks'):
            self.admin.apply(draft, revision(draft))

    def test_disabled_workspace_keeps_terminal_direct_operation_visible(self):
        self.direct_operation()
        draft = self.admin.bundle(); revision_before = revision(draft)
        draft['config']['workspaces'][0]['enabled'] = False
        del draft['config']['default_workspace_id']
        self.admin.apply(draft, revision_before)
        receipts = self.admin.direct_executions()
        self.assertEqual(receipts[0]['operation_id'], 'direct-123')
        # Command submission correctly rejects disabled workspaces, but historical terminal receipts
        # must not make Admin state, a later configuration apply, or restart admission unusable.
        self.assertEqual(self.admin.active(), [])
        current = self.admin.bundle()
        enabled = copy.deepcopy(current)
        enabled['config']['workspaces'][0]['enabled'] = True
        enabled['config']['default_workspace_id'] = 'alpha'
        self.admin.apply(enabled, revision(current))

    def test_direct_read_cancel_diff_endpoints_use_workspace_and_operation_without_worker_provenance(self):
        controller = __import__('unittest').mock.Mock()
        controller.get_execution.return_value = {'executor_kind': 'direct', 'worker_model': None, 'codex_thread_id': None,
                                                  'stdout_base64': 'b2s=', 'stderr_base64': ''}
        controller.workspace_diff.return_value = {'executor_kind': 'direct', 'changed_paths': ['read.md']}
        controller.cancel_execution.return_value = {'status': 'cancel_requested'}
        with patch.object(self.admin, 'direct_controller', return_value=controller):
            receipt = self.admin.dispatch('POST', '/api/direct/get', {'workspace_id': 'alpha', 'operation_id': 'direct-123'})
            self.assertIsNone(receipt['worker_model'])
            self.assertEqual(self.admin.dispatch('POST', '/api/direct/diff', {'workspace_id': 'alpha', 'operation_id': 'direct-123'})['changed_paths'], ['read.md'])
            self.assertEqual(self.admin.dispatch('POST', '/api/direct/cancel', {'workspace_id': 'alpha', 'operation_id': 'direct-123'})['status'], 'cancel_requested')
        controller.cancel_execution.assert_called_once_with('alpha', 'direct-123')


    def test_disabled_workspace_task_receipts_remain_visible(self):
        self.submit(objective='write'); self.broker.get('task', 5, 'alpha')
        draft = self.admin.bundle(); rev = revision(draft)
        draft['config']['workspaces'][0]['enabled'] = False; del draft['config']['default_workspace_id']
        self.admin.apply(draft, rev)
        self.assertEqual(self.admin.tasks()[0]['orchestrator_task_id'], 'task')

    def test_pause_is_durable_and_audited(self):
        self.admin.dispatch('POST', '/api/admission', {'paused': True})
        self.assertTrue(admission_paused(self.admin.state))
        self.admin.dispatch('POST', '/api/admission', {'paused': False})
        self.assertFalse(admission_paused(self.admin.state))
        self.assertEqual(len(self.admin.history()['events']), 2)

    def test_unknown_runtime_is_not_started(self):
        with patch('admin_server.subprocess.run') as run:
            with self.assertRaises(AdminError): self.admin.restart()
            run.assert_not_called()

    def test_restart_blocked_while_worker_active(self):
        self.submit()
        with self.assertRaisesRegex(AdminError, 'active'): self.admin.restart()

    def test_redacted_public_receipt_cannot_bypass_active_task_guard(self):
        from codex_orchestrator import task_key
        path = self.admin.state / 'workspaces/alpha/tasks' / task_key('withheld') / 'state.json'
        _atomic_json(path, {'status': 'running', 'workspace_id': 'alpha', 'orchestrator_task_id': 'withheld',
                           'summary': 'sk-proj-' + 'a' * 30})
        self.assertTrue(self.admin.tasks()[0]['report_withheld'])
        self.assertEqual(len(self.admin.active()), 1)
        bundle = self.admin.bundle()
        with self.assertRaisesRegex(AdminError, 'active tasks'):
            self.admin.apply(bundle, revision(bundle))

    def test_reconcile_live_runner_is_blocked(self):
        directory = self.submit(); self.started(directory)
        with self.assertRaisesRegex(AdminError, 'still active'):
            self.admin.reconcile('alpha', 'task')

    def test_reconcile_orphan_preserves_history_without_retry(self):
        directory = self.admin.state / 'workspaces/alpha/tasks' / __import__('codex_orchestrator').task_key('orphan')
        _atomic_json(directory / 'state.json', {'status': 'running', 'orchestrator_task_id': 'orphan', 'workspace_id': 'alpha'})
        self.assertEqual(self.admin.reconcile('alpha', 'orphan')['status'], 'failed')
        self.assertIn('no automatic retry', json.loads((directory / 'state.json').read_text())['error'])

    def test_fixed_worker_probe_records_profile_and_finishes_without_writes(self):
        receipt = self.admin.worker_probe('alpha', 'standard')
        self.jobs.append(receipt['orchestrator_task_id'])
        result = self.broker.get(receipt['orchestrator_task_id'], 5, 'alpha')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['actual_model'], 'test-model')
        self.assertEqual(result['changed_paths'], [])
        request_path = self.broker._selected('alpha')._paths(receipt['orchestrator_task_id'])[1]
        self.assertEqual(json.loads(request_path.read_text())['write_mode'], 'read_only')

    def test_worker_probe_cannot_bypass_pause(self):
        self.admin.dispatch('POST', '/api/admission', {'paused': True})
        with self.assertRaisesRegex(AdminError, 'admission'): self.admin.worker_probe('alpha', 'standard')

    def test_schema_and_revision_routes(self):
        schema = self.admin.dispatch('GET', '/api/schema', {})
        self.assertIn('workspaces', schema['registry']['properties'])
        rev = revision(self.admin.bundle())
        self.assertEqual(self.admin.dispatch('GET', '/api/revisions/' + rev, {})['revision'], rev)
        with self.assertRaises(AdminError): self.admin.dispatch('GET', '/api/revisions/../../etc/passwd', {})

    def test_workspace_candidates_merge_registered_and_recent_codex_roots(self):
        (self.base / 'beta').mkdir()
        (self.base / 'alpha/nested').mkdir()
        recent = [
            {'path': str(self.base / 'beta'), 'label': 'beta', 'thread_name': 'Beta work',
             'project_id': None, 'last_used': 10},
            {'path': str(self.base / 'alpha/nested'), 'label': 'nested', 'thread_name': 'Nested work',
             'project_id': None, 'last_used': 9},
        ]
        with patch('admin_server.recent_workspaces', return_value=recent):
            result = self.admin.workspace_candidates(refresh=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([item['source'] for item in result['candidates']], ['registered', 'recent_codex', 'recent_codex'])
        self.assertEqual(result['candidates'][1]['path'], str(self.base / 'beta'))
        self.assertIn(str(self.base / 'alpha/nested'), [item['path'] for item in result['candidates']])

    def test_workspace_candidate_failure_keeps_registered_picker_available(self):
        from workspace_discovery import WorkspaceDiscoveryError
        with patch('admin_server.recent_workspaces', side_effect=WorkspaceDiscoveryError('offline')):
            result = self.admin.workspace_candidates(refresh=True)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['candidates'][0]['registered_workspace_id'], 'alpha')

    def test_workspace_directory_browser_is_bounded_and_allows_overlap(self):
        (self.base / 'beta/child').mkdir(parents=True)
        (self.base / '__pycache__').mkdir()
        (self.base / 'node_modules').mkdir()
        self.admin._workspace_candidates_cache = {
            'status': 'ready', 'message': None, 'candidates': [], 'browse_roots': [str(self.base)]}
        self.admin._workspace_candidates_cached_at = time.monotonic()
        top = self.admin.browse_workspace_directories(str(self.base))
        self.assertIn('beta', [item['name'] for item in top['directories']])
        self.assertNotIn('__pycache__', [item['name'] for item in top['directories']])
        self.assertNotIn('node_modules', [item['name'] for item in top['directories']])
        self.assertTrue(self.admin.browse_workspace_directories(str(self.base / 'alpha'))['can_select'])
        self.assertTrue(self.admin.browse_workspace_directories(str(self.base / 'beta'))['can_select'])
        with self.assertRaisesRegex(AdminError, 'outside'):
            self.admin.browse_workspace_directories('/tmp')

    def test_overlapping_workspace_applies_without_special_review_step(self):
        (self.base / 'alpha/nested').mkdir()
        bundle = self.admin.bundle()
        bundle['config']['workspaces'].append({
            'id': 'nested', 'label': 'Nested', 'root': str(self.base / 'alpha/nested'), 'enabled': True,
        })
        result = self.admin.apply(bundle, revision(self.admin.bundle()))
        self.assertEqual(result['status'], 'applied')
        registry = Registry.load(self.path)
        self.assertEqual(registry.workspace('alpha')['writer_lock'], registry.workspace('nested')['writer_lock'])

    def test_workspace_discovery_routes(self):
        (self.base / 'beta').mkdir()
        with patch.object(self.admin, 'workspace_candidates', return_value={
                'status': 'ready', 'message': None, 'candidates': [], 'browse_roots': [str(self.base)]}):
            self.assertEqual(self.admin.dispatch('GET', '/api/workspace-candidates', {})['status'], 'ready')
            result = self.admin.dispatch('POST', '/api/workspace/browse', {'path': str(self.base / 'beta')})
            self.assertEqual(result['path'], str(self.base / 'beta'))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        AdminTests.setUp(self)
        self.server = Server(('127.0.0.1', 0), self.admin, [])
        self.host = f'127.0.0.1:{self.server.server_port}'
        self.server.hosts.add(self.host)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''; self.csrf = ''

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        AdminTests.tearDown(self)

    def call(self, path, body=None, **extra):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Host': self.host, 'Cookie': self.cookie, **extra}
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': 'http://' + self.host, 'X-CSRF-Token': self.csrf})
            headers.update(extra)
        connection.request('GET' if body is None else 'POST', path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse(); status = response.status
        data = response.read(); response_headers = dict(response.getheaders()); connection.close()
        return status, json.loads(data) if response_headers.get('Content-Type') == 'application/json' else data, response_headers

    def bootstrap(self):
        status, value, headers = self.call('/api/session')
        self.assertEqual(status, 200)
        self.csrf = value['csrf']
        return value, headers

    def test_local_state_and_operations_need_no_login(self):
        self.assertEqual(self.call('/api/state')[0], 200)
        self.bootstrap()
        self.assertEqual(self.call('/api/admission', {'paused': True})[0], 200)
        self.assertTrue(admission_paused(self.admin.state))

    def test_browser_bootstrap_is_automatic_without_cookie_or_access_file(self):
        value, headers = self.bootstrap()
        self.assertEqual(value['access_mode'], 'local_no_login')
        self.assertNotIn('Set-Cookie', headers)
        self.assertFalse((self.admin.directory / 'access.json').exists())

    def test_origin_host_and_csrf_blocks(self):
        self.bootstrap()
        for headers in [{'Origin': 'http://evil.invalid'}, {'Host': 'evil.invalid'}, {'X-CSRF-Token': 'wrong'}]:
            self.assertEqual(self.call('/api/admission', {'paused': True}, **headers)[0], 403)
        self.assertFalse(admission_paused(self.admin.state))

    def test_login_and_logout_routes_are_removed(self):
        self.bootstrap()
        self.assertEqual(self.call('/api/login', {'code': 'unused'})[0], 404)
        self.assertEqual(self.call('/api/logout', {})[0], 404)

    def test_stale_auth_cookie_does_not_gate_local_access(self):
        self.cookie = 'orch_admin=expired'
        self.assertEqual(self.call('/api/state')[0], 200)

    def test_http_apply_conflict_and_pause(self):
        self.bootstrap(); bundle = self.admin.bundle()
        bundle['config']['workspaces'][0]['label'] = 'Through HTTP'
        self.assertEqual(self.call('/api/config/apply', {'bundle': bundle, 'expected_revision': 'stale'})[0], 409)
        self.assertEqual(self.call('/api/config/apply', {'bundle': bundle, 'expected_revision': revision(self.admin.bundle())})[0], 200)
        self.assertEqual(self.call('/api/admission', {'paused': True})[0], 200)
        self.assertTrue(admission_paused(self.admin.state))

    def test_review_only_validation_route_is_removed(self):
        self.bootstrap()
        self.assertEqual(self.call('/api/config/validate', {'bundle': self.admin.bundle()})[0], 404)

    def test_admin_ui_has_immediate_save_without_review_workflow(self):
        from importlib import resources
        source = resources.files('prouse_assets').joinpath('admin_ui', 'app.js').read_text()
        self.assertNotIn('설정 변경 검토', source)
        self.assertNotIn('/api/config/validate', source)
        self.assertNotIn('data-action="review"', source)
        self.assertGreaterEqual(source.count('await applyBundle(next,'), 5)

    def test_static_shell_has_no_private_config_or_inline_code(self):
        status, html, headers = self.call('/')
        self.assertEqual(status, 200); self.assertNotIn(str(self.base).encode(), html)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.call('/../registry.json')[0], 404)

if __name__ == '__main__': unittest.main()
