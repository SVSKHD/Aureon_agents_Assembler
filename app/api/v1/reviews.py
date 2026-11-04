"""Claude PR reviews (docs/22-claude-review.md). Admin only; writes need CSRF.

    POST /api/v1/github/repositories/{owner}/{repo}/pulls/{n}/claude-review   queue (202)
    GET  /api/v1/reviews?repo=&pr=&limit=&offset=                            Page[Review]
    GET  /api/v1/reviews/{id}
    POST /api/v1/reviews/{id}/publish          GitHub review, event COMMENT only (never APPROVE)

A review is advisory: nothing here reads or writes approval requests, and a verdict never
satisfies or bypasses the ApprovalGate.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.ui import ui_module
from app.api.v1.github import Client, _allowed
from app.api.v1.github_issues import Number, RepoDep, errors
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.integrations.claude_cli import ClaudeCLI
from app.integrations.github.client import GitHubClient
from app.models.reviews import Review
from app.schemas.common import Page
from app.schemas.reviews import PublishOut, ReviewOut, ReviewQueued, ReviewRequest
from app.services import audit
from app.services import claude_review as cr
from app.services import github_ops as ops
from app.services.audit import ClientInfo

UI_MODULE = ui_module("reviews", label="Reviews", group="dev", icon="message-square-code", order=56)
router = APIRouter(prefix="/v1", tags=["reviews"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def _runner(request: Request) -> cr.ReviewRunner:
    runner: cr.ReviewRunner = request.app.state.review_runner
    return runner


def _cli(request: Request) -> ClaudeCLI:
    cli: ClaudeCLI = request.app.state.claude_cli
    return cli


def _out(row: Review) -> ReviewOut:
    return ReviewOut.model_validate(row)


@router.post(
    "/github/repositories/{owner}/{repo}/pulls/{n}/claude-review",
    response_model=ReviewQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_review(r: RepoDep, n: Number, body: ReviewRequest | None = None) -> ReviewQueued:
    force = bool(body and body.force)
    runner = _runner(r.request)
    if runner.full():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"code": "review_queue_full", "detail": "too many reviews waiting; try later"},
            headers={"Retry-After": "60"},
        )
    with errors():
        pr = ops._obj(await ops.get_pull(r.client, r.full, n, cache=False))
    if pr.get("state") != "open":
        raise HTTPException(409, {"code": "pr_not_open", "detail": "the pull request is not open"})
    head = (pr.get("head") or {}).get("sha")
    base = (pr.get("base") or {}).get("sha")
    if not isinstance(head, str) or not isinstance(base, str):
        raise HTTPException(502, {"code": "github_bad_response", "detail": "no head/base sha"})
    repo = r.full.lower()
    actor = r.actor
    review_id, dedup = await cr.create_row(
        r.db,
        repo=repo,
        number=n,
        head_sha=head,
        base_sha=base,
        requested_by=r.admin.user.id,
        force=force,
    )
    audit.record(
        r.db,
        "claude_review_requested",
        actor.client,
        actor_user_id=actor.user_id,
        details={
            "review_id": str(review_id),
            "repo": repo,
            "pr": n,
            "head_sha": head,
            "force": force,
            "deduplicated": dedup,
        },
    )
    await r.db.commit()
    if dedup:
        row = await r.db.get(Review, review_id)
        return ReviewQueued(
            id=review_id, status=row.status if row else "done", head_sha=head, deduplicated=True
        )
    job = cr.ReviewJob(
        review_id=review_id,
        repo=repo,
        number=n,
        head_sha=head,
        base_sha=base,
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        actor_id=actor.user_id,
        client=actor.client,
    )
    maker, settings, client, cli = r.maker, r.settings, r.client, _cli(r.request)

    async def work() -> None:
        await cr.run_job(maker, settings, client, cli, job)

    runner.submit(work)
    return ReviewQueued(id=review_id, status="queued", head_sha=head, deduplicated=False)


@router.get("/reviews", response_model=Page[ReviewOut])
async def list_reviews(
    _admin: AdminRequired,
    db: DbSession,
    repo: Annotated[str | None, Query(max_length=200)] = None,
    pr: Annotated[int | None, Query(ge=1, le=10**9)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[ReviewOut]:
    conds: list[Any] = []
    if repo:
        conds.append(Review.repo == repo.lower())
    if pr is not None:
        conds.append(Review.pr_number == pr)
    total = await db.scalar(select(func.count()).select_from(Review).where(*conds)) or 0
    rows = await db.scalars(
        select(Review)
        .where(*conds)
        .order_by(Review.created_at.desc(), Review.id)
        .limit(limit)
        .offset(offset)
    )
    return Page[ReviewOut](items=[_out(x) for x in rows], total=total, limit=limit, offset=offset)


@router.get("/reviews/{review_id}", response_model=ReviewOut)
async def get_review(_admin: AdminRequired, db: DbSession, review_id: uuid.UUID) -> ReviewOut:
    row = await db.get(Review, review_id)
    if row is None:
        raise HTTPException(404, {"code": "review_not_found", "detail": "no such review"})
    return _out(row)


@router.post("/reviews/{review_id}/publish", response_model=PublishOut)
async def publish_review(
    admin: AdminRequired,
    client: Client,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    review_id: uuid.UUID,
) -> PublishOut:
    row = await db.get(Review, review_id)
    if row is None:
        raise HTTPException(404, {"code": "review_not_found", "detail": "no such review"})
    owner, _, name = row.repo.partition("/")
    await _allowed(db, client, settings, owner, name)  # still allowlisted (404 otherwise)
    await db.commit()
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    gh: GitHubClient = client
    actor = ops.Actor(admin.user.id, ClientInfo.from_request(request))
    try:
        with errors():
            fresh, gh_id, inline, in_body = await cr.publish(maker, gh, actor, review_id, settings)
    except cr.PublishError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "detail": exc.message}) from None
    return PublishOut(
        review=_out(fresh),
        github_review_id=gh_id,
        event="COMMENT",
        inline_comments=inline,
        body_findings=in_body,
    )
