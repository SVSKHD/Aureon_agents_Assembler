"""GitHub issues, issue <-> Spasta task links and tracked operations
(docs/19-github-issues-prs.md). Admin only; writes need CSRF. Repos outside the allowlist are
404 and GitHub is not called for them.

    GET    /api/v1/github/repositories/{owner}/{repo}/issues  ?state&labels&assignee&since…
    POST   /api/v1/github/repositories/{owner}/{repo}/issues
    GET    /api/v1/github/repositories/{owner}/{repo}/issues/{n}        detail + comments + links
    PATCH  /api/v1/github/repositories/{owner}/{repo}/issues/{n}
    POST   …/issues/{n}/close | …/issues/{n}/reopen
    POST   …/issues/{n}/comments ; PATCH|DELETE …/issues/comments/{comment_id}
    POST   …/issues/{n}/labels ; DELETE …/issues/{n}/labels/{name}
    PUT    …/issues/{n}/lock ; DELETE …/issues/{n}/lock
    POST   …/issues/{n}/links ; DELETE …/issues/{n}/links?task_ref=
    GET    /api/v1/github/links?task_ref=&repo=
    GET    /api/v1/github/operations?repo=&op=&status=
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.ui import ui_module
from app.api.v1.github import Client, _allowed, _error, _invalid
from app.auth.deps import AdminRequired, CurrentSession, DbSession, SettingsDep
from app.core.config import Settings
from app.integrations.github.client import GitHubClient, GitHubError
from app.schemas.common import Page
from app.schemas.github_ops import (
    CommentCreate,
    CommentOut,
    IssueClose,
    IssueCreate,
    IssueDetail,
    IssueLinkOut,
    IssueOut,
    IssueUpdate,
    LabelsAdd,
    LinkCreate,
    ListPage,
    LockIn,
    TrackedOperationOut,
)
from app.services import github as gh
from app.services import github_ops as ops
from app.services.approvals import ApprovalError, ApprovalGate
from app.services.audit import ClientInfo

UI_MODULE = ui_module("github", label="GitHub", group="dev", icon="github", order=52)
router = APIRouter(prefix="/v1/github", tags=["github"])

Number = Annotated[int, Path(ge=1, le=10**9)]
CommentId = Annotated[int, Path(ge=1)]
PerPage = Annotated[int, Query(ge=1, le=100)]
PageNo = Annotated[int, Query(ge=1, le=100)]


@contextlib.contextmanager
def errors() -> Iterator[None]:
    """Map service errors to `{"detail": {"code", "detail"}}` HTTP errors."""
    try:
        yield
    except gh.InvalidInput as exc:
        raise _invalid(exc) from None
    except GitHubError as exc:
        raise _error(exc) from None
    except ApprovalError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "detail": exc.message}) from None


@dataclass(slots=True)
class Repo:
    """An allowlisted repo plus everything an endpoint needs to act on it."""

    full: str
    client: GitHubClient
    admin: CurrentSession
    db: AsyncSession
    settings: Settings
    request: Request

    @property
    def actor(self) -> ops.Actor:
        return ops.Actor(self.admin.user.id, ClientInfo.from_request(self.request))

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        maker: async_sessionmaker[AsyncSession] = self.request.app.state.sessionmaker
        return maker

    @property
    def gate(self) -> ApprovalGate:
        return ApprovalGate(self.maker, self.settings)

    def path(self, suffix: str = "") -> str:
        return f"/repos/{self.full}{suffix}"

    async def write(
        self, op: str, target: str | None, method: str, suffix: str, json: Any = None
    ) -> Any:
        return await ops.tracked_write(
            self.maker,
            self.client,
            self.actor,
            op=op,
            repo=self.full,
            target=target,
            method=method,
            path=self.path(suffix),
            json=json,
        )


async def _repo(
    admin: AdminRequired,
    client: Client,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    owner: str,
    repo: str,
) -> Repo:
    full = await _allowed(db, client, settings, owner, repo)
    return Repo(full, client, admin, db, settings, request)


RepoDep = Annotated[Repo, Depends(_repo)]
ISSUES = "/repositories/{owner}/{repo}/issues"


def _issue(r: Repo, data: Any) -> IssueOut:
    with errors():
        return ops.issue_out(r.client, ops._obj(data))


# --- issues -------------------------------------------------------------------------------


@router.get(ISSUES, response_model=ListPage[IssueOut])
async def list_issues(
    r: RepoDep,
    state: Literal["open", "closed", "all"] = "open",
    labels: Annotated[str | None, Query(max_length=500)] = None,
    assignee: Annotated[
        str | None, Query(max_length=100, pattern=r"^([A-Za-z0-9-]+|\*|none)$")
    ] = None,
    since: datetime | None = None,
    sort: Literal["created", "updated", "comments"] = "created",
    direction: Literal["asc", "desc"] = "desc",
    per_page: PerPage = 30,
    page: PageNo = 1,
    include_pulls: bool = False,
) -> ListPage[IssueOut]:
    with errors():
        return await ops.list_issues(
            r.client,
            r.full,
            state=state,
            labels=labels,
            assignee=assignee,
            since=since,
            sort=sort,
            direction=direction,
            per_page=per_page,
            page=page,
            include_pulls=include_pulls,
        )


@router.post(ISSUES, response_model=IssueOut, status_code=status.HTTP_201_CREATED)
async def create_issue(r: RepoDep, body: IssueCreate) -> IssueOut:
    with errors():
        data = await r.write(
            "issue.create", None, "POST", "/issues", body.model_dump(exclude_none=True)
        )
    return _issue(r, data)


@router.get(ISSUES + "/{n}", response_model=IssueDetail)
async def get_issue(r: RepoDep, n: Number) -> IssueDetail:
    with errors():
        return await ops.issue_detail(r.db, r.client, r.full, n)


@router.patch(ISSUES + "/{n}", response_model=IssueOut)
async def update_issue(r: RepoDep, n: Number, body: IssueUpdate) -> IssueOut:
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(422, {"code": "empty_update", "detail": "nothing to update"})
    with errors():
        data = await r.write("issue.update", f"issue#{n}", "PATCH", f"/issues/{n}", payload)
    return _issue(r, data)


@router.post(ISSUES + "/{n}/close", response_model=IssueOut)
async def close_issue(r: RepoDep, n: Number, body: IssueClose | None = None) -> IssueOut:
    reason = (body or IssueClose()).state_reason
    with errors():
        data = await r.write(
            "issue.close",
            f"issue#{n}",
            "PATCH",
            f"/issues/{n}",
            {"state": "closed", "state_reason": reason},
        )
    return _issue(r, data)


@router.post(ISSUES + "/{n}/reopen", response_model=IssueOut)
async def reopen_issue(r: RepoDep, n: Number) -> IssueOut:
    with errors():
        data = await r.write(
            "issue.reopen", f"issue#{n}", "PATCH", f"/issues/{n}", {"state": "open"}
        )
    return _issue(r, data)


# --- comments -----------------------------------------------------------------------------


def _comment(r: Repo, data: Any) -> CommentOut:
    with errors():
        return ops.comment_out(r.client, ops._obj(data))


@router.post(ISSUES + "/{n}/comments", response_model=CommentOut, status_code=201)
async def create_comment(r: RepoDep, n: Number, body: CommentCreate) -> CommentOut:
    with errors():
        data = await r.write(
            "issue.comment.create",
            f"issue#{n}",
            "POST",
            f"/issues/{n}/comments",
            {"body": body.body},
        )
    return _comment(r, data)


@router.patch(ISSUES + "/comments/{comment_id}", response_model=CommentOut)
async def edit_comment(r: RepoDep, comment_id: CommentId, body: CommentCreate) -> CommentOut:
    with errors():
        data = await r.write(
            "issue.comment.edit",
            f"comment:{comment_id}",
            "PATCH",
            f"/issues/comments/{comment_id}",
            {"body": body.body},
        )
    return _comment(r, data)


@router.delete(ISSUES + "/comments/{comment_id}", status_code=204)
async def delete_comment(r: RepoDep, comment_id: CommentId) -> Response:
    with errors():
        await r.write(
            "issue.comment.delete",
            f"comment:{comment_id}",
            "DELETE",
            f"/issues/comments/{comment_id}",
        )
    return Response(status_code=204)


# --- labels + lock ------------------------------------------------------------------------


def _labels(data: Any) -> list[str]:
    return [str(x["name"]) for x in data or [] if isinstance(x, dict) and "name" in x]


@router.post(ISSUES + "/{n}/labels", response_model=list[str])
async def add_labels(r: RepoDep, n: Number, body: LabelsAdd) -> list[str]:
    with errors():
        data = await r.write(
            "issue.label.add", f"issue#{n}", "POST", f"/issues/{n}/labels", {"labels": body.labels}
        )
    return _labels(data)


# {name:path}: label names may contain "/" (sent as %2F, which the server decodes).
@router.delete(ISSUES + "/{n}/labels/{name:path}", response_model=list[str])
async def remove_label(
    r: RepoDep, n: Number, name: Annotated[str, Path(min_length=1, max_length=100)]
) -> list[str]:
    with errors():
        data = await r.write(
            "issue.label.remove",
            f"issue#{n}",
            "DELETE",
            f"/issues/{n}/labels/{quote(name, safe='')}",
        )
    return _labels(data)


@router.put(ISSUES + "/{n}/lock", status_code=204)
async def lock_issue(r: RepoDep, n: Number, body: LockIn | None = None) -> Response:
    payload = body.model_dump(exclude_none=True) if body else {}
    with errors():
        await r.write("issue.lock", f"issue#{n}", "PUT", f"/issues/{n}/lock", payload)
    return Response(status_code=204)


@router.delete(ISSUES + "/{n}/lock", status_code=204)
async def unlock_issue(r: RepoDep, n: Number) -> Response:
    with errors():
        await r.write("issue.unlock", f"issue#{n}", "DELETE", f"/issues/{n}/lock")
    return Response(status_code=204)


# --- task links ---------------------------------------------------------------------------


@router.post(ISSUES + "/{n}/links", response_model=IssueLinkOut)
async def create_link(r: RepoDep, n: Number, body: LinkCreate, response: Response) -> IssueLinkOut:
    with errors():
        row, created = await ops.create_link(
            r.db,
            r.client,
            r.full,
            n,
            task_ref=body.task_ref,
            task_kind=body.task_kind,
            actor=r.actor,
        )
    response.status_code = 201 if created else 200
    return IssueLinkOut.model_validate(row)


@router.delete(ISSUES + "/{n}/links", status_code=204)
async def delete_link(
    r: RepoDep, n: Number, task_ref: Annotated[str, Query(min_length=1, max_length=200)]
) -> Response:
    if not await ops.delete_link(r.db, r.full, n, task_ref=task_ref, actor=r.actor):
        raise HTTPException(404, {"code": "not_found", "detail": "link not found"})
    return Response(status_code=204)


@router.get("/links", response_model=list[IssueLinkOut])
async def list_links(
    _admin: AdminRequired,
    db: DbSession,
    task_ref: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    repo: Annotated[str | None, Query(max_length=201)] = None,
) -> list[IssueLinkOut]:
    if task_ref is None and repo is None:
        raise HTTPException(422, {"code": "filter_required", "detail": "pass task_ref or repo"})
    return await ops.list_links(db, task_ref=task_ref, repo=repo)


# --- tracked operations -------------------------------------------------------------------


@router.get("/operations", response_model=Page[TrackedOperationOut])
async def list_operations(
    _admin: AdminRequired,
    db: DbSession,
    repo: Annotated[str | None, Query(max_length=201)] = None,
    op: Annotated[str | None, Query(max_length=64)] = None,
    status_: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[TrackedOperationOut]:
    rows, total = await ops.list_operations(
        db, repo=repo, op=op, status=status_, limit=limit, offset=offset
    )
    return Page(
        items=[TrackedOperationOut.model_validate(x) for x in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
