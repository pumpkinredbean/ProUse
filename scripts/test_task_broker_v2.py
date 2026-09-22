import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from codex_orchestrator import (TaskBroker, OrchestratorError, _permission_settings,
                              _workspace_manifest, _manifest_delta)
from workspace_registry import Registry

FAKE = r'''#!/usr/bin/env python3
import json, pathlib, sys, time
args=sys.argv[1:]
request=json.loads(sys.stdin.read().split('Orchestrator task JSON:\n',1)[1])
mode=request['objective']
out=pathlib.Path(args[args.index('--output-last-message')+1])
(out.parent/'argv.json').write_text(json.dumps(args))
if mode=='slow': time.sleep(.3)
if mode=='hold':
 (out.parent/'worker-started').write_text('started')
 while not (out.parent/'release-worker').exists(): time.sleep(.02)
if mode in ('write','slow'): pathlib.Path('allowed/output.md').write_text('exact output\n')
if mode=='outside': pathlib.Path('unexpected.md').write_text('outside\n')
if mode=='read_only_write': pathlib.Path('allowed/output.md').write_text('violation\n')
if mode=='new_symlink': pathlib.Path('allowed/link').symlink_to('../original.md')
if mode!='missing_thread': print(json.dumps({'type':'thread.started','thread_id':'independent-test-'+request['workspace_id']}))
if mode=='two_threads': print(json.dumps({'type':'thread.started','thread_id':'different'}))
if mode!='no_runtime': print(json.dumps({'type':'turn_context','payload':{'model':'wrong' if mode=='wrong_model' else request['worker_model'], 'effort': 'low' if mode=='wrong_effort' else request['worker_reasoning_effort']}}))
if mode=='reconnect': print(json.dumps({'type':'error','message':'Reconnecting... 1/5 (stream disconnected before completion)'}))
if mode!='no_completed': print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}))
if mode=='bad_events': print('malformed event')
final={'summary':'done','deliverables':[],'validations':['local validation exit 0'],'blockers':[]}
if mode=='blocker': final['blockers']=['cannot execute']
if mode=='bad_array': final['validations']='not a list'
if mode=='extra_key': final['unknown']='invalid'
if mode=='missing_key': del final['summary']
if mode=='absolute_summary': final['summary']=str(pathlib.Path.cwd()/'allowed/output.md')
if mode=='malformed': out.write_text('not json')
elif mode!='missing_result': out.write_text(json.dumps(final))
if mode=='nonzero': sys.exit(4)
'''


class TaskBrokerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        for name in ('alpha', 'beta'):
            root = self.base / name
            (root / 'allowed').mkdir(parents=True)
            (root / 'original.md').write_text('pre-existing dirty bytes\n')
        self.fake = self.base / 'fake-codex'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.config = {'version': 1, 'default_worker_profile': 'standard',
                       'model_capabilities': {'test-model': ['high']},
                       'worker_profiles': [{'id': 'standard', 'model': 'test-model', 'reasoning_effort': 'high'}],
                       'codex_bin': str(self.fake),
                       'workspaces': [{'id': n, 'label': n, 'root': n, 'enabled': True} for n in ('alpha', 'beta')]}
        self.registry = Registry(self.config, self.base)
        self.broker = TaskBroker(registry=self.registry)
        self.request = {'workspace_id': 'alpha', 'orchestrator_task_id': 'task', 'objective': 'write',
                        'allowed_paths': ['allowed'], 'max_seconds': 60}
        self.submitted = []

    def tearDown(self):
        for wid, tid in self.submitted:
            self.broker.get(tid, 10, wid)
        self.tmp.cleanup()

    def submit(self, **changes):
        request = dict(self.request, **changes)
        receipt = self.broker.submit(request)
        self.submitted.append((request['workspace_id'], request['orchestrator_task_id']))
        return receipt

    def run_mode(self, mode, **changes):
        self.submit(objective=mode, **changes)
        return self.broker.get('task', 10, 'alpha')

    def test_valid_receipt_exact_runtime_profile_and_preserved_changes(self):
        result = self.run_mode('write')
        self.assertEqual(result['status'], 'succeeded', result)
        for field, expected in [('workspace_id', 'alpha'), ('worker_profile_id', 'standard'),
                                ('worker_access', 'full_access'),
                                ('actual_model', 'test-model'), ('actual_reasoning_effort', 'high'),
                                ('codex_thread_id', 'independent-test-alpha')]:
            self.assertEqual(result[field], expected)
        self.assertEqual(result['changed_paths'], ['allowed/output.md'])
        self.assertEqual(result['unexpected_changed_paths'], [])
        self.assertEqual((self.base / 'alpha/original.md').read_text(), 'pre-existing dirty bytes\n')
        self.assertNotIn(str(self.base), json.dumps(result))
        private = next((self.base / '.state/orchestrator/workspaces/alpha/tasks').glob('*/state.json'))
        state = json.loads(private.read_text())
        self.assertEqual(state['workspace_root'], str(self.base / 'alpha'))
        self.assertTrue((private.parent / 'manifest-before.json').exists())
        self.assertTrue((private.parent / 'manifest-after.json').exists())
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)

    def test_scope_violation(self):
        result = self.run_mode('outside')
        self.assertEqual(result['status'], 'scope_violation')
        self.assertEqual(result['unexpected_changed_paths'], ['unexpected.md'])

    def test_read_only_means_no_writes_even_inside_allowed(self):
        self.assertEqual(self.run_mode('read_only_write', write_mode='read_only')['status'], 'scope_violation')

    def test_new_symlink_is_violation(self):
        self.assertEqual(self.run_mode('new_symlink')['status'], 'scope_violation')

    def test_absolute_traversal_secret_and_runtime_paths_rejected(self):
        for path in ('../beta', str(self.base / 'beta'), 'allowed/../original.md', '.state/x', '.git/config', '.GIT/config', '.STATE/x',
                     'allowed/.env', 'allowed/secrets.json', '.', 'allowed//x', 'allowed\\x'):
            with self.subTest(path=path), self.assertRaises(OrchestratorError):
                self.submit(allowed_paths=[path])

    def test_cross_workspace_symlink_and_nested_symlink_rejected(self):
        (self.base / 'alpha/allowed/escape').symlink_to(self.base / 'beta', target_is_directory=True)
        for path in ('allowed/escape/file.md', 'allowed'):
            with self.subTest(path=path), self.assertRaises(OrchestratorError):
                self.submit(allowed_paths=[path])

    def test_cross_workspace_hardlink_rejected(self):
        os.link(self.base / 'beta/original.md', self.base / 'alpha/allowed/link.md')
        with self.assertRaises(OrchestratorError):
            self.submit()

    def test_task_cannot_supply_root_model_effort_or_authorization(self):
        for key in ('root', 'worker_model', 'model', 'reasoning_effort', 'authorized_operations'):
            with self.subTest(key=key), self.assertRaises(OrchestratorError):
                self.submit(**{key: 'override'})

    def test_actual_thread_required(self):
        result = self.run_mode('missing_thread')
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['codex_thread_id'])

    def test_multiple_thread_ids_fail(self):
        self.assertEqual(self.run_mode('two_threads')['status'], 'failed')

    def test_malformed_result_fails(self):
        self.assertEqual(self.run_mode('malformed')['status'], 'failed')

    def test_missing_result_fails(self):
        self.assertEqual(self.run_mode('missing_result')['status'], 'failed')

    def test_wrong_result_array_type_fails(self):
        self.assertEqual(self.run_mode('bad_array')['status'], 'failed')

    def test_extra_result_fields_fail(self):
        self.assertEqual(self.run_mode('extra_key')['status'], 'failed')

    def test_missing_result_fields_fail(self):
        self.assertEqual(self.run_mode('missing_key')['status'], 'failed')

    def test_nonzero_process_exit_fails(self):
        result = self.run_mode('nonzero')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 4)

    def test_missing_completed_event_fails(self):
        self.assertEqual(self.run_mode('no_completed')['status'], 'failed')

    def test_unreported_runtime_provenance_keeps_the_work_result(self):
        result = self.run_mode('no_runtime')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertIsNone(result['actual_model'])
        self.assertEqual(result['blockers'], [])

    def test_mismatched_runtime_model_provenance_fails(self):
        self.assertEqual(self.run_mode('wrong_model')['status'], 'failed')

    def test_runtime_model_mismatch_fails(self):
        self.assertEqual(self.run_mode('wrong_model')['status'], 'failed')

    def test_runtime_effort_mismatch_fails(self):
        self.assertEqual(self.run_mode('wrong_effort')['status'], 'failed')

    def test_malformed_events_fail(self):
        self.assertEqual(self.run_mode('bad_events')['status'], 'failed')

    def test_worker_blockers_fail(self):
        self.assertEqual(self.run_mode('blocker')['status'], 'failed')

    def test_exact_retry_and_changed_bytes(self):
        self.submit()
        duplicate = self.broker.submit(dict(self.request))
        self.assertTrue(duplicate['duplicate_submission'])
        for change in ({'objective': 'write '}, {'validation': ['different']}, {'allowed_paths': ['allowed/output.md']}):
            with self.subTest(change=change), self.assertRaises(OrchestratorError):
                self.broker.submit(dict(self.request, **change))

    def test_concurrent_duplicate_is_one_launch(self):
        request = dict(self.request, objective='slow')
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(self.broker.submit, [request] * 3))
        self.submitted.append(('alpha', 'task'))
        self.assertEqual(sum(not r['duplicate_submission'] for r in results), 1)
        self.assertEqual(self.broker.get('task', 10, 'alpha')['status'], 'succeeded')

    def test_running_duplicate_returns_before_worker_finishes(self):
        request = dict(self.request, objective='hold')
        self.submit(objective='hold')
        directory = self.broker._selected('alpha')._paths('task')[0]
        deadline = time.monotonic() + 5
        while not (directory / 'worker-started').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.broker.submit, request)
            try:
                duplicate = future.result(timeout=2)
                self.assertTrue(duplicate['duplicate_submission'])
                self.assertEqual(duplicate['status'], 'running')
            finally:
                (directory / 'release-worker').write_text('release')
        self.assertEqual(self.broker.get('task', 10, 'alpha')['status'], 'succeeded')

    def test_receipt_survives_new_broker_instance(self):
        self.run_mode('write')
        restarted = TaskBroker(registry=Registry(self.config, self.base))
        self.assertEqual(restarted.get('task', workspace_id='alpha')['status'], 'succeeded')
        self.assertTrue(restarted.submit(self.request)['duplicate_submission'])

    def test_same_task_id_is_namespaced_per_workspace(self):
        self.submit()
        self.submit(workspace_id='beta')
        for wid in ('alpha', 'beta'):
            self.assertEqual(self.broker.get('task', 10, wid)['workspace_id'], wid)
            self.assertTrue((self.base / wid / 'allowed/output.md').exists())

    def test_private_root_redacted_from_worker_summary(self):
        result = self.run_mode('absolute_summary')
        self.assertNotIn(str(self.base), json.dumps(result))
        self.assertEqual(result['summary'], '<workspace>/allowed/output.md')

    def test_recovered_reconnect_notice_does_not_fail_the_task(self):
        result = self.run_mode('reconnect')
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['usage'], {'input_tokens': 1})

    def test_task_text_stays_on_stdin_and_security_options_are_explicit(self):
        self.run_mode('no_edit $(do-not-execute) `also-no`')
        p = next((self.base / '.state/orchestrator/workspaces/alpha/tasks').glob('*/argv.json'))
        argv = json.loads(p.read_text())
        self.assertEqual(argv[-1], '-')
        self.assertFalse(any('do-not-execute' in a for a in argv))
        self.assertIn('--ignore-user-config', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn('sandbox_mode="danger-full-access"', argv)
        self.assertIn('shell_environment_policy.inherit="all"', argv)
        self.assertNotIn('default_permissions="broker-task"', argv)
        self.assertIn('model_reasoning_effort="high"', argv)


class WorkerAccessModeTests(unittest.TestCase):
    def test_full_access_adds_no_sandbox_settings(self):
        self.assertEqual(_permission_settings(Path('/tmp/ws'), {
            'write_mode': 'workspace_write', 'allowed_paths': ['a.md']}), [])

    def test_workspace_sandbox_keeps_the_restricted_boundary(self):
        settings = _permission_settings(Path('/tmp/ws'), {
            'write_mode': 'workspace_write', 'allowed_paths': ['a.md'],
            'worker_access': 'workspace_sandbox'})
        self.assertIn('default_permissions="broker-task"', settings)
        self.assertIn('permissions.broker-task.network.enabled=false', settings)
        self.assertIn('permissions.broker-task.filesystem={":root"="deny",":minimal"="read","/tmp/ws"=',
                      settings[1])


if __name__ == '__main__':
    unittest.main()
