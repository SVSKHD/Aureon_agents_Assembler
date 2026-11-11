"""The coding agent's work: reading allowlisted repos and running code tasks.

Reads go through git's object store (`git ls-tree`, `git show`) at a ref, so the working
tree, untracked files and symlinks never matter, and blocked paths (.git, .env, keys) are
filtered out. A `change` task:

1. creates a worktree at CODE_WORKTREE_ROOT/<task> on a new `aureon/task-<id>` branch from
   the repo's base branch (git hooks disabled);
2. asks the local Ollama coding model for a unified diff over the files it was shown;
3. validates the diff (paths, size, no binaries/symlinks/modes) and re-checks every path
   inside the worktree with symlinks resolved, so it cannot write through a link;
4. applies it in the worktree only, commits exactly those files, and runs the repo's
   allowlisted test commands there with a scrubbed environment;
5. waits for an admin: approve keeps the branch (optionally also as `feature/...`) through
   a `task` git op the coding worker runs (for a GitHub clone it also pushes the branch and
   opens a PR); reject deletes it. Nothing is ever merged or deployed.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coding import patch as patches
from app.agents.coding.commands import DEFAULT_ALLOWED, check_allowed, run_allowed
from app.agents.coding.git import TASK_BRANCH, Git, GitError
from app.agents.coding.paths import PathRejected, is_blocked, normalize, safe_path
from app.agents.coding.repos import BRANCH, RepoConfig, get_repo, parse_repos
from app.agents.ollama.client import OllamaClient
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.agents import CodeGitOp, CodeTask
from app.services import coding_git
from app.services.audit import ClientInfo
from app.services.coding_git import clone_repos

log = get_logger(__name__)

SHA = re.compile(r"^[0-9a-f]{7,40}$")
FEATURE = re.compile(r"^(feature|fix|aureon)/[A-Za-z0-9._-]{1,80}$")
MAX_LISTING = 400


class CodingError(RuntimeError):
    """A task-level failure with a message that is safe to show."""


@dataclass(frozen=True, slots=True)
class CodingConfig:
    repos: dict[str, RepoConfig]
    allowed: tuple[tuple[str, ...], ...]
    protected: frozenset[str]
    worktree_root: Path
    model: str | None
    model_timeout: float
    test_timeout: float
    max_context: int
    max_file: int
    max_patch: int

    @classmethod
    def from_settings(cls, s: Settings) -> CodingConfig:
        # Clones of allowlisted GitHub repos (coding_git) are coding repos too.
        repos = {**clone_repos(s), **parse_repos(s.CODE_REPOS)}  # CODE_REPOS wins on a clash
        allowed = tuple(tuple(a) for a in s.CODE_ALLOWED_COMMANDS) or DEFAULT_ALLOWED
        for r in repos.values():
            for argv in r.tests:
                check_allowed(argv, allowed)  # a repo cannot name a command outside the list
        return cls(
            repos=repos,
            allowed=allowed,
            protected=frozenset(s.CODE_PROTECTED_BRANCHES),
            worktree_root=s.CODE_WORKTREE_ROOT,
            model=s.CODE_MODEL if s.AI_LOCAL_ENABLED else None,  # local Ollama only
            model_timeout=s.CODE_MODEL_TIMEOUT_SECONDS,
            test_timeout=s.CODE_TEST_TIMEOUT_SECONDS,
            max_context=s.CODE_MAX_CONTEXT_BYTES,
            max_file=s.CODE_MAX_FILE_BYTES,
            max_patch=s.CODE_MAX_PATCH_BYTES,
        )


def check_ref(ref: str) -> str:
    if not (SHA.match(ref) or (BRANCH.match(ref) and ".." not in ref and "@{" not in ref)):
        raise PathRejected("invalid ref")
    return ref


# --- read-only repo access (GET /api/repos/...) --------------------------------------------


async def repo_summary(repo: RepoConfig) -> dict[str, Any]:
    git = Git(repo.path)
    out: dict[str, Any] = {
        "name": repo.name,
        "base": repo.base,
        "tests": [list(t) for t in repo.tests],
        "available": False,
    }
    if not (repo.path / ".git").exists():
        out["detail"] = "checkout not found"
        return out
    try:
        out["head"] = await git.head(repo.base)
        out["branches"] = len(await git.branches())
        out["available"] = True
    except GitError as exc:
        out["detail"] = str(exc)[:200]
    return out


async def tree(repo: RepoConfig, ref: str, path: str) -> list[dict[str, str]]:
    clean = normalize(path)
    items = await Git(repo.path).ls_tree(check_ref(ref), clean)
    out = []
    for it in items:
        full = f"{clean}/{it['name']}" if clean else it["name"]
        if is_blocked(full.lower()):
            continue
        out.append({**it, "path": full})
    return out


async def read_file(repo: RepoConfig, ref: str, path: str, max_bytes: int) -> dict[str, Any]:
    clean = normalize(path)
    if not clean:
        raise PathRejected("a file path is required")
    data = await Git(repo.path).show(check_ref(ref), clean)
    binary = b"\x00" in data[:8000]
    truncated = len(data) > max_bytes
    return {
        "path": clean,
        "ref": ref,
        "size": len(data),
        "binary": binary,
        "truncated": truncated and not binary,
        "content": None if binary else data[:max_bytes].decode("utf-8", "replace"),
    }


async def commits(repo: RepoConfig, ref: str, path: str, limit: int) -> list[dict[str, str]]:
    return await Git(repo.path).log(check_ref(ref), limit, normalize(path))


async def branches(repo: RepoConfig) -> list[str]:
    return await Git(repo.path).branches()


# --- model ------------------------------------------------------------------------------------

SYSTEM_ANALYZE = (
    "You are a senior engineer explaining a codebase to its owner. Use only the files and "
    "listing provided; say plainly when something is not in them. Be concise and concrete: "
    "name files and functions."
)
SYSTEM_CHANGE = (
    "You are a careful senior engineer. Make the requested change to the files provided. "
    "Reply with ONE unified diff in a ```diff fenced block, with paths relative to the "
    "repository root using a/ and b/ prefixes, followed by a short explanation. Change only "
    "what the instruction needs. Never touch .git, .env files, keys, lockfiles or CI "
    "configuration, and never add binary files or symlinks."
)


async def ask_model(
    ollama: OllamaClient, model: str, system: str, prompt: str, *, limit_s: float
) -> str:
    parts: list[str] = []

    async def collect() -> None:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        async for chunk in ollama.chat_stream(model, msgs, {"temperature": 0.1}):
            parts.append(str((chunk.get("message") or {}).get("content") or ""))
            if chunk.get("done"):
                break

    async with asyncio.timeout(limit_s):
        await collect()
    return "".join(parts).strip()


async def gather_context(
    git: Git, ref: str, paths: list[str], max_bytes: int
) -> tuple[str, list[str]]:
    """A file listing plus the contents of the requested files, within max_bytes."""
    listing = [f for f in await git.files(ref) if not is_blocked(f.lower())]
    shown: list[str] = []
    budget = max_bytes
    blocks = []
    for p in paths:
        clean = normalize(p)
        try:
            data = await git.show(ref, clean)
        except GitError:
            blocks.append(f"### {clean}\n(not found at {ref[:12]})\n")
            continue
        if b"\x00" in data[:8000]:
            continue
        text = data.decode("utf-8", "replace")
        if len(text) > budget:
            text = text[: max(0, budget)] + "\n…[truncated]\n"
        budget -= len(text)
        blocks.append(f"### {clean}\n```\n{text}\n```\n")
        shown.append(clean)
        if budget <= 0:
            break
    head = "\n".join(listing[:MAX_LISTING])
    more = f"\n…and {len(listing) - MAX_LISTING} more files" if len(listing) > MAX_LISTING else ""
    return f"Repository files:\n{head}{more}\n\n" + "\n".join(blocks), shown


# --- tasks ------------------------------------------------------------------------------------


def task_branch(task_id: uuid.UUID) -> str:
    return f"aureon/task-{task_id.hex[:12]}"


async def claim(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    async with maker() as db, db.begin():
        tid = await db.scalar(
            select(CodeTask.id)
            .where(CodeTask.status == "queued")
            .order_by(CodeTask.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if tid is None:
            return None
        await db.execute(
            update(CodeTask)
            .where(CodeTask.id == tid)
            .values(status="running", started_at=func.now())
        )
    return tid


async def _set(maker: async_sessionmaker[AsyncSession], tid: uuid.UUID, **values: Any) -> None:
    async with maker() as db, db.begin():
        await db.execute(update(CodeTask).where(CodeTask.id == tid).values(**values))


async def _cleanup(git: Git, worktree: Path, branch: str | None, *, delete_branch: bool) -> None:
    try:
        if await asyncio.to_thread(worktree.exists):
            await git.worktree_remove(worktree)
    except GitError:
        shutil.rmtree(worktree, ignore_errors=True)
        await git.run("worktree", "prune")
    if delete_branch and branch:
        try:
            await git.branch_delete(branch)
        except GitError:
            pass


async def run_task(
    maker: async_sessionmaker[AsyncSession],
    cfg: CodingConfig,
    ollama: OllamaClient | Any,
    tid: uuid.UUID,
) -> str:
    """Execute one claimed task; returns the final status."""
    async with maker() as db:
        task = await db.get(CodeTask, tid)
        assert task is not None
    worktree: Path | None = None
    branch: str | None = None
    git: Git | None = None
    try:
        if not cfg.model:
            raise CodingError(
                "CODE_MODEL is not set or local models are off (the local Ollama coding model)"
            )
        repo = get_repo(cfg.repos, task.repo)
        git = Git(repo.path)
        base_sha = await git.head(check_ref(task.base_ref))
        await _set(maker, tid, base_sha=base_sha, model=cfg.model)
        context, shown = await gather_context(git, base_sha, list(task.paths), cfg.max_context)

        if task.kind == "analyze":
            answer = await ask_model(
                ollama,
                cfg.model,
                SYSTEM_ANALYZE,
                f"{context}\n\nQuestion:\n{task.instruction}",
                limit_s=cfg.model_timeout,
            )
            await _set(
                maker, tid, status="ready", finished_at=func.now(),
                result={"answer": answer, "files": shown},
            )  # fmt: skip
            return "ready"

        branch = task_branch(tid)
        worktree = cfg.worktree_root / tid.hex
        cfg.worktree_root.mkdir(parents=True, exist_ok=True)
        await git.worktree_add(worktree, branch, base_sha)
        await _set(maker, tid, branch=branch, worktree=str(worktree))
        for d in repo.link:  # reuse installed deps so tests run offline; never committed
            src = repo.path / d
            dst = worktree / d
            if src.is_dir() and not dst.exists():
                dst.symlink_to(src, target_is_directory=True)

        answer = await ask_model(
            ollama,
            cfg.model,
            SYSTEM_CHANGE,
            f"{context}\n\nChange to make:\n{task.instruction}",
            limit_s=cfg.model_timeout,
        )
        try:
            patch = patches.validate(patches.extract(answer), cfg.max_patch)
            for f in patch.files:  # resolves symlinks: no writing through node_modules -> main
                safe_path(worktree, f)
                if f.split("/", 1)[0] in repo.link:
                    raise PathRejected(f"{f} is inside a linked directory")
            await git.apply(worktree, patch.text, check=True)
        except (patches.PatchRejected, PathRejected, GitError) as exc:
            raise CodingError(f"patch refused: {exc}") from exc
        await git.apply(worktree, patch.text, check=False)
        await git.run("add", "--", *patch.files, cwd=worktree)
        commit = await git.commit_all_staged(
            worktree, f"aureon: {task.title}\n\n{task.instruction}"
        )
        stat = await git.diff_stat(base_sha, branch)

        results = []
        home = cfg.worktree_root / f"{tid.hex}.home"
        home.mkdir(exist_ok=True)
        for argv in repo.tests:
            r = await run_allowed(
                argv, allowed=cfg.allowed, cwd=worktree, limit_s=cfg.test_timeout, home=home
            )
            results.append(r.as_dict())
        shutil.rmtree(home, ignore_errors=True)
        await _set(
            maker,
            tid,
            status="ready",
            commit_sha=commit,
            finished_at=func.now(),
            tests=results,
            result={
                "explanation": answer[answer.rfind("```") + 3 :].strip()[:4000],
                "files": list(patch.files),
                "diff_stat": stat,
                "patch": patch.text,
                "shown": shown,
            },
        )
        return "ready"
    except Exception as exc:
        msg = (
            str(exc)
            if isinstance(exc, CodingError | PathRejected | GitError)
            else (f"{type(exc).__name__}: {exc}")
        )
        log.warning("code_task_failed", task=str(tid), error=msg[:200])
        if git is not None and worktree is not None:
            await _cleanup(git, worktree, branch, delete_branch=True)
        await _set(
            maker, tid, status="failed", error=msg[:2000], finished_at=func.now(),
            worktree=None, branch=None,
        )  # fmt: skip
        return "failed"


class DecisionRefused(ValueError):
    pass


async def approve(
    db: AsyncSession,
    settings: Settings,
    task: CodeTask,
    user_id: uuid.UUID,
    feature: str | None,
    client: ClientInfo,
) -> CodeGitOp:
    """Record the approval and queue its git work for the coding worker (D-24-12): keep the
    task branch (optionally also as feature/...), remove the worktree, and for a clone of an
    allowlisted GitHub repo push the branch and open a PR. Runs no git here (the API image
    has none). Never touches a protected branch, never merges, never deploys."""
    cfg = CodingConfig.from_settings(settings)
    if task.kind != "change" or task.status != "ready":
        raise DecisionRefused(f"only a ready change task can be approved (status {task.status})")
    if not task.branch or not TASK_BRANCH.match(task.branch) or task.branch in cfg.protected:
        raise DecisionRefused("task has no agent branch")
    if not task.commit_sha:
        raise DecisionRefused("task has no commit")
    failed = [t for t in task.tests if not t.get("ok")]
    if failed:
        raise DecisionRefused(f"{len(failed)} test command(s) failed; fix and re-run instead")
    get_repo(cfg.repos, task.repo)
    if feature and (not FEATURE.match(feature) or feature in cfg.protected):
        raise DecisionRefused("feature branch must look like feature/<name> or fix/<name>")
    branch = feature or task.branch
    op = await coding_git.queue_task(
        db, settings, task, branch=branch, feature=feature, user_id=user_id, client=client
    )
    task.status = "approved"
    task.decided_by = user_id
    task.decided_at = datetime.now(UTC)
    task.result = {**task.result, "feature_branch": branch, "git_op_id": str(op.id)}
    return op


async def reject(
    db: AsyncSession, cfg: CodingConfig, task: CodeTask, user_id: uuid.UUID
) -> CodeTask:
    if task.status not in ("ready", "failed"):
        raise DecisionRefused(f"cannot reject a task that is {task.status}")
    if task.kind == "change":
        repo = get_repo(cfg.repos, task.repo)
        git = Git(repo.path)
        if task.worktree:
            await _cleanup(git, Path(task.worktree), task.branch, delete_branch=True)
        elif task.branch:
            await _cleanup(git, Path("/nonexistent"), task.branch, delete_branch=True)
    task.status = "rejected"
    task.decided_by = user_id
    task.decided_at = datetime.now(UTC)
    task.worktree = None
    return task


async def task_diff(cfg: CodingConfig, task: CodeTask) -> str:
    if task.kind != "change":
        return ""
    if task.branch and task.base_sha and task.status in ("ready", "approved"):
        try:
            return await Git(get_repo(cfg.repos, task.repo).path).diff(task.base_sha, task.branch)
        except GitError:
            pass
    return str(task.result.get("patch") or "")
