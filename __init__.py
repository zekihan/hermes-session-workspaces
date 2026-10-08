"""Hermes native plugin: session-scoped, multi-repository worktrees."""
import json
import logging
import os
import re
from pathlib import Path

from .workspace import Manager, WorkspaceError

log = logging.getLogger(__name__)
PATH_TOOLS = {'read_file', 'write_file', 'patch', 'search_files'}
SCHEMA = {
    'name': 'workspace_repo',
    'description': 'Create or reuse this chat\'s isolated repository worktree from fetched origin/main. '
                   'Call before accessing a repository. Children inherit the root chat workspace. '
                   'Use create=true only for a new nonexistent repo path: initializes an isolated unborn repo, '
                   'without origin/main. Use the returned absolute path for file tools and terminal workdir. '
                   'Does not commit or push.',
    'parameters': {'type': 'object', 'properties': {
        'repo': {'type': 'string', 'description': 'Absolute existing or intended repository path under configured roots'},
        'create': {'type': 'boolean', 'default': False,
                   'description': 'Explicitly bootstrap a new isolated repo; requested path must not exist'}},
        'required': ['repo'], 'additionalProperties': False},
}


def runtime(task_id, kwargs):
    """Resolve the same local file-tool anchor as Hermes; reject remote backends."""
    backend = kwargs.get('env_type')
    cwd = kwargs.get('cwd')
    if backend is None or cwd is None:
        try:
            from tools.file_tools_paths import _terminal_env_type_for_task, _resolve_base_dir
            backend = backend or _terminal_env_type_for_task(task_id)
            if backend == 'local':
                cwd = cwd or str(_resolve_base_dir(task_id))
        except ImportError:
            backend = backend or os.environ.get('TERMINAL_ENV', 'local')
            cwd = cwd or os.environ.get('TERMINAL_CWD') or os.getcwd()
    if backend != 'local':
        raise WorkspaceError('session-workspaces supports only the local terminal backend')
    if not cwd or not Path(cwd).expanduser().is_absolute():
        raise WorkspaceError('Cannot safely resolve the session working directory')
    return Path(cwd).expanduser().resolve()


class Plugin:
    def __init__(self, manager):
        self.manager = manager
        self.errors = {}

    def pre_llm(self, session_id='', task_id=None, parent_session_id='', **kwargs):
        identity = session_id or task_id
        try:
            self.manager.bind(session_id, task_id, parent_session_id or None)
            self.errors.pop(identity, None)
            entries = self.manager.entries(identity)
            return {'context': 'Session workspace: ' + self.manager.workspace_id(identity) + '. '
                    'Before accessing an existing repository under the configured roots, call workspace_repo '
                    'with its original absolute repository path. Use ONLY the returned worktree path, '
                    'including terminal workdir. Never edit/switch/reset the original checkout or main/master. '
                    'Subagents inherit this workspace; coordinate parallel edits to shared files. '
                    'Do not use Hermes\' separate worktree isolation for these same repositories. '
                    'Worktrees start from fetched origin/' + self.manager.base_branch + '; missing refs/fetch failures '
                    'must stop the task. Existing worktrees are reused without pulling or resetting. '
                    'For a new repository, call workspace_repo with create=true and a nonexistent intended path; '
                    'work only in its returned isolated directory. Repository-parent/non-repo paths permit cloning; '
                    'once cloned, use workspace_repo normally. No automatic commit, push, merge, or cleanup. '
                    'Mappings: ' + json.dumps(entries)}
        except Exception as exc:
            self.errors[identity] = str(exc) if isinstance(exc, WorkspaceError) else 'Workspace initialization failed'
            return {'context': 'WORKSPACE ERROR: ' + self.errors[identity] + '. Do not access repositories.'}

    def tool(self, args, task_id=None, session_id=None, **kwargs):
        identity = session_id or task_id
        try:
            if identity in self.errors:
                raise WorkspaceError(self.errors[identity])
            runtime(task_id or session_id, kwargs)
            entry = self.manager.ensure(identity, args['repo'], create=args.get('create', False))
            return json.dumps({'success': True, 'workspace': entry})
        except Exception as exc:
            message = str(exc) if isinstance(exc, WorkspaceError) else 'Workspace operation failed'
            return json.dumps({'success': False, 'error': message})

    def pre_tool(self, tool_name, args, task_id=None, session_id=None, **kwargs):
        if tool_name not in PATH_TOOLS | {'terminal', 'workspace_repo'}:
            return None
        identity = session_id or task_id
        try:
            if identity in self.errors:
                raise WorkspaceError(self.errors[identity])
            self.manager.root_session(identity)
            cwd = runtime(task_id or session_id, kwargs)
            if tool_name == 'workspace_repo':
                return None  # The manager validates the original repo and performs the fetch.
            paths = []
            if tool_name == 'terminal':
                workdir = Path(args.get('workdir') or cwd).expanduser()
                if not workdir.is_absolute():
                    workdir = cwd / workdir
                paths.append(workdir)
                # Conservative literal-path checks, NOT a shell interpreter/sandbox.
                paths.extend(re.findall(r'(?:~?/)[^\s\"\'`;|&()<>]+', args.get('command', '')))
            else:
                for key in ('path', 'filepath', 'file_path'):
                    if args.get(key):
                        paths.append(args[key])
                if tool_name == 'search_files' and not paths:
                    paths.append('.')
                if tool_name == 'patch':
                    paths.extend(re.findall(r'^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$',
                                            args.get('patch', ''), re.MULTILINE))
            for raw in paths:
                path = Path(raw).expanduser()
                if not path.is_absolute():
                    path = cwd / path
                reason = self.manager.guard_path(identity, path)
                if reason:
                    return {'action': 'block', 'message': 'session-workspaces: ' + reason + ': ' + str(path)}
            return None
        except Exception as exc:
            message = str(exc) if isinstance(exc, WorkspaceError) else 'Workspace guard failed'
            return {'action': 'block', 'message': 'session-workspaces: ' + message}


def register(ctx):
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except ImportError:
        home = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))
    manager = Manager(ctx.get_config('state_dir', str(home / 'session-workspaces')),
                      ctx.get_config('roots', []),
                      ctx.get_config('workspace_root', str(home / 'workspaces')),
                      ctx.get_config('base_branch', 'main'),
                      ctx.get_config('git_timeout', 20))
    plugin = Plugin(manager)
    ctx.register_tool(name='workspace_repo', toolset='session_workspaces', schema=SCHEMA, handler=plugin.tool)
    ctx.register_hook('pre_llm_call', plugin.pre_llm)
    ctx.register_hook('pre_tool_call', plugin.pre_tool)
