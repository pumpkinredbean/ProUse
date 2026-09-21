import json
from pathlib import Path
import tempfile
import unittest

from codex_orchestrator import TaskBroker, OrchestratorError, _atomic_json, _json_bytes, _sha256, task_key
from migrate_orchestrator_state import migrate
from workspace_registry import Registry


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.source = self.base / '.state/old'
        self.request = {'orchestrator_task_id': 'old-task', 'objective': 'Historical task', 'allowed_paths': ['output.md'],
                        'deliverables': [], 'validation': [], 'source_refs': [], 'write_mode': 'workspace_write', 'max_seconds': 60}
        self.legacy = dict(self.request, protocol_version=1, worker_model='gpt-5.6-sol')
        self.directory = self.source / 'tasks' / task_key('old-task')
        _atomic_json(self.directory / 'request.json', self.legacy)
        _atomic_json(self.directory / 'state.json', {'protocol_version': 1, 'orchestrator_task_id': 'old-task',
                     'worker_model': 'gpt-5.6-sol', 'request_sha256': _sha256(_json_bytes(self.legacy)),
                     'status': 'succeeded', 'codex_thread_id': 'historical-independent-thread'})
        self.registry = Registry({'version': 1, 'state_dir': '.state/new', 'default_worker_profile': 'standard',
                                 'model_capabilities': {'gpt-5.6-sol': ['xhigh']},
                                 'worker_profiles': [{'id': 'standard', 'model': 'gpt-5.6-sol', 'reasoning_effort': 'xhigh'}],
                                 'workspaces': [{'id': 'project', 'label': 'Project', 'root': 'project', 'enabled': True}]}, self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_retains_history_and_duplicate_never_launches(self):
        result = migrate(self.registry, 'project', self.source)
        self.assertEqual(result['task_ids'], ['old-task'])
        self.assertEqual(json.loads((self.directory / 'request.json').read_text()), self.legacy)
        broker = TaskBroker(registry=self.registry)
        receipt = broker.get('old-task', workspace_id='project')
        self.assertEqual(receipt['workspace_id'], 'project')
        self.assertTrue(receipt['legacy_receipt'])
        self.assertIsNone(receipt['actual_model'])
        self.assertTrue(broker.submit(dict(self.request, workspace_id='project'))['duplicate_submission'])
        self.assertEqual(migrate(self.registry, 'project', self.source)['task_ids'], [])
        with self.assertRaises(OrchestratorError):
            broker.submit(dict(self.request, workspace_id='project', objective='Different'))

    def test_nonterminal_legacy_task_blocks_migration(self):
        state = json.loads((self.directory / 'state.json').read_text())
        state['status'] = 'running'
        _atomic_json(self.directory / 'state.json', state)
        with self.assertRaises(OrchestratorError):
            migrate(self.registry, 'project', self.source)

    def test_legacy_single_root_api_reads_and_retries_old_receipt(self):
        broker = TaskBroker(self.root, self.source)
        self.assertEqual(broker.get('old-task')['workspace_id'], 'legacy')
        self.assertTrue(broker.submit(self.request)['duplicate_submission'])
        with self.assertRaises(OrchestratorError):
            broker.get('old-task', workspace_id='other')


if __name__ == '__main__':
    unittest.main()
