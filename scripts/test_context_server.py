import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import asynccontextmanager
from functools import wraps

from test_task_broker_v2 import FAKE
from workspace_registry import Registry


TOOLS = {'list_workspaces', 'inspect_workspace', 'list_worker_profiles', 'resolve_execution_context',
         'registry_health', 'workspace_index', 'list_directory', 'glob_files', 'search_files', 'grep_files',
         'read_files', 'read_file_bytes', 'changed_files',
         'write_file', 'edit_file', 'delete_file',
         'submit_codex_worker_task', 'get_codex_worker_task',
         'exec_command', 'get_execution', 'write_execution_stdin', 'cancel_execution', 'workspace_diff', 'read_artifact'}


def connected(function):
    @wraps(function)
    async def wrapped(self):
        async with self.connection():
            await function(self)
    return wrapped


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        for name in ('alpha', 'beta'):
            (self.base / name / 'allowed').mkdir(parents=True)
            (self.base / name / 'read.md').write_text('same source\n')
            (self.base / name / 'allowed/note.md').write_text('first line\nTODO: follow up\nlast line\n')
            (self.base / name / 'allowed/tool.py').write_text('def run():\n    return 1\n')
        (self.base / 'policy.json').write_text(json.dumps({
            'version': 1, 'name': 'test', 'files': ['read.md'],
            'directories': [{'path': 'allowed', 'extensions': ['.md', '.py']}]}))
        fake = self.base / 'fake-codex'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.config = {'version': 1, 'default_worker_profile': 'standard', 'codex_bin': str(fake),
                       'model_capabilities': {'test-model': ['high']},
                       'worker_profiles': [{'id': 'standard', 'model': 'test-model', 'reasoning_effort': 'high'}],
                       'workspaces': [{'id': n, 'label': n, 'root': n, 'enabled': True, 'context_policy': 'policy.json'}
                                      for n in ('alpha', 'beta')]}
        (self.base / 'registry.json').write_text(json.dumps(self.config))

    @asynccontextmanager
    async def connection(self):
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(Path(__file__).with_name('context_server.py')), '--registry', str(self.base / 'registry.json')])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                self.session = session
                await session.initialize()
                yield

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def call(self, name, args, error=False):
        response = await self.session.call_tool(name, args)
        self.assertEqual(bool(response.isError), error, response)
        return response.structuredContent

    @connected
    async def test_real_stdio_discovery_and_readonly_admin_contracts(self):
        tools = await self.session.list_tools()
        self.assertEqual({t.name for t in tools.tools}, TOOLS)
        for tool in tools.tools:
            self.assertEqual(tool.annotations.readOnlyHint, tool.name not in {
                'submit_codex_worker_task', 'exec_command', 'write_execution_stdin', 'cancel_execution',
                'write_file', 'edit_file', 'delete_file'})
            self.assertEqual(tool.annotations.destructiveHint, not tool.annotations.readOnlyHint)
        workspaces = await self.call('list_workspaces', {})
        self.assertEqual([w['workspace_id'] for w in workspaces['workspaces']], ['alpha', 'beta'])
        self.assertNotIn(str(self.base), json.dumps(workspaces))
        self.assertEqual((await self.call('registry_health', {}))['status'], 'ready')
        self.assertEqual((await self.call('list_worker_profiles', {}))['default_worker_profile'], 'standard')
        self.assertEqual((await self.call('inspect_workspace', {'workspace_id': 'beta'}))['label'], 'beta')

    @connected
    async def test_real_stdio_structured_ambiguity_and_unknown_errors(self):
        for name in ('resolve_execution_context', 'workspace_index'):
            value = await self.call(name, {}, error=True)
            self.assertEqual(value['error']['code'], 'ambiguous_workspace')
            self.assertEqual(len(value['candidates']), 2)
        value = await self.call('resolve_execution_context', {'workspace_id': 'missing'}, error=True)
        self.assertEqual(value['error']['code'], 'unknown_workspace')
        value = await self.call('resolve_execution_context', {'workspace_id': 'alpha', 'worker_profile_id': 'raw'}, error=True)
        self.assertEqual(value['error']['code'], 'unknown_worker_profile')

    @connected
    async def test_real_stdio_workspace_scoped_context_and_views(self):
        a = await self.call('workspace_index', {'workspace_id': 'alpha'})
        source = next(item for item in a['files'] if item['path'] == 'read.md')
        read = await self.call('read_files', {'workspace_id': 'alpha', 'requests': [{'path': 'read.md', 'sha256': source['sha256']}], 'expected_view_id': a['view_id']})
        self.assertEqual(read['files'][0]['text'], '1: same source')
        b = await self.call('read_files', {'workspace_id': 'beta', 'requests': [{'path': 'read.md'}], 'expected_view_id': a['view_id']})
        self.assertEqual(b['status'], 'changed_view')
        mismatch = await self.call('changed_files', {'workspace_id': 'beta', 'known': {}, 'known_view_id': a['view_id']}, error=True)
        self.assertEqual(mismatch['error']['code'], 'workspace_view_mismatch')
        await self.call('read_files', {'workspace_id': 'alpha', 'requests': [{'path': '../beta/read.md'}]}, error=True)

    @connected
    async def test_real_stdio_filesystem_primitives_and_change_detection(self):
        listing = await self.call('list_directory', {'workspace_id': 'alpha', 'depth': 2})
        self.assertEqual([entry['path'] for entry in listing['entries']],
                         ['allowed', 'allowed/note.md', 'allowed/tool.py', 'read.md'])
        globbed = await self.call('glob_files', {'workspace_id': 'alpha', 'patterns': ['**/*.py']})
        self.assertEqual([item['path'] for item in globbed['files']], ['allowed/tool.py'])
        grep = await self.call('grep_files', {'workspace_id': 'alpha', 'pattern': 'TODO|FIXME', 'before': 1})
        self.assertEqual([item['path'] for item in grep['matches']], ['allowed/note.md'])
        self.assertEqual(grep['matches'][0]['line'], 2)
        self.assertEqual(grep['matches'][0]['before'], [{'line': 1, 'text': 'first line'}])
        literal = await self.call('search_files', {'workspace_id': 'alpha', 'queries': ['TODO'], 'limit': 5})
        self.assertEqual(literal['total_matches'], 1)
        window = await self.call('read_file_bytes', {'workspace_id': 'alpha', 'path': 'allowed/note.md',
                                                     'offset': 0, 'limit': 5})
        self.assertEqual(window['text'], 'first')
        self.assertEqual(window['next_offset'], 5)
        index = await self.call('workspace_index', {'workspace_id': 'alpha'})
        known = {item['path']: item['sha256'] for item in index['files']}
        (self.base / 'alpha/allowed/new.md').write_text('new file\n')
        delta = await self.call('changed_files', {'workspace_id': 'alpha', 'known': known})
        self.assertEqual([item['path'] for item in delta['added']], ['allowed/new.md'])
        self.assertEqual(delta['modified'], [])
        self.assertEqual(delta['deleted'], [])

    @connected
    async def test_real_stdio_policy_scoped_file_mutations_and_hash_preconditions(self):
        created = await self.call('write_file', {
            'workspace_id': 'alpha', 'path': 'allowed/new.md', 'content': 'first\n'})
        self.assertTrue(created['created'])
        edited = await self.call('edit_file', {
            'workspace_id': 'alpha', 'path': 'allowed/new.md',
            'expected_sha256': created['sha256'], 'edits': [{'old': 'first', 'new': 'second'}]})
        self.assertEqual(edited['replacements'], 1)
        await self.call('write_file', {
            'workspace_id': 'alpha', 'path': 'allowed/new.md', 'content': 'stale\n',
            'expected_sha256': created['sha256']}, error=True)
        read = await self.call('read_files', {
            'workspace_id': 'alpha', 'requests': [{'path': 'allowed/new.md', 'sha256': edited['sha256']}]})
        self.assertEqual(read['files'][0]['text'], '1: second')
        deleted = await self.call('delete_file', {
            'workspace_id': 'alpha', 'path': 'allowed/new.md', 'expected_sha256': edited['sha256']})
        self.assertTrue(deleted['deleted'])
        invalid_hash = await self.session.call_tool('delete_file', {
            'workspace_id': 'alpha', 'path': 'allowed/note.md', 'expected_sha256': 'not-a-hash'})
        self.assertTrue(invalid_hash.isError)

    @connected
    async def test_real_stdio_file_mutation_respects_the_shared_writer_lock(self):
        registry = Registry.load(self.base / 'registry.json')
        lock_path = registry.workspace('alpha')['writer_lock']
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = await self.call('write_file', {
                'workspace_id': 'alpha', 'path': 'allowed/locked.md', 'content': 'blocked\n'}, error=True)
        self.assertEqual(blocked['error']['code'], 'request_rejected')
        self.assertFalse((self.base / 'alpha/allowed/locked.md').exists())

    @connected
    async def test_real_stdio_task_submission_and_durable_poll(self):
        request = {'workspace_id': 'alpha', 'worker_profile_id': 'standard', 'orchestrator_task_id': 'mcp-task',
                   'objective': 'write', 'allowed_paths': ['allowed'], 'max_seconds': 60}
        first = await self.call('submit_codex_worker_task', {'request': request})
        duplicate = await self.call('submit_codex_worker_task', {'request': request})
        self.assertTrue(duplicate['duplicate_submission'])
        result = await self.call('get_codex_worker_task', {'workspace_id': 'alpha', 'orchestrator_task_id': 'mcp-task', 'wait_seconds': 10})
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['actual_model'], 'test-model')
        self.assertEqual(first['task_key'], result['task_key'])
        await self.call('get_codex_worker_task', {'workspace_id': 'beta', 'orchestrator_task_id': 'mcp-task'}, error=True)

    @connected
    async def test_live_reload_configuration_and_policy_on_same_mcp_session(self):
        first = await self.call('list_workspaces', {})
        self.config['workspaces'][0]['label'] = 'Reloaded Alpha'
        (self.base / 'registry.json').write_text(json.dumps(self.config))
        changed = await self.call('list_workspaces', {})
        self.assertNotEqual(first['registry_sha256'], changed['registry_sha256'])
        self.assertEqual(changed['workspaces'][0]['label'], 'Reloaded Alpha')
        (self.base / 'policy.json').write_text(json.dumps({'version': 1, 'files': ['other.md']}))
        index = await self.call('workspace_index', {'workspace_id': 'alpha'})
        self.assertEqual(index['files'][0]['path'], 'other.md')
        self.assertEqual(index['files'][0]['status'], 'unavailable')
        (self.base / 'registry.json').write_text('{invalid')
        await self.call('list_workspaces', {}, error=True)

    @connected
    async def test_admin_pause_blocks_only_new_submissions_and_keeps_context_available(self):
        state = self.base / '.state/orchestrator'
        (state / 'admission.json').write_text(json.dumps({'paused': True}))
        request = {'workspace_id': 'alpha', 'orchestrator_task_id': 'paused',
                   'objective': 'write', 'allowed_paths': ['allowed'], 'max_seconds': 60}
        value = await self.call('submit_codex_worker_task', {'request': request}, error=True)
        self.assertEqual(value['error']['code'], 'admission_paused')
        write = await self.call('write_file', {
            'workspace_id': 'alpha', 'path': 'allowed/paused.md', 'content': 'blocked\n'}, error=True)
        self.assertEqual(write['error']['code'], 'admission_paused')
        await self.call('workspace_index', {'workspace_id': 'alpha'})
        (state / 'admission.json').write_text(json.dumps({'paused': False}))
        await self.call('submit_codex_worker_task', {'request': request})
        receipt = await self.call('get_codex_worker_task', {'workspace_id': 'alpha', 'orchestrator_task_id': 'paused', 'wait_seconds': 10})
        self.assertEqual(receipt['status'], 'succeeded')

    @connected
    async def test_raw_model_root_overrides_rejected_by_mcp_schema(self):
        request = {'workspace_id': 'alpha', 'orchestrator_task_id': 'bad', 'objective': 'write', 'allowed_paths': ['allowed'], 'model': 'raw'}
        result = await self.session.call_tool('submit_codex_worker_task', {'request': request})
        self.assertTrue(result.isError)


if __name__ == '__main__':
    unittest.main()
