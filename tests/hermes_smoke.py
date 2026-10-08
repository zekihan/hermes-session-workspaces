"""Run with the Hermes installation's Python and HERMES_SOURCE set.

Uses the actual Hermes loader, hook dispatcher and tool registry. Only a
throwaway HERMES_HOME and local Git repositories are modified; no LLM calls.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
if os.environ.get('HERMES_SOURCE'):
    sys.path.insert(0, os.environ['HERMES_SOURCE'])
    import hermes_bootstrap  # noqa: F401
sys.path.insert(0, str(SOURCE / 'tests'))
git = importlib.import_module('test_manager').git

with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', SOURCE)) as temp:
    root = Path(temp)
    home = root / 'home'
    (home / 'plugins').mkdir(parents=True)
    (home / 'plugins' / 'session-workspaces').symlink_to(SOURCE, target_is_directory=True)
    os.environ['HERMES_HOME'] = str(home)
    os.environ['TERMINAL_ENV'] = 'local'
    os.environ['TERMINAL_CWD'] = str(root)
    repos = root / 'repos'
    repos.mkdir()
    env = dict(os.environ)
    for key, value in [
        ('plugins.enabled', '["session-workspaces"]'),
        ('plugins.entries.session-workspaces.settings.roots', json.dumps([str(repos)])),
    ]:
        result = subprocess.run(['hermes', 'config', 'set', key, value], env=env,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Isolated Hermes configuration failed: ' + result.stderr)
    remote = root / 'remote.git'
    remote.mkdir()
    git(remote, 'init', '--bare', '--initial-branch=main')
    repo = repos / 'alpha'
    git(repos, 'clone', str(remote), str(repo))
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'file.txt').write_text('original\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'initial')
    git(repo, 'push', '-u', 'origin', 'main')
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    manager = PluginManager()
    manager.discover_and_load()
    note = manager.invoke_hook('pre_llm_call', session_id='smoke-root', task_id='smoke-task', parent_session_id='')
    assert any('Session workspace:' in entry.get('context', '') for entry in note), note
    first = json.loads(registry.dispatch('workspace_repo', {'repo': str(repo)}, task_id='smoke-task'))
    assert first['success'], first
    worktree = Path(first['workspace']['path'])
    manager.invoke_hook('pre_llm_call', session_id='smoke-child', task_id='smoke-child-task', parent_session_id='smoke-root')
    second = json.loads(registry.dispatch('workspace_repo', {'repo': str(repo)}, task_id='smoke-child-task'))
    assert second['workspace']['path'] == str(worktree), second
    blocked = manager.invoke_hook('pre_tool_call', tool_name='write_file', args={'path': str(repo / 'file.txt')},
                                  session_id='smoke-root', task_id='smoke-task', cwd=str(root), env_type='local')
    assert any(entry.get('action') == 'block' for entry in blocked), blocked
    allowed = manager.invoke_hook('pre_tool_call', tool_name='write_file', args={'path': str(worktree / 'file.txt')},
                                  session_id='smoke-root', task_id='smoke-task', cwd=str(root), env_type='local')
    assert not allowed, allowed
    # Exercise actual model_tools middleware, not merely the hook callback.
    from model_tools import handle_function_call
    result = json.loads(handle_function_call('write_file', {'path': str(repo / 'file.txt'), 'content': 'must not write'},
                        task_id='smoke-task', session_id='smoke-root'))
    assert result.get('error', '').startswith('session-workspaces:'), result
    assert (repo / 'file.txt').read_text() == 'original\n'
    fresh_target = repos / 'brand-new'
    fresh_result = registry.dispatch('workspace_repo', {'repo': str(fresh_target), 'create': True},
                                     task_id='smoke-task')
    fresh = json.loads(fresh_result) if isinstance(fresh_result, str) else fresh_result
    assert fresh['success'] and fresh['workspace']['bootstrap'], fresh
    fresh_path = Path(fresh['workspace']['path'])
    assert not fresh_target.exists()
    written = json.loads(handle_function_call('write_file',
        {'path': str(fresh_path / 'README.md'), 'content': 'new project\n'},
        task_id='smoke-task', session_id='smoke-root'))
    assert not written.get('error'), written
    assert (fresh_path / 'README.md').read_text() == 'new project\n'
    git(fresh_path, 'config', 'user.name', 'Test')
    git(fresh_path, 'config', 'user.email', 'test@example.invalid')
    git(fresh_path, 'add', 'README.md')
    git(fresh_path, 'commit', '-m', 'initial')
    # Publishing is explicit, not part of the tool; keep the registered task branch.
    new_remote = root / 'new-remote.git'
    new_remote.mkdir()
    git(new_remote, 'init', '--bare', '--initial-branch=main')
    git(fresh_path, 'remote', 'add', 'origin', str(new_remote))
    git(fresh_path, 'push', 'origin', 'HEAD:main')
    assert git(new_remote, 'rev-parse', 'main') == git(fresh_path, 'rev-parse', 'HEAD')
    resumed_result = registry.dispatch('workspace_repo', {'repo': str(fresh_target)}, task_id='smoke-task')
    resumed = json.loads(resumed_result) if isinstance(resumed_result, str) else resumed_result
    assert resumed == fresh, resumed
    clone_path = repos / 'new-clone'
    clone_allowed = manager.invoke_hook('pre_tool_call', tool_name='terminal',
        args={'command': 'git clone ' + str(remote) + ' ' + str(clone_path), 'workdir': str(repos)},
        session_id='smoke-root', task_id='smoke-task', cwd=str(root), env_type='local')
    assert not clone_allowed, clone_allowed
    git(repos, 'clone', str(remote), str(clone_path))
    clone_blocked = json.loads(handle_function_call('write_file',
        {'path': str(clone_path / 'file.txt'), 'content': 'must not write'},
        task_id='smoke-task', session_id='smoke-root'))
    assert clone_blocked.get('error', '').startswith('session-workspaces:'), clone_blocked
    assert (clone_path / 'file.txt').read_text() == 'original\n'
    print(json.dumps({'loader': 'passed', 'tool_dispatch': 'passed', 'child_inheritance': 'passed',
                      'original_path_block': 'passed', 'middleware_block': 'passed',
                      'worktree_path_allow': 'passed', 'original_unchanged': 'passed',
                      'bootstrap_write_commit_publish_resume': 'passed',
                      'clone_destination_allow': 'passed', 'cloned_checkout_block': 'passed'}, indent=2))
