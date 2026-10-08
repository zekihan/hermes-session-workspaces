"""Persistent, profile-scoped session worktrees. No Hermes imports required."""
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    """An operation could not safely establish a session workspace."""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Manager:
    def __init__(self, state_dir, roots, workspace_root, base_branch='main', timeout=20):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.roots = [Path(root).expanduser().resolve() for root in roots]
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.base_branch = base_branch
        self.timeout = timeout
        self.db = self.state_dir / 'workspaces.sqlite3'
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (session TEXT PRIMARY KEY, root TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (task TEXT PRIMARY KEY, session TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspaces (
                    root TEXT NOT NULL, repo TEXT NOT NULL, path TEXT NOT NULL,
                    branch TEXT NOT NULL, base_commit TEXT NOT NULL,
                    PRIMARY KEY(root, repo));
            ''')

    def connect(self):
        return sqlite3.connect(self.db, timeout=60, factory=ClosingConnection)

    def git(self, repo, *args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0',
                   GIT_CONFIG_NOSYSTEM='1')
        try:
            options = ['-c', 'core.fsmonitor=', '-c', 'core.hooksPath=/dev/null',
                       '-c', 'protocol.ext.allow=never']
            if 'worktree' in args and 'add' in args:
                discovery = subprocess.run(
                    ['git', *options, '-C', str(repo), 'config', '--name-only', '--get-regexp',
                     r'^filter\..*\.(smudge|process|clean|required)$'],
                    capture_output=True, text=True, timeout=self.timeout, env=env,
                    stdin=subprocess.DEVNULL)
                if discovery.returncode not in (0, 1):
                    raise WorkspaceError('Cannot inspect checkout filters; refusing worktree creation')
                drivers = {key.rsplit('.', 1)[0] for key in discovery.stdout.splitlines()}
                for driver in sorted(drivers):
                    for operation in ('smudge', 'clean', 'process'):
                        options.extend(['-c', driver + '.' + operation + '='])
                    options.extend(['-c', driver + '.required=false'])
            result = subprocess.run(
                ['git', *options, '-C', str(repo), *args],
                capture_output=True, text=True, timeout=self.timeout, env=env,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError('Git unavailable or timed out; original checkout was not selected') from exc
        if result.returncode:
            # Git output can include credential-bearing URLs: do not expose it.
            raise WorkspaceError('Git operation failed: ' + args[0] + '; no checkout fallback')
        return result.stdout.strip()

    def bind(self, session_id, task_id=None, parent_session_id=None):
        if not isinstance(session_id, str) or not session_id or session_id == 'default':
            raise WorkspaceError('A real session ID is required')
        with self.connect() as conn:
            parent = conn.execute('SELECT root FROM sessions WHERE session=?',
                                  (parent_session_id,)).fetchone() if parent_session_id else None
            if parent_session_id and not parent:
                raise WorkspaceError('Unknown parent session; refusing a separate child workspace')
            root = parent[0] if parent else session_id
            conn.execute('INSERT OR IGNORE INTO sessions VALUES (?, ?)', (session_id, root))
            if task_id and task_id != 'default':
                conn.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?)', (task_id, session_id))
        return self.workspace_id(session_id)

    def root_session(self, identity):
        with self.connect() as conn:
            row = conn.execute('SELECT root FROM sessions WHERE session=?', (identity,)).fetchone()
            if not row:
                task = conn.execute('SELECT session FROM tasks WHERE task=?', (identity,)).fetchone()
                row = conn.execute('SELECT root FROM sessions WHERE session=?',
                                   (task[0],)).fetchone() if task else None
        if not row:
            raise WorkspaceError('Session not initialized; run a chat turn before workspace_repo')
        return row[0]

    def workspace_id(self, identity):
        return 's-' + hashlib.sha256(self.root_session(identity).encode()).hexdigest()[:20]

    def canonical_repo(self, path):
        path = Path(path).expanduser()
        if not path.is_absolute():
            raise WorkspaceError('Repository path must be absolute')
        path = path.resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise WorkspaceError('Repository is outside configured roots')
        repo = Path(self.git(path if path.is_dir() else path.parent,
                             'rev-parse', '--show-toplevel')).resolve()
        if not any(repo.is_relative_to(root) for root in self.roots):
            raise WorkspaceError('Repository root is outside configured roots')
        return repo

    def entries(self, identity):
        root = self.root_session(identity)
        with self.connect() as conn:
            rows = conn.execute('SELECT repo, path, branch, base_commit FROM workspaces WHERE root=? ORDER BY repo',
                                (root,)).fetchall()
        return [dict(repo=repo, path=path, branch=branch, base_commit=base,
                     workspace_id=self.workspace_id(identity)) for repo, path, branch, base in rows]

    def validate_worktree(self, repo, path, branch):
        if not Path(path).exists() or self.git(path, 'branch', '--show-current') != branch:
            raise WorkspaceError('Recorded worktree is missing or on another branch; refusing reset')
        common = self.git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        original = self.git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if Path(common).resolve() != Path(original).resolve():
            raise WorkspaceError('Recorded worktree belongs to a different repository; refusing reuse')

    def guard_path(self, identity, path):
        path = Path(path).expanduser().resolve()
        for entry in self.entries(identity):
            if path.is_relative_to(Path(entry['path'])):
                if '.git' in path.relative_to(Path(entry['path'])).parts:
                    return 'Direct access to Git metadata is not allowed'
                try:
                    self.validate_worktree(entry['repo'], entry['path'], entry['branch'])
                except WorkspaceError as exc:
                    return str(exc)
                return None
        if path.is_relative_to(self.workspace_root):
            return 'This path belongs to another workspace or is not registered'
        if any(path.is_relative_to(root) for root in self.roots):
            return 'Use workspace_repo for this repository, then use its returned worktree path'
        return None

    def ensure(self, identity, repo_path):
        repo = self.canonical_repo(repo_path)
        root = self.root_session(identity)
        wid = self.workspace_id(identity)
        # SQLite write transaction serializes creation across threads/processes.
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT path, branch, base_commit FROM workspaces WHERE root=? AND repo=?',
                               (root, str(repo))).fetchone()
            if row:
                path, branch, base = row
                self.validate_worktree(repo, path, branch)
            else:
                branch = 'agent/' + wid
                suffix = hashlib.sha256(str(repo).encode()).hexdigest()[:10]
                path = str(self.workspace_root / wid / (repo.name + '-' + suffix))
                ref = 'refs/remotes/origin/' + self.base_branch
                self.git(repo, 'check-ref-format', 'refs/heads/' + self.base_branch)
                self.git(repo, 'fetch', '--no-recurse-submodules', 'origin',
                         '+refs/heads/' + self.base_branch + ':' + ref)
                base = self.git(repo, 'rev-parse', '--verify', ref + '^{commit}')
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.git(repo, '-c', 'branch.autoSetupMerge=false', 'worktree', 'add',
                         '--no-track', '-b', branch, path, base)
                conn.execute('INSERT INTO workspaces VALUES (?, ?, ?, ?, ?)',
                             (root, str(repo), path, branch, base))
            conn.commit()
            return dict(repo=str(repo), path=path, branch=branch, base_commit=base,
                        workspace_id=wid)
        finally:
            conn.close()
