import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from workspace_registry import Registry, ResolutionError, REGISTRY_SCHEMA


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        for name in ('alpha', 'beta'):
            (self.base / name).mkdir()
            (self.base / name / 'read.md').write_text('identical bytes\n')
        (self.base / 'policy.json').write_text(json.dumps({'version': 1, 'files': ['read.md']}))
        self.config = {'version': 1, 'default_worker_profile': 'standard',
                       'model_capabilities': {'test-model': ['high', 'xhigh']},
                       'worker_profiles': [{'id': 'standard', 'model': 'test-model', 'reasoning_effort': 'high'},
                                           {'id': 'deep', 'model': 'test-model', 'reasoning_effort': 'xhigh'}],
                       'workspaces': [{'id': 'alpha', 'label': 'Alpha', 'root': 'alpha', 'enabled': True, 'context_policy': 'policy.json'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def registry(self):
        return Registry(self.config, self.base)

    def add_beta(self, enabled=True):
        self.config['workspaces'].append({'id': 'beta', 'label': 'Beta', 'root': 'beta', 'enabled': enabled,
                                          'context_policy': 'policy.json'})

    def assertCode(self, code, function):
        with self.assertRaises(ResolutionError) as caught:
            function()
        self.assertEqual(caught.exception.result['error']['code'], code)
        return caught.exception.result

    def test_valid_canonical_workspace_and_public_inspection(self):
        r = self.registry()
        self.assertEqual(r.workspace('alpha')['root'], self.base / 'alpha')
        self.assertNotIn(str(self.base), json.dumps(r.list_workspaces()))
        self.assertEqual(r.inspect('alpha')['label'], 'Alpha')

    def test_duplicate_workspace_id(self):
        self.config['workspaces'].append(dict(self.config['workspaces'][0]))
        self.assertCode('invalid_configuration', self.registry)

    def test_missing_and_non_directory_roots(self):
        for root in ('missing', 'policy.json'):
            with self.subTest(root=root):
                self.config['workspaces'][0]['root'] = root
                self.assertCode('invalid_configuration', self.registry)

    def test_malformed_entries_fail_closed(self):
        original = copy.deepcopy(self.config)
        for changes in ({'enabled': 'yes'}, {'id': '../bad'}, {'label': ''}, {'token': 'synthetic'}, {'root': 4}):
            with self.subTest(changes=changes):
                self.config = copy.deepcopy(original)
                self.config['workspaces'][0].update(changes)
                self.assertCode('invalid_configuration', self.registry)
        self.config = copy.deepcopy(original)
        del self.config['workspaces'][0]['enabled']
        self.assertCode('invalid_configuration', self.registry)

    def test_symlink_root_is_canonicalized_and_aliases_share_writer_lock(self):
        (self.base / 'link').symlink_to(self.base / 'alpha', target_is_directory=True)
        self.config['workspaces'][0]['root'] = 'link'
        r = self.registry()
        self.assertEqual(r.workspace()['root'], self.base / 'alpha')
        (self.base / 'link').unlink()
        (self.base / 'link').symlink_to(self.base / 'beta', target_is_directory=True)
        self.assertEqual(r.workspace()['root'], self.base / 'alpha')
        self.add_beta()
        self.config['workspaces'][1]['root'] = 'link'
        self.config['workspaces'][0]['root'] = 'link'
        r = self.registry()
        self.assertEqual(r.workspace('alpha')['root'], self.base / 'beta')
        self.assertEqual(r.workspace('alpha')['writer_lock'], r.workspace('beta')['writer_lock'])

    def test_overlapping_workspaces_are_allowed_and_share_writer_lock(self):
        (self.base / 'alpha/nested').mkdir()
        self.add_beta()
        self.config['workspaces'][1]['root'] = 'alpha/nested'
        r = self.registry()
        self.assertEqual(r.workspace('beta')['root'], self.base / 'alpha/nested')
        self.assertEqual(r.workspace('alpha')['writer_lock'], r.workspace('beta')['writer_lock'])

    def test_independent_workspaces_keep_independent_writer_locks(self):
        self.add_beta()
        r = self.registry()
        self.assertNotEqual(r.workspace('alpha')['writer_lock'], r.workspace('beta')['writer_lock'])

    def test_root_and_home_not_workspaces(self):
        for root in ('/', str(Path.home()), '$HOME', '~/project'):
            with self.subTest(root=root):
                self.config['workspaces'][0]['root'] = root
                self.assertCode('invalid_configuration', self.registry)

    def test_replaced_root_fails_before_access(self):
        r = self.registry()
        (self.base / 'alpha').rename(self.base / 'original')
        (self.base / 'alpha').symlink_to(self.base / 'beta', target_is_directory=True)
        self.assertCode('workspace_unavailable', lambda: r.workspace('alpha'))

    def test_explicit_workspace_beats_default(self):
        self.add_beta()
        self.config['default_workspace_id'] = 'alpha'
        self.assertEqual(self.registry().resolve('beta')[0]['id'], 'beta')

    def test_configured_default(self):
        self.add_beta()
        self.config['default_workspace_id'] = 'beta'
        self.assertEqual(self.registry().resolve()[0]['id'], 'beta')

    def test_unique_enabled_workspace(self):
        self.add_beta(enabled=False)
        self.assertEqual(self.registry().resolve()[0]['id'], 'alpha')

    def test_ambiguous_workspace_candidates_are_bounded_public_ids(self):
        self.add_beta()
        r = self.registry()
        error = self.assertCode('ambiguous_workspace', r.resolve)
        self.assertEqual(error['candidates'], [{'workspace_id': 'alpha', 'label': 'Alpha'}, {'workspace_id': 'beta', 'label': 'Beta'}])
        self.assertNotIn(str(self.base), json.dumps(error))

    def test_no_eligible_workspace(self):
        self.config['workspaces'][0]['enabled'] = False
        self.assertCode('workspace_required', self.registry().resolve)

    def test_unknown_and_disabled_workspace(self):
        self.add_beta(enabled=False)
        r = self.registry()
        self.assertCode('unknown_workspace', lambda: r.resolve('guessed-from-text'))
        self.assertCode('disabled_workspace', lambda: r.resolve('beta'))
        self.assertCode('disabled_workspace', lambda: r.context('beta'))

    def test_invalid_default_fails_configuration(self):
        for wid in ('unknown', 'alpha'):
            with self.subTest(wid=wid):
                self.config['default_workspace_id'] = wid
                self.config['workspaces'][0]['enabled'] = False
                self.assertCode('invalid_configuration', self.registry)

    def test_profile_explicit_workspace_and_global_defaults(self):
        self.assertEqual(self.registry().resolve()[1]['id'], 'standard')
        self.config['workspaces'][0]['default_worker_profile'] = 'deep'
        self.assertEqual(self.registry().resolve()[1]['id'], 'deep')
        self.assertEqual(self.registry().resolve(worker_profile_id='standard')[1]['id'], 'standard')

    def test_unknown_profile(self):
        self.assertCode('unknown_worker_profile', lambda: self.registry().resolve(worker_profile_id='raw-model'))

    def test_duplicate_profile(self):
        self.config['worker_profiles'].append(dict(self.config['worker_profiles'][0]))
        self.assertCode('invalid_configuration', self.registry)

    def test_unsupported_model_effort(self):
        original = copy.deepcopy(self.config)
        for fields in ({'model': 'unlisted'}, {'reasoning_effort': 'max'}, {'reasoning_effort': 'nonsense'}):
            with self.subTest(fields=fields):
                self.config = copy.deepcopy(original)
                self.config['worker_profiles'][0].update(fields)
                self.assertCode('invalid_configuration', self.registry)

    def test_missing_or_unknown_global_profile(self):
        self.config['default_worker_profile'] = 'absent'
        self.assertCode('invalid_configuration', self.registry)
        del self.config['default_worker_profile']
        self.assertCode('invalid_configuration', self.registry)

    def test_invalid_context_policy_does_not_broaden_access(self):
        (self.base / 'policy.json').write_text(json.dumps({'version': 1, 'files': ['../beta/read.md']}))
        self.assertCode('invalid_configuration', self.registry)

    def test_duplicate_json_keys_rejected(self):
        p = self.base / 'registry.json'
        p.write_text('{"version":1,"version":1}')
        self.assertCode('invalid_configuration', lambda: Registry.load(p))

    def test_context_identical_bytes_cannot_share_view_across_workspaces(self):
        self.add_beta()
        r = self.registry()
        a = r.context('alpha').index()
        b = r.context('beta').index()
        self.assertEqual(a['files'][0]['sha256'], b['files'][0]['sha256'])
        self.assertNotEqual(a['view_id'], b['view_id'])
        self.assertEqual(r.context('beta').read([{'path': 'read.md'}], a['view_id'])['status'], 'changed_view')
        self.assertEqual(r.context('beta').index()['workspace_id'], 'beta')

    def test_context_policy_change_changes_view(self):
        r = self.registry()
        old = r.context('alpha').index()['view_id']
        (self.base / 'policy.json').write_text(json.dumps({'version': 1, 'name': 'new-policy', 'files': ['read.md']}))
        self.assertNotEqual(self.registry().context('alpha').index()['view_id'], old)

    def test_context_read_search_changes_are_workspace_scoped(self):
        self.add_beta()
        r = self.registry()
        a = r.context('alpha').index()
        known = {f['path']: f['sha256'] for f in a['files']}
        (self.base / 'beta/read.md').write_text('only beta\n')
        self.assertEqual(r.context('alpha').search(['only beta'])['total_matches'], 0)
        alpha = r.context('alpha').changes(known)
        self.assertEqual([alpha['added'], alpha['modified'], alpha['deleted']], [[], [], []])
        self.assertEqual(alpha['unchanged_files'], 1)
        self.assertEqual([item['path'] for item in r.context('beta').changes(known)['modified']], ['read.md'])

    def test_registry_schema_artifact_matches_runtime(self):
        p = Path(__file__).resolve().parent.parent / 'examples/workspace-registry.schema.json'
        self.assertEqual(json.loads(p.read_text()), REGISTRY_SCHEMA)


if __name__ == '__main__':
    unittest.main()
