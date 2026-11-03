"""Coding agent endpoints (admin only, audited): read allowlisted repos, ask the local
model about code, and run patch tasks in isolated worktrees. See docs/05-agents.md.

Nothing here can merge, deploy, or run a command outside CODE_ALLOWED_COMMANDS. The only push
is the git-ops one (`/api/code/git/push`): a feature branch of an allowlisted repo, run by the
coding worker, followed by a PR; merging stays behind the approval gate.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from app.agents.coding.git import GitError
from app.agents.coding.paths import PathRejected, normalize
from app.agents.coding.repos import RepoConfig, RepoNotAllowed, get_repo
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.core.config import Settings
from app.models.agents import CodeGitOp, CodeTask
from app.schemas.code import (
    AnalyzeIn,
    ApproveIn,
    CodeTaskOut,
    CommitOut,
    DiffOut,
    FileOut,
    GitOpOut,
    GitPushIn,
    GitRepoIn,
    GitRepoStateOut,
    RepoOut,
    TaskIn,
    TreeItem,
)
from app.schemas.common import Page
from app.services import audit, coding, coding_git
from app.services.audit import ClientInfo

router = APIRouter(tags=["code"])
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def _cfg(settings: Settings) -> coding.CodingConfig:
    try:
        return coding.CodingConfig.from_settings(settings)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "coding_misconfigured", "detail": str(exc)[:300]},
        ) from exc


def _repo(cfg: coding.CodingConfig, name: str) -> RepoConfig:
    try:
        return get_repo(cfg.repos, name)
    except RepoNotAllowed as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not allowlisted") from exc


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, PathRejected):
        return HTTPException(422, detail={"code": "path_rejected", "detail": str(exc)})
    return HTTPException(404, detail={"code": "not_found", "detail": str(exc)[:200]})


# --- repos (read-only) -------------------------------------------------------------------


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(_: AdminRequired, settings: SettingsDep) -> list[RepoOut]:
    cfg = _cfg(settings)
    return [RepoOut.model_validate(await coding.repo_summary(r)) for r in cfg.repos.values()]


@router.get("/repos/{name}/branches", response_model=list[str])
async def repo_branches(name: str, _: AdminRequired, settings: SettingsDep) -> list[str]:
    try:
        return await coding.branches(_repo(_cfg(settings), name))
    except GitError as exc:
        raise _bad(exc) from exc


@router.get("/repos/{name}/tree", response_model=list[TreeItem])
async def repo_tree(
    name: str,
    _: AdminRequired,
    settings: SettingsDep,
    ref: str | None = None,
    path: str = "",
) -> list[TreeItem]:
    repo = _repo(_cfg(settings), name)
    try:
        items = await coding.tree(repo, ref or repo.base, path)
    except (PathRejected, GitError) as exc:
        raise _bad(exc) from exc
    return [TreeItem.model_validate(i) for i in items]


@router.get("/repos/{name}/file", response_model=FileOut)
async def repo_file(
    name: str,
    _: AdminRequired,
    settings: SettingsDep,
    path: Annotated[str, Query(min_length=1, max_length=1000)],
    ref: str | None = None,
) -> FileOut:
    cfg = _cfg(settings)
    repo = _repo(cfg, name)
    try:
        return FileOut.model_validate(
            await coding.read_file(repo, ref or repo.base, path, cfg.max_file)
        )
    except (PathRejected, GitError) as exc:
        raise _bad(exc) from exc


@router.get("/repos/{name}/commits", response_model=list[CommitOut])
async def repo_commits(
    name: str,
    _: AdminRequired,
    settings: SettingsDep,
    ref: str | None = None,
    path: str = "",
    limit: Limit = 30,
) -> list[CommitOut]:
    repo = _repo(_cfg(settings), name)
    try:
        return [
            CommitOut.model_validate(c)
            for c in await coding.commits(repo, ref or repo.base, path, limit)
        ]
    except (PathRejected, GitError) as exc:
        raise _bad(exc) from exc


# --- tasks --------------------------------------------------------------------------------


def _check_paths(paths: list[str]) -> list[str]:
    try:
        return [normalize(p) for p in paths]
    except PathRejected as exc:
        raise _bad(exc) from exc


async def _queue(
    db: DbSession,
    request: Request,
    admin: AdminRequired,
    *,
    kind: str,
    repo: RepoConfig,
    title: str,
    instruction: str,
    paths: list[str],
    ref: str,
) -> CodeTaskOut:
    try:
        coding.check_ref(ref)
    except PathRejected as exc:
        raise _bad(exc) from exc
    task = CodeTask(
        kind=kind,
        repo=repo.name,
        title=title,
        instruction=instruction,
        paths=_check_paths(paths),
        base_ref=ref,
        created_by=admin.user.id,
    )
    db.add(task)
    await db.flush()
    audit.record(
        db,
        f"code_{kind}_queued",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"task": str(task.id), "repo": repo.name, "title": title, "paths": task.paths},
    )
    await db.commit()
    await db.refresh(task)
    return CodeTaskOut.model_validate(task)


@router.post("/code/analyze", response_model=CodeTaskOut, status_code=status.HTTP_202_ACCEPTED)
async def analyze(
    body: AnalyzeIn, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> CodeTaskOut:
    """Explain code with the local Ollama coding model (read-only; the answer arrives in the
    task's result)."""
    repo = _repo(_cfg(settings), body.repo)
    return await _queue(
        db,
        request,
        admin,
        kind="analyze",
        repo=repo,
        title=body.question[:200],
        instruction=body.question,
        paths=body.paths,
        ref=body.ref or repo.base,
    )


@router.post("/code/tasks", response_model=CodeTaskOut, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    body: TaskIn, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> CodeTaskOut:
    """Queue a change: a patch made in an isolated worktree on an aureon/task-* branch, then
    tested with the repo's allowlisted commands. Nothing reaches a real branch until approved."""
    repo = _repo(_cfg(settings), body.repo)
    return await _queue(
        db,
        request,
        admin,
        kind="change",
        repo=repo,
        title=body.title,
        instruction=body.instruction,
        paths=body.paths,
        ref=repo.base,
    )


@router.get("/code/tasks", response_model=Page[CodeTaskOut])
async def list_tasks(
    db: DbSession, _: AdminRequired, repo: str | None = None, limit: Limit = 50, offset: Offset = 0
) -> Page[CodeTaskOut]:
    from app.services.trading import count

    stmt = select(CodeTask)
    if repo:
        stmt = stmt.where(CodeTask.repo == repo)
    rows = (
        await db.scalars(stmt.order_by(CodeTask.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return Page[CodeTaskOut](
        items=[CodeTaskOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


async def _task(db: DbSession, task_id: uuid.UUID) -> CodeTask:
    task = await db.get(CodeTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return task


@router.get("/code/tasks/{task_id}", response_model=CodeTaskOut)
async def get_task(task_id: uuid.UUID, db: DbSession, _: AdminRequired) -> CodeTaskOut:
    return CodeTaskOut.model_validate(await _task(db, task_id))


@router.get("/code/tasks/{task_id}/diff", response_model=DiffOut)
async def get_diff(
    task_id: uuid.UUID, db: DbSession, _: AdminRequired, settings: SettingsDep
) -> DiffOut:
    task = await _task(db, task_id)
    return DiffOut(
        task_id=task.id,
        files=list(task.result.get("files") or []),
        diff=await coding.task_diff(_cfg(settings), task),
    )


@router.post("/code/tasks/{task_id}/approve", response_model=CodeTaskOut)
async def approve_task(
    task_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
    body: ApproveIn | None = None,
) -> CodeTaskOut:
    """Keep the tested change on its own branch (optionally also named feature/... or
    fix/...). Never main/dev01, never pushed, never deployed."""
    task = await _task(db, task_id)
    feature = body.feature_branch if body else None
    try:
        await coding.approve(db, _cfg(settings), task, admin.user.id, feature)
    except coding.DecisionRefused as exc:
        raise HTTPException(409, detail={"code": "not_approvable", "detail": str(exc)}) from exc
    except GitError as exc:
        raise HTTPException(409, detail={"code": "git_error", "detail": str(exc)[:300]}) from exc
    audit.record(
        db,
        "code_task_approved",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={
            "task": str(task.id),
            "repo": task.repo,
            "branch": task.branch,
            "feature_branch": feature,
            "commit": task.commit_sha,
        },
    )
    await db.commit()
    await db.refresh(task)
    return CodeTaskOut.model_validate(task)


@router.post("/code/tasks/{task_id}/reject", response_model=CodeTaskOut)
async def reject_task(
    task_id: uuid.UUID, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> CodeTaskOut:
    task = await _task(db, task_id)
    try:
        await coding.reject(db, _cfg(settings), task, admin.user.id)
    except coding.DecisionRefused as exc:
        raise HTTPException(409, detail={"code": "not_rejectable", "detail": str(exc)}) from exc
    audit.record(
        db,
        "code_task_rejected",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"task": str(task.id), "repo": task.repo, "branch": task.branch},
    )
    await db.commit()
    await db.refresh(task)
    return CodeTaskOut.model_validate(task)


# --- git ops: clone / pull / push + PR (run by the coding worker) -------------------------


def _git_cfg(settings: Settings) -> coding_git.GitOpsConfig:
    try:
        return coding_git.GitOpsConfig.from_settings(settings)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "coding_misconfigured", "detail": str(exc)[:300]},
        ) from exc


@router.get("/code/git/repos", response_model=list[GitRepoStateOut])
async def git_repos(_: AdminRequired, settings: SettingsDep) -> list[GitRepoStateOut]:
    """GITHUB_CODING_REPO_ALLOWLIST with each repo's clone state."""
    cfg = _git_cfg(settings)
    out = []
    for full in sorted(cfg.allowlist):
        path = cfg.clone_path(full)
        out.append(
            GitRepoStateOut(
                repo=full,
                cloned=(path / ".git").exists(),
                coding_repo=coding_git.clone_repo_name(full),
                default_branch=coding_git.head_branch(path),
                push_prefixes=list(cfg.prefixes),
            )
        )
    return out


async def _queue_git(
    db: DbSession,
    request: Request,
    admin: AdminRequired,
    settings: Settings,
    op: str,
    repo: str,
    **kw: str | None,
) -> GitOpOut:
    try:
        row = await coding_git.queue(
            db,
            settings,
            op=op,
            repo=repo,
            user_id=admin.user.id,
            client=ClientInfo.from_request(request),
            **kw,
        )
    except coding_git.GitOpRefused as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "detail": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "coding_misconfigured", "detail": str(exc)[:300]},
        ) from exc
    await db.commit()
    await db.refresh(row)
    return GitOpOut.model_validate(row)


@router.post("/code/git/clone", response_model=GitOpOut, status_code=status.HTTP_202_ACCEPTED)
async def git_clone(
    body: GitRepoIn, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> GitOpOut:
    """Queue a clone of an allowlisted repo into CODE_CLONE_ROOT/<owner>/<repo>."""
    return await _queue_git(db, request, admin, settings, "clone", body.repo)


@router.post("/code/git/pull", response_model=GitOpOut, status_code=status.HTTP_202_ACCEPTED)
async def git_pull(
    body: GitRepoIn, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> GitOpOut:
    """Queue a fetch + fast-forward of the clone's default branch."""
    return await _queue_git(db, request, admin, settings, "pull", body.repo)


@router.post("/code/git/push", response_model=GitOpOut, status_code=status.HTTP_202_ACCEPTED)
async def git_push(
    body: GitPushIn, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> GitOpOut:
    """Queue a push of one feature branch (never main/master/dev01, the default branch, a
    protected branch, or a name outside CODE_PUSH_BRANCH_PREFIXES) and a PR to the default
    branch. Merging stays behind the approval gate."""
    return await _queue_git(
        db, request, admin, settings, "push", body.repo,
        branch=body.branch, title=body.title, body=body.body,
    )  # fmt: skip


@router.get("/code/git/ops", response_model=Page[GitOpOut])
async def git_ops(
    db: DbSession, _: AdminRequired, repo: str | None = None, limit: Limit = 50, offset: Offset = 0
) -> Page[GitOpOut]:
    from app.services.trading import count

    stmt = select(CodeGitOp)
    if repo:
        stmt = stmt.where(CodeGitOp.repo == repo.lower())
    rows = (
        await db.scalars(stmt.order_by(CodeGitOp.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return Page[GitOpOut](
        items=[GitOpOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


@router.get("/code/git/ops/{op_id}", response_model=GitOpOut)
async def git_op(op_id: uuid.UUID, db: DbSession, _: AdminRequired) -> GitOpOut:
    row = await db.get(CodeGitOp, op_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "git op not found")
    return GitOpOut.model_validate(row)
