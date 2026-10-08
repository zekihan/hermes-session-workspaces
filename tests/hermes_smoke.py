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
    print(json.dumps({'loader': 'passed', 'tool_dispatch': 'passed', 'child_inheritance': 'passed',
                      'original_path_block': 'passed', 'middleware_block': 'passed',
                      'worktree_path_allow': 'passed', 'original_unchanged': 'passed'}, indent=2))
