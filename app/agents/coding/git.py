"""Fixed git invocations for the coding agent. Repo hooks never run (core.hooksPath is
pointed at an empty directory), nothing is pushed, and no ref outside `aureon/task-*` is
ever written."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

TASK_BRANCH = re.compile(r"^aureon/task-[0-9a-f]{8,32}$")
FEATURE_BRANCH = re.compile(r"^(feature|fix|aureon)/[A-Za-z0-9._-]{1,80}$")
AUTHOR = "Aureon coding agent <coding-agent@aureon.local>"
EMPTY_HOOKS = "/nonexistent-aureon-hooks"
# No user config, no credential helpers, no prompts: HOME points nowhere.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent-aureon-home",
}


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path, timeout: float = 120.0) -> None:
        self.repo = repo
        self.timeout = timeout

    async def run(self, *args: str, cwd: Path | None = None, stdin: bytes | None = None) -> str:
        cmd = [
            "git",
            "-c", f"core.hooksPath={EMPTY_HOOKS}",
            "-c", "commit.gpgsign=false",
            "-c", "core.quotePath=false",
            "-c", "protocol.allow=never",  # no fetch/push/clone from inside the agent
            *args,
        ]  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd or self.repo),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
        )
        try:
            async with asyncio.timeout(self.timeout):
                out, err = await proc.communicate(stdin)
        except TimeoutError as exc:
            proc.kill()
            raise GitError(f"git {args[0]} timed out") from exc
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[:400]}")
        return out.decode("utf-8", "replace")

    # --- read-only -------------------------------------------------------------------
    async def head(self, ref: str = "HEAD") -> str:
        return (await self.run("rev-parse", "--verify", f"{ref}^{{commit}}")).strip()

    async def branches(self) -> list[str]:
        out = await self.run("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [b for b in out.splitlines() if b]

    async def log(self, ref: str, limit: int, path: str = "") -> list[dict[str, str]]:
        fmt = "%H%x1f%an%x1f%aI%x1f%s"
        args = ["log", f"-n{limit}", f"--format={fmt}", ref, "--"]
        if path:
            args.append(path)
        out = await self.run(*args)
        rows = []
        for line in out.splitlines():
            sha, author, when, subject = [*line.split("\x1f"), "", "", ""][:4]
            rows.append({"sha": sha, "author": author, "date": when, "subject": subject})
        return rows

    async def ls_tree(self, ref: str, path: str) -> list[dict[str, str]]:
        spec = f"{ref}:{path}" if path else f"{ref}:"
        out = await self.run("ls-tree", "-l", spec)
        items = []
        for line in out.splitlines():
            meta, name = line.split("\t", 1)
            mode, kind, _sha, size = meta.split()
            items.append({"name": name, "type": kind, "mode": mode, "size": size})
        return items

    async def show(self, ref: str, path: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", f"core.hooksPath={EMPTY_HOOKS}", "show", f"{ref}:{path}",
            cwd=str(self.repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=GIT_ENV,
        )  # fmt: skip
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise GitError(err.decode(errors="replace").strip()[:200] or "not found")
        return out

    async def files(self, ref: str) -> list[str]:
        out = await self.run("ls-tree", "-r", "--name-only", ref)
        return [f for f in out.splitlines() if f]

    # --- task worktrees (writes only aureon/task-* refs) -----------------------------
    async def worktree_add(self, path: Path, branch: str, base: str) -> None:
        if not TASK_BRANCH.match(branch):
            raise GitError("refusing a branch outside aureon/task-*")
        await self.run("worktree", "add", "-b", branch, str(path), base)

    async def worktree_remove(self, path: Path) -> None:
        await self.run("worktree", "remove", "--force", str(path))

    async def branch_delete(self, branch: str) -> None:
        if not TASK_BRANCH.match(branch):
            raise GitError("refusing to delete a branch outside aureon/task-*")
        await self.run("branch", "-D", branch)

    async def apply(self, worktree: Path, patch: str, *, check: bool) -> None:
        args = ["apply", "--whitespace=nowarn", "--recount"]
        if check:
            args.append("--check")
        await self.run(*args, "-", cwd=worktree, stdin=patch.encode())

    async def commit_all_staged(self, worktree: Path, message: str) -> str:
        """Commit exactly what was staged (the patch's files), never `add -A`."""
        await self.run(
            "-c", "user.name=Aureon coding agent",
            "-c", "user.email=coding-agent@aureon.local",
            "commit", "--no-verify", "-m", message, f"--author={AUTHOR}",
            cwd=worktree,
        )  # fmt: skip
        return (await self.run("rev-parse", "HEAD", cwd=worktree)).strip()

    async def create_branch(self, name: str, sha: str) -> None:
        """An approved task's feature branch: feature/*, fix/* or aureon/* only."""
        if not FEATURE_BRANCH.match(name):
            raise GitError("refusing a branch outside feature/*, fix/*, aureon/*")
        await self.run("branch", name, sha)

    async def diff(self, base: str, branch: str) -> str:
        return await self.run("diff", "--no-color", "--find-renames", f"{base}...{branch}")

    async def diff_stat(self, base: str, branch: str) -> str:
        return await self.run("diff", "--stat", "--no-color", f"{base}...{branch}")
