import importlib.util
import json
import sys
import unittest
from pathlib import Path

import test_manager as fixtures


def load_plugin():
    path = Path(__file__).parents[1]
    if not (path / '__init__.py').exists():
        raise AssertionError('Plugin hook adapter has not been implemented')
    spec = importlib.util.spec_from_file_location('session_workspaces_test', path / '__init__.py',
                                                 submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self, settings):
        self.settings = settings
        self.hooks = {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs


class PluginTests(fixtures.ManagerTests):
    # Reuse only Git fixture setup, not the manager tests themselves.
    def plugin(self):
        module = load_plugin()
        ctx = Context(dict(roots=[str(self.repos)], workspace_root=str(self.root / 'workspaces'),
                           state_dir=str(self.root / 'state')))
        module.register(ctx)
        ctx.hooks['pre_llm_call'](session_id='root', task_id='task', parent_session_id='')
        return ctx

    def test_hook_tool_flow_routes_child_to_same_workspace(self):
        ctx = self.plugin()
        tool = ctx.tools['workspace_repo']['handler']
        first = json.loads(tool({'repo': str(self.repo)}, task_id='task'))
        self.assertTrue(first['success'], first)
        ctx.hooks['pre_llm_call'](session_id='child', task_id='child-task', parent_session_id='root')
        child = json.loads(tool({'repo': str(self.repo)}, task_id='child-task'))
        self.assertEqual(first['workspace']['path'], child['workspace']['path'])
        note = ctx.hooks['pre_llm_call'](session_id='root', task_id='task', parent_session_id='')
        self.assertIn(first['workspace']['path'], note['context'])

    def test_create_bootstraps_isolated_repo_without_origin_and_resumes(self):
        ctx = self.plugin()
        tool = ctx.tools['workspace_repo']['handler']
        target = self.repos / 'new-project'
        first = json.loads(tool({'repo': str(target), 'create': True}, task_id='task'))
        self.assertTrue(first['success'], first)
        entry = first['workspace']
        path = Path(entry['path'])
        self.assertFalse(target.exists())
        self.assertTrue(entry['bootstrap'])
        self.assertEqual(fixtures.git(path, 'branch', '--show-current'), entry['branch'])
        self.assertEqual(fixtures.git(path, 'remote'), '')
        self.assertIsNone(ctx.hooks['pre_tool_call'](tool_name='write_file',
            args={'path': str(path / 'first.txt')}, task_id='task', cwd=str(self.root)))
        (path / 'first.txt').write_text('initial work')
        resumed = json.loads(tool({'repo': str(target)}, task_id='task'))
        self.assertEqual(first, resumed)
        ctx.hooks['pre_llm_call'](session_id='child', task_id='child-task', parent_session_id='root')
        child = json.loads(tool({'repo': str(target), 'create': True}, task_id='child-task'))
        self.assertEqual(first, child)
        self.assertEqual((path / 'first.txt').read_text(), 'initial work')

    def test_new_clone_destination_is_allowed_then_checkout_is_protected(self):
        ctx = self.plugin()
        hook = ctx.hooks['pre_tool_call']
        target = self.repos / 'new-clone'
        args = {'command': 'git clone ' + str(self.remote) + ' ' + str(target),
                'workdir': str(self.repos)}
        self.assertIsNone(hook(tool_name='terminal', args=args, session_id='root',
                               task_id='task', cwd=str(self.root)))
        fixtures.git(self.repos, 'clone', str(self.remote), str(target))
        result = hook(tool_name='write_file', args={'path': str(target / 'new.txt')},
                      session_id='root', task_id='task', cwd=str(self.root))
        self.assertEqual(result['action'], 'block')

    def test_original_file_guard_blocks_and_worktree_allows(self):
        ctx = self.plugin()
        hook = ctx.hooks['pre_tool_call']
        blocked = hook(tool_name='write_file', args={'path': str(self.repo / 'file.txt')},
                       session_id='root', task_id='task', cwd=str(self.root))
        self.assertEqual(blocked['action'], 'block')
        entry = json.loads(ctx.tools['workspace_repo']['handler']({'repo': str(self.repo)}, task_id='task'))
        allowed = hook(tool_name='write_file', args={'path': entry['workspace']['path'] + '/file.txt'},
                       session_id='root', task_id='task', cwd=str(self.root))
        self.assertIsNone(allowed)

    def test_terminal_original_path_and_cwd_are_blocked(self):
        ctx = self.plugin()
        hook = ctx.hooks['pre_tool_call']
        for args in [{'command': 'git status', 'workdir': str(self.repo)},
                     {'command': 'git -C ' + str(self.repo) + ' status', 'workdir': str(self.root)}]:
            self.assertEqual(hook(tool_name='terminal', args=args, task_id='task',
                                  session_id='root', cwd=str(self.root))['action'], 'block')

    def test_relative_file_paths_use_actual_cwd(self):
        ctx = self.plugin()
        result = ctx.hooks['pre_tool_call'](tool_name='patch', args={'path': 'file.txt'},
                                           task_id='task', session_id='root', cwd=str(self.repo))
        self.assertEqual(result['action'], 'block')

    def test_v4a_patch_paths_are_guarded(self):
        ctx = self.plugin()
        result = ctx.hooks['pre_tool_call'](tool_name='patch',
            args={'patch': '*** Begin Patch\n*** Update File: ' + str(self.repo / 'file.txt') + '\n@@\n-old\n+new\n*** End Patch'},
            task_id='task', session_id='root', cwd=str(self.root))
        self.assertEqual(result['action'], 'block')

    def test_unknown_session_cannot_create_worktree(self):
        ctx = self.plugin()
        result = json.loads(ctx.tools['workspace_repo']['handler']({'repo': str(self.repo)}, task_id='unknown'))
        self.assertFalse(result['success'])

    def test_shared_default_task_id_does_not_mix_session_workspaces(self):
        ctx = self.plugin()
        ctx.hooks['pre_llm_call'](session_id='one', task_id='default', parent_session_id='')
        ctx.hooks['pre_llm_call'](session_id='two', task_id='default', parent_session_id='')
        tool = ctx.tools['workspace_repo']['handler']
        one = json.loads(tool({'repo': str(self.repo)}, task_id='default', session_id='one'))
        two = json.loads(tool({'repo': str(self.repo)}, task_id='default', session_id='two'))
        self.assertTrue(one['success'], one)
        self.assertTrue(two['success'], two)
        self.assertNotEqual(one['workspace']['path'], two['workspace']['path'])
        ambiguous = json.loads(tool({'repo': str(self.repo)}, task_id='default'))
        self.assertFalse(ambiguous['success'])

    def test_remote_backend_is_explicitly_rejected(self):
        ctx = self.plugin()
        result = ctx.hooks['pre_tool_call'](tool_name='write_file', args={'path': '/workspace/file'},
            task_id='task', session_id='root', cwd='/workspace', env_type='docker')
        self.assertEqual(result['action'], 'block')


# Avoid counting inherited manager tests twice.
for name in list(vars(fixtures.ManagerTests)):
    if name.startswith('test_'):
        setattr(PluginTests, name, None)

if __name__ == '__main__':
    unittest.main()
