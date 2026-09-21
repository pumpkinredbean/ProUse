import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workspace_discovery


class KeepValueIO(io.StringIO):
    def close(self):
        pass


class FakeProcess:
    def __init__(self, lines):
        self.stdin = KeepValueIO()
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class WorkspaceDiscoveryTests(unittest.TestCase):
    def test_private_codex_and_temporary_roots_are_not_candidates(self):
        custom_codex_home = Path.home() / 'custom-codex-home'
        with patch.dict('os.environ', {'CODEX_HOME': str(custom_codex_home)}):
            self.assertTrue(workspace_discovery._is_private_or_ephemeral(Path('/private/tmp/example')))
            self.assertTrue(workspace_discovery._is_private_or_ephemeral(Path.home() / '.codex/worktrees/example'))
            self.assertTrue(workspace_discovery._is_private_or_ephemeral(custom_codex_home / 'worktrees/example'))
            self.assertTrue(workspace_discovery._is_private_or_ephemeral(Path.home() / 'workspace/project/.state/run'))
            self.assertFalse(workspace_discovery._is_private_or_ephemeral(Path.home() / 'workspace/project'))

    def test_recent_workspaces_deduplicates_and_ignores_home_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'alpha').mkdir()
            process = FakeProcess([
                {'id': 1, 'result': {'platformFamily': 'unix'}},
                {'id': 2, 'result': {'data': [
                    {'cwd': str(root / 'alpha'), 'name': 'Latest', 'updatedAt': 3},
                    {'cwd': str(root / 'alpha'), 'name': 'Older', 'updatedAt': 2},
                    {'cwd': str(root / 'missing'), 'updatedAt': 1},
                    {'cwd': str(Path.home()), 'updatedAt': 1},
                ]}},
            ])
            with patch('workspace_discovery.subprocess.Popen', return_value=process), \
                 patch('workspace_discovery.select.select', side_effect=lambda read, *_: (read, [], [])), \
                 patch('workspace_discovery._is_private_or_ephemeral', return_value=False):
                result = workspace_discovery.recent_workspaces(Path('/fake/codex'), root)
            self.assertEqual(result, [{
                'path': str(root / 'alpha'), 'label': 'alpha', 'thread_name': 'Latest',
                'project_id': None, 'last_used': 3,
            }])
            sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual([item['method'] for item in sent], ['initialize', 'initialized', 'thread/list'])
            self.assertEqual(sent[-1]['params']['sortKey'], 'recency_at')


if __name__ == '__main__':
    unittest.main()
