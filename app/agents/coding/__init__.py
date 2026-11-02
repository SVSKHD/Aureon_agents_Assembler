"""The restricted coding agent (Phase 5b). See docs/05-agents.md, "Coding agent".

Everything it touches is bounded:
- repos: only the allowlisted ones in CODE_REPOS (repos.py);
- files: only inside a repo, never .git or secrets (paths.py);
- commands: only exact argv matches from CODE_ALLOWED_COMMANDS, no shell (commands.py);
- git: fixed internal invocations with hooks disabled (git.py);
- changes: only inside a per-task worktree on an `aureon/task-*` branch, never main/dev01,
  never pushed, never deployed (worker + approve endpoint).
"""
