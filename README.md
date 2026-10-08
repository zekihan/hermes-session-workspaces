# Hermes Session Workspaces

A standalone Hermes Agent plugin for **one root-chat workspace name across every repository that chat touches**. Delegated children inherit the same name, including when they access additional repositories. No Hermes fork or core patch is needed.

Each repository gets its own worktree and branch, for example:

```text
$HERMES_HOME/workspaces/s-<root-chat-hash>/
├── argocd-<repo-hash>/       # branch agent/s-<root-chat-hash>
└── terraform-<repo-hash>/    # branch agent/s-<root-chat-hash>
```

The repository hash avoids collisions between repositories with identical basenames. The branch name is identical across repositories belonging to the same root chat.

## Behaviour

- `workspace_repo` lazily creates a repository's worktree from **freshly fetched `origin/main`**, using the resolved commit SHA. It never branches from local `HEAD` as a fallback.
- Original checkout files, index and checked-out branch are not changed by creation. Fetch updates shared remote-tracking refs; Git adds a task branch and worktree registration.
- Child and nested-child sessions inherit the root chat's identity through Hermes's `parent_session_id` hook payload.
- Mappings persist in profile-scoped SQLite storage. Resuming the **same Hermes session ID** reuses its dirty worktrees without fetching, pulling, recreating or resetting them.
- Tool hooks block configured original-checkout paths, other sessions' workspace paths, direct `.git` file access and access to session worktrees switched away from their registered branch.
- Missing `origin/main`, failed fetches, missing worktrees and unknown parents produce errors rather than selecting the main checkout.
- No automatic commits, pushes, merges, cleanup or branch deletion. Worktrees and changes survive session end.

## Install and configure

Requires Git 2.31+, Python 3.10+, and a Hermes version exposing `pre_llm_call` with `session_id`, `task_id`, `parent_session_id`, plus blocking `pre_tool_call` hooks and native plugin tools. Tested against the installed Hermes loader and middleware; older releases without these hook payloads are not supported. Linux/local-backend support only in this release.

Install into the profile you use. These examples target `dev`:

```bash
export HERMES_HOME=/home/hermes/.hermes/profiles/dev
hermes plugins install zekihan/hermes-session-workspaces --no-enable
hermes config set plugins.entries.session-workspaces.settings.roots '["/home/hermes/repos/github.com/zekihan"]'
hermes plugins enable session-workspaces
```

Restart the relevant Hermes process so the plugin is discovered. Installed plugins are not automatically active in other profiles. The toolset is `session_workspaces`; if you explicitly restrict toolsets, include it.

Use a fresh chat for the first trial. Avoid enabling other plugins that automatically commit/push these repositories while testing.

### Settings

Settings live under `plugins.entries.session-workspaces.settings` and are read at plugin load. Use `hermes config set`; do not edit YAML manually.

| Setting | Default | Meaning |
|---|---|---|
| `roots` | `[]` | List of absolute repository-parent directories. Explicit opt-in; no repository can be created without it. All paths beneath these roots are guarded, including discovery commands. |
| `workspace_root` | `$HERMES_HOME/workspaces` | Location for session worktrees; should be outside `roots`. |
| `state_dir` | `$HERMES_HOME/session-workspaces` | Persistent mapping database; should be outside `roots`. |
| `base_branch` | `main` | Branch fetched from `origin`. A repo without it fails; no automatic `master` fallback. This is a profile-wide setting. |
| `git_timeout` | `20` | Seconds per Git operation. |

Repository paths passed to `workspace_repo` must be absolute paths beneath `roots`, not remote URLs. Use the same configured base branch across the profile, or configure a separate profile for `master` repositories.

## Agent workflow

The plugin injects guidance into each turn. The agent calls:

```json
{"repo": "/home/hermes/repos/github.com/zekihan/argocd"}
```

The `workspace_repo` tool returns:

```text
success: true
workspace: {repo, path, branch, base_commit, workspace_id}
```

The agent uses `workspace.path` for all subsequent file paths and terminal `workdir`. When it needs another repository, it calls the tool again. Children call it the same way and get their parent's naming scope. No workspace/session identifier is exposed as an LLM-controlled tool argument.

Direct original-checkout access is **blocked with instructions to call the helper**, not silently rewritten. The plugin intentionally does not rewrite arbitrary shell commands or globally change process cwd.

Parent and children **share the same worktree for the same repository**. Coordinate file ownership or serialize editing tasks; this plugin does not provide per-file editing locks.

## Safety boundaries — please read

This is an accidental-edit guardrail, **not a filesystem sandbox**:

- File-tool paths (including relative paths, symlink resolution and V4A patch paths) are checked.
- Terminal cwd and literal absolute paths in commands are conservatively checked. Dynamically computed paths, scripts, environment expansion and commands executing elsewhere can bypass those checks. False-positive blocking is possible.
- Tools other than `read_file`, `write_file`, `patch`, `search_files`, `terminal` and `workspace_repo` are not intercepted. For example, external MCP tools, browser/computer access and direct Python filesystem writes are outside this guard.
- Shared Git metadata and local refs are not isolated by normal Git worktrees. Branch protection should be configured separately for remote `main`; this plugin does not police every possible Git command.
- Repository hooks, fsmonitor and checkout filters are disabled for manager Git calls. Git LFS/custom smudge filters are not materialized automatically. Normal agent-run Git commands retain their usual behaviour.
- Fetch may use your configured credential/SSH helper. Git error output is not returned to the model because remote URLs can contain credentials.
- Remote terminal backends are explicitly rejected for intercepted tools; the plugin does not pretend host paths refer to remote filesystems.
- Disable Hermes's separate `delegation.worktree_isolation` for this workflow; it uses child-specific worktrees instead of the shared root-chat mapping.
- If Hermes rotates a session ID rather than resuming it, it is a new workspace unless an inheritance mapping is present. `/new` starts a new workspace.

For hard isolation against an uncooperative agent, use OS/container restrictions. This plugin does not require them for normal cooperative coding workflows.

## Recovery and cleanup

Nothing is auto-deleted. Review and publish task branches with your normal test/PR workflow. After confirming work is preserved elsewhere, remove individual worktrees with Git yourself.

If a creation is interrupted after Git created the branch/tree but before SQLite recorded it, a retry refuses the existing branch rather than destroying it. Inspect `git worktree list` and the workspace directory. Back up any work, then resolve the orphan manually; do not reset or delete it blindly. Likewise, missing or switched recorded worktrees are errors, not recreation triggers. Keep the SQLite database with your worktrees when moving/backing up a profile.

## Tests

No third-party Python dependencies are needed for the plugin or its ordinary test suite:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

The suite uses real throwaway local Git remotes and checks fresh-base creation, dirty-original preservation, multi-repository child/grandchild inheritance, resume, concurrent creation, fetch failures, missing branches, path guards and smudge-filter suppression.

`tests/hermes_smoke.py` is an additional integration test for an installed Hermes runtime. Set `HERMES_SOURCE` to its source checkout and run the script with that installation's Python interpreter. It bootstraps Hermes dependencies, creates a throwaway profile with the supported config CLI, then exercises the actual plugin loader, registry dispatch, hook dispatcher and `model_tools` blocking middleware. No LLM/network calls, active-profile configuration changes or real project edits are needed.

CI runs the standalone suite and Ruff on Python 3.10, 3.13 and 3.14. The Hermes integration smoke test is local/opt-in because it needs an installed Hermes runtime.

MIT licensed.
