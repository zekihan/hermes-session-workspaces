"""Binding integrity tests use only temporary SQLite state, without Git/services."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from test_manager import load_manager


class BindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = self.new_manager()
        # Preserve unrelated workspace records on every rejected transaction.
        with self.manager.connect() as conn:
            conn.execute('''INSERT INTO workspaces
                            (root, repo, path, branch, base_commit) VALUES (?, ?, ?, ?, ?)''',
                         ('unrelated', 'fixture-repo', 'fixture-path', 'fixture-branch', 'fixture-base'))

    def new_manager(self):
        return load_manager()(self.root / 'state', [], self.root / 'workspaces')

    def snapshot(self):
        with self.manager.connect() as conn:
            return {table: conn.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
                    for table in ('sessions', 'tasks', 'workspaces')}

    def assert_rejected_unchanged(self, message, *args, **kwargs):
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, message):
            self.manager.bind(*args, **kwargs)
        self.assertEqual(self.snapshot(), before)

    def parents(self):
        self.manager.bind('parent-a', 'task-a')
        self.manager.bind('parent-b', 'task-b')

    def test_idempotence_and_parentless_resume_across_instances(self):
        self.parents()
        wid = self.manager.bind('child', 'task', 'parent-a')
        before = self.snapshot()
        for parent in ('parent-a', None, ''):
            with self.subTest(parent=parent):
                self.assertEqual(self.new_manager().bind('child', 'task', parent), wid)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.new_manager().bind('child', 'resume-task'), wid)
        self.assertEqual(self.manager.root_session('resume-task'), 'parent-a')
        self.assertEqual(self.manager.bind('parent-a', 'task-a'), self.manager.workspace_id('parent-a'))

    def test_parent_conflict_does_not_add_task(self):
        self.parents()
        self.manager.bind('child', 'task', 'parent-a')
        self.assert_rejected_unchanged('different parent', 'child', 'new-task', 'parent-b')

    def test_different_immediate_parent_in_same_root_is_rejected(self):
        self.manager.bind('root')
        self.manager.bind('parent-a', parent_session_id='root')
        self.manager.bind('parent-b', parent_session_id='root')
        self.manager.bind('child', parent_session_id='parent-a')
        self.assert_rejected_unchanged('different parent', 'child', 'new-task', 'parent-b')

    def test_root_session_cannot_be_reparented(self):
        self.parents()
        self.assert_rejected_unchanged('different parent', 'parent-a', 'new-task', 'parent-b')
        self.assert_rejected_unchanged('own parent', 'parent-a', 'new-task', 'parent-a')

    def test_unknown_parent_rejects_new_and_resumed_sessions(self):
        self.parents()
        self.manager.bind('child', 'task', 'parent-a')
        for session in ('new', 'child'):
            with self.subTest(session=session):
                self.assert_rejected_unchanged('Unknown parent', session, 'new-task', 'missing')

    def test_task_conflict_rolls_back_new_session(self):
        self.parents()
        self.assert_rejected_unchanged('Task.*different session', 'new', 'task-a', 'parent-b')
        self.assert_rejected_unchanged('Task.*different session', 'new', 'task-a')
        self.assert_rejected_unchanged('Task.*different session', 'parent-b', 'task-a')

    def test_task_cannot_move_between_sessions_even_in_same_root(self):
        self.manager.bind('root', 'task')
        self.assert_rejected_unchanged('Task.*different session', 'child', 'task', 'root')

    def test_placeholder_tasks_remain_unmapped(self):
        for task in (None, '', 'default'):
            self.manager.bind('one', task)
            self.manager.bind('two', task)
        self.assertEqual(self.snapshot()['tasks'], [])

    def test_legacy_child_resume_and_parent_pinning_are_atomic(self):
        # Simulate the released schema, whose direct parent was never stored.
        with self.manager.connect() as conn:
            conn.execute('ALTER TABLE sessions DROP COLUMN parent')
            conn.executemany('INSERT INTO sessions VALUES (?, ?)',
                             [('root', 'root'), ('parent-a', 'root'),
                              ('parent-b', 'root'), ('child', 'root'), ('other', 'other')])
            conn.execute('INSERT INTO tasks VALUES (?, ?)', ('taken', 'other'))
        self.manager = self.new_manager()
        self.manager.bind('child', 'resume-task')
        self.assertEqual(self.manager.root_session('resume-task'), 'root')
        self.assert_rejected_unchanged('different parent', 'child', 'new-task', 'other')
        self.assert_rejected_unchanged('Task.*different session', 'child', 'taken', 'parent-a')
        with self.manager.connect() as conn:
            self.assertIsNone(conn.execute('SELECT parent FROM sessions WHERE session="child"').fetchone()[0])
        self.manager.bind('child', 'resume-task', 'parent-a')
        self.assert_rejected_unchanged('different parent', 'child', 'new-task', 'parent-b')

    def test_legacy_schema_migrates_concurrently(self):
        self.manager.bind('root', 'task')
        with self.manager.connect() as conn:
            conn.execute('ALTER TABLE sessions DROP COLUMN parent')
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.new_manager().bind('root', 'task'), range(4)))
        self.assertEqual(results, [self.manager.workspace_id('root')] * 4)
        self.assertEqual(self.snapshot()['sessions'], [('root', 'root', None)])

    def test_database_failure_rolls_back_session_and_task(self):
        with self.manager.connect() as conn:
            conn.execute('''CREATE TRIGGER fail_task AFTER INSERT ON tasks
                            BEGIN SELECT RAISE(ABORT, 'injected failure'); END''')
        before = self.snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected failure'):
            self.manager.bind('new', 'task')
        self.assertEqual(self.snapshot(), before)

    def race(self, bindings):
        managers = [self.new_manager() for _ in bindings]
        barrier = Barrier(len(bindings))

        def bind(item):
            manager, args = item
            barrier.wait(timeout=10)
            try:
                return manager.bind(*args)
            except RuntimeError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(bindings)) as executor:
            return list(executor.map(bind, zip(managers, bindings)))

    def test_concurrent_identical_bindings_all_succeed(self):
        self.manager.bind('parent')
        results = self.race([('child', 'task', 'parent')] * 8)
        self.assertEqual(results, [self.manager.workspace_id('parent')] * 8)
        self.assertEqual(self.snapshot()['tasks'], [('task', 'child')])

    def test_concurrent_conflicting_parents_have_one_winner(self):
        self.parents()
        results = self.race([('child', 'child-a', 'parent-a'), ('child', 'child-b', 'parent-b')])
        self.assertEqual(sum(isinstance(result, RuntimeError) for result in results), 1)
        winner = 0 if isinstance(results[0], str) else 1
        task, parent = [('child-a', 'parent-a'), ('child-b', 'parent-b')][winner]
        self.assertEqual(self.manager.root_session('child'), parent)
        self.assertEqual(self.snapshot()['tasks'],
                         sorted([('task-a', 'parent-a'), ('task-b', 'parent-b'), (task, 'child')]))

    def test_concurrent_task_claims_roll_back_losing_session(self):
        results = self.race([('one', 'task'), ('two', 'task')])
        self.assertEqual(sum(isinstance(result, RuntimeError) for result in results), 1)
        winner = 'one' if isinstance(results[0], str) else 'two'
        self.assertEqual(self.snapshot()['tasks'], [('task', winner)])
        self.assertEqual(self.snapshot()['sessions'], [(winner, winner, None)])
