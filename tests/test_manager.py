import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


def load_manager():
    source = Path(__file__).parents[1] / 'workspace.py'
    if not source.exists():
        raise AssertionError('Session workspace manager has not been implemented')
    spec = importlib.util.spec_from_file_location('workspace', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Manager


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repos = self.root / 'repos'
        self.repos.mkdir()
        self.remote = self.root / 'remote.git'
        self.remote.mkdir()
        git(self.remote, 'init', '--bare', '--initial-branch=main')
        self.repo = self.repos / 'alpha'
        git(self.repos, 'clone', str(self.remote), str(self.repo))
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'file.txt').write_text('initial\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        git(self.repo, 'push', '-u', 'origin', 'main')

    def manager(self):
        return load_manager()(self.root / 'state', [self.repos], self.root / 'workspaces')

    def test_new_workspace_uses_fetched_main_without_changing_original(self):
        old = git(self.repo, 'rev-parse', 'HEAD')
        publisher = self.root / 'publisher'
        git(self.root, 'clone', str(self.remote), str(publisher))
        git(publisher, 'config', 'user.name', 'Test')
        git(publisher, 'config', 'user.email', 'test@example.invalid')
        (publisher / 'new.txt').write_text('remote update')
        git(publisher, 'add', '.')
        git(publisher, 'commit', '-m', 'remote update')
        git(publisher, 'push')
        newest = git(publisher, 'rev-parse', 'HEAD')
        (self.repo / 'file.txt').write_text('user dirty work\n')
        m = self.manager()
        m.bind('chat-1', 'task-1')
        entry = m.ensure('task-1', str(self.repo))
        self.assertEqual(git(entry['path'], 'rev-parse', 'HEAD'), newest)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), old)
        self.assertEqual((self.repo / 'file.txt').read_text(), 'user dirty work\n')
        self.assertEqual(entry['branch'], 'agent/' + m.workspace_id('task-1'))
        self.assertEqual(git(entry['path'], 'for-each-ref', '--format=%(upstream)',
                             'refs/heads/' + entry['branch']), '')

    def test_child_and_grandchild_share_root_name_across_repos_and_resume(self):
        m = self.manager()
        m.bind('root', 'root-task')
        m.bind('child', 'child-task', 'root')
        m.bind('grandchild', 'grandchild-task', 'child')
        beta = self.repos / 'beta'
        git(self.repos, 'clone', str(self.remote), str(beta))
        first = m.ensure('root-task', str(self.repo))
        second = m.ensure('grandchild-task', str(beta))
        self.assertEqual(first['branch'], second['branch'])
        self.assertNotEqual(first['path'], second['path'])
        (Path(first['path']) / 'file.txt').write_text('session edits')
        resumed = self.manager().ensure('child-task', str(self.repo))
        self.assertEqual(first, resumed)
        self.assertEqual((Path(resumed['path']) / 'file.txt').read_text(), 'session edits')
        self.assertEqual(len(self.manager().entries('root-task')), 2)

    def test_unknown_parent_is_rejected_not_given_a_different_workspace(self):
        m = self.manager()
        with self.assertRaisesRegex(RuntimeError, 'parent'):
            m.bind('child', 'child-task', 'unknown-parent')

    def test_original_and_other_session_paths_are_blocked(self):
        m = self.manager()
        m.bind('root', 'task')
        m.bind('other', 'other-task')
        ours = m.ensure('task', str(self.repo))
        theirs = m.ensure('other-task', str(self.repo))
        self.assertIsNotNone(m.guard_path('task', self.repo / 'file.txt'))
        self.assertIsNone(m.guard_path('task', Path(ours['path']) / 'file.txt'))
        self.assertIsNotNone(m.guard_path('task', Path(theirs['path']) / 'file.txt'))
        self.assertIsNone(m.guard_path('task', self.root / 'notes.txt'))

    def test_guard_resolves_symlink_to_original_checkout(self):
        m = self.manager()
        m.bind('root', 'task')
        alias = self.root / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertIsNotNone(m.guard_path('task', alias / 'file.txt'))

    def test_fetch_failure_never_creates_local_head_fallback(self):
        m = self.manager()
        m.bind('root', 'task')
        git(self.repo, 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        with self.assertRaisesRegex(RuntimeError, 'no checkout fallback'):
            m.ensure('task', str(self.repo))
        self.assertEqual(git(self.repo, 'branch', '--list', 'agent/*'), '')

    def test_missing_main_is_rejected(self):
        m = self.manager()
        m.bind('root', 'task')
        m.base_branch = 'missing'
        with self.assertRaises(RuntimeError):
            m.ensure('task', str(self.repo))
        self.assertEqual(git(self.repo, 'branch', '--list', 'agent/*'), '')

    def test_outside_allowlist_is_rejected(self):
        m = self.manager()
        m.bind('root', 'task')
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            m.ensure('task', str(self.root))

    def test_worktree_git_metadata_and_wrong_branch_are_blocked(self):
        m = self.manager()
        m.bind('root', 'task')
        entry = m.ensure('task', str(self.repo))
        self.assertIsNotNone(m.guard_path('task', Path(entry['path']) / '.git'))
        git(entry['path'], 'switch', '--detach')
        self.assertIsNotNone(m.guard_path('task', Path(entry['path']) / 'file.txt'))
        with self.assertRaisesRegex(RuntimeError, 'another branch'):
            m.ensure('task', str(self.repo))

    def test_checkout_does_not_execute_repo_smudge_filters(self):
        import shlex
        import sys
        sentinel = self.root / 'filter-ran'
        (self.repo / '.gitattributes').write_text('file.txt filter=poison\n')
        git(self.repo, 'add', '.gitattributes')
        git(self.repo, 'commit', '-m', 'attributes')
        git(self.repo, 'push')
        code = 'from pathlib import Path; Path(' + repr(str(sentinel)) + ').write_text("executed")'
        git(self.repo, 'config', 'filter.poison.smudge', shlex.quote(sys.executable) + ' -c ' + shlex.quote(code))
        m = self.manager()
        m.bind('root', 'task')
        m.ensure('task', str(self.repo))
        self.assertFalse(sentinel.exists(), 'worktree checkout executed a repo-configured command')

    def test_relative_repository_input_is_rejected(self):
        m = self.manager()
        m.bind('root', 'task')
        with self.assertRaisesRegex(RuntimeError, 'absolute'):
            m.ensure('task', '.')

    def test_replaced_worktree_is_not_silently_reused(self):
        m = self.manager()
        m.bind('root', 'task')
        entry = m.ensure('task', str(self.repo))
        git(self.repo, 'worktree', 'remove', entry['path'])
        git(self.root, 'clone', str(self.remote), entry['path'])
        git(entry['path'], 'switch', '-c', entry['branch'])
        with self.assertRaisesRegex(RuntimeError, 'repository'):
            m.ensure('task', str(self.repo))
        self.assertIsNotNone(m.guard_path('task', Path(entry['path']) / 'file.txt'))

    def test_legacy_database_migrates_concurrently_without_losing_dirty_worktree(self):
        from concurrent.futures import ThreadPoolExecutor
        m = self.manager()
        m.bind('root', 'task')
        entry = m.ensure('task', str(self.repo))
        (Path(entry['path']) / 'file.txt').write_text('session work')
        with m.connect() as conn:
            conn.execute('ALTER TABLE workspaces DROP COLUMN bootstrap')
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: self.manager().ensure('task', str(self.repo)), range(8)))
        self.assertTrue(all(result == entry for result in results))
        self.assertEqual((Path(entry['path']) / 'file.txt').read_text(), 'session work')

    def test_bootstrap_rejects_existing_paths_and_nested_checkouts(self):
        m = self.manager()
        m.bind('root', 'task')
        empty = self.repos / 'empty'
        empty.mkdir()
        for path in (self.repo, self.repo / 'new-project', self.repos, empty,
                     self.root / 'outside'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                m.ensure('task', str(path), create=True)
        self.assertEqual(m.entries('task'), [])
        self.assertEqual(git(self.repo, 'branch', '--show-current'), 'main')

    def test_bootstrap_cross_session_metadata_and_changed_branch_are_blocked(self):
        m = self.manager()
        m.bind('root', 'task')
        m.bind('other', 'other-task')
        target = self.repos / 'fresh'
        entry = m.ensure('task', str(target), create=True)
        path = Path(entry['path'])
        self.assertIsNotNone(m.guard_path('other-task', path / 'first.txt'))
        self.assertIsNotNone(m.guard_path('task', path / '.git' / 'config'))
        git(path, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.assertIsNotNone(m.guard_path('task', path / 'first.txt'))
        with self.assertRaises(RuntimeError):
            m.ensure('task', str(target))

    def test_bootstrap_concurrent_creation_reuses_one_directory(self):
        from concurrent.futures import ThreadPoolExecutor
        m = self.manager()
        m.bind('root', 'task')
        target = self.repos / 'fresh'
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: self.manager().ensure('task', str(target), create=True), range(4)))
        self.assertTrue(all(entry == results[0] for entry in results))
        self.assertEqual(len(m.entries('task')), 1)
        self.assertFalse(target.exists())

    def test_guard_protects_bare_repos_and_git_file_markers(self):
        m = self.manager()
        m.bind('root', 'task')
        bare = self.repos / 'bare.git'
        bare.mkdir()
        git(bare, 'init', '--bare')
        self.assertIsNotNone(m.guard_path('task', bare / 'config'))
        linked = self.repos / 'linked'
        linked.mkdir()
        (linked / '.git').write_text('gitdir: /missing/metadata')
        self.assertIsNotNone(m.guard_path('task', linked / 'file.txt'))
        self.assertIsNotNone(m.guard_path('task', self.repo / 'missing' / 'new.txt'))
        self.assertIsNone(m.guard_path('task', self.repos / 'not-a-repo' / 'new.txt'))

    def test_concurrent_creation_reuses_exactly_one_worktree(self):
        from concurrent.futures import ThreadPoolExecutor
        m = self.manager()
        m.bind('root', 'task')
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.manager().ensure('task', str(self.repo)), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(m.entries('task')), 1)


if __name__ == '__main__':
    unittest.main()
