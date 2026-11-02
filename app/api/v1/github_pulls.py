"""GitHub pull requests and the approval-gated repository operations
(docs/19-github-issues-prs.md). Admin only; writes need CSRF; allowlist enforced (404).

    GET   …/{owner}/{repo}/pulls                      ?state&head&base&sort&direction&page
    POST  …/{owner}/{repo}/pulls                      create (head, base, title, body, draft)
    GET   …/pulls/{n} ; PATCH …/pulls/{n}             detail ; update (title, body, base, state)
    GET   …/pulls/{n}/files | /diff | /commits | /merge-status
    GET|POST …/pulls/{n}/comments                     review comments
    GET|POST …/pulls/{n}/issue-comments               conversation comments
    GET|POST …/pulls/{n}/reviews                      COMMENT | APPROVE | REQUEST_CHANGES

Gated (202 + an approval request; nothing is written to GitHub until it is approved):

    POST   …/pulls/{n}/merge                          merge | squash | rebase, expected head sha
    POST   …/{owner}/{repo}/force-push                forced ref update to a sha
    PUT    …/{owner}/{repo}/branch-protection/{branch}
    DELETE …/{owner}/{repo}/branch-protection/{branch}
    DELETE …/{owner}/{repo}/branches/{branch}
    PATCH  …/{owner}/{repo}/settings                  visibility, default branch, merge settings
    PUT    …/{owner}/{repo}/actions-permissions       enable / disable Actions
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.api.v1.github_issues import Number, PageNo, PerPage, RepoDep, errors
from app.schemas.github import CommitOut
from app.schemas.github_ops import (
    ActionsPermissionsUpdate,
    CommentCreate,
    CommentOut,
    DiffOut,
    ForcePushRequest,
    GatedOut,
    ListPage,
    MergeRequest,
    MergeStatusOut,
    ProtectionUpdate,
    PullCreate,
    PullDetail,
    PullFileOut,
    PullOut,
    PullUpdate,
    RepoSettingsUpdate,
    ReviewCommentCreate,
    ReviewCommentOut,
    ReviewCreate,
    ReviewOut,
)
from app.services import github as gh
from app.services import github_ops as ops

router = APIRouter(prefix="/v1/github", tags=["github"])

REPO = "/repositories/{owner}/{repo}"
PULLS = REPO + "/pulls"
Branch = Annotated[str, Path(min_length=1, max_length=255)]
ACCEPTED = status.HTTP_202_ACCEPTED


@router.get(PULLS, response_model=ListPage[PullOut])
async def list_pulls(
    r: RepoDep,
    state: Literal["open", "closed", "all"] = "open",
    head: Annotated[str | None, Query(max_length=356)] = None,
    base: Annotated[str | None, Query(max_length=255)] = None,
    sort: Literal["created", "updated", "popularity", "long-running"] = "created",
    direction: Literal["asc", "desc"] = "desc",
    per_page: PerPage = 30,
    page: PageNo = 1,
) -> ListPage[PullOut]:
    with errors():
        if head:
            ops.check_head(head)
        if base:
            ops.check_branch(base)
        return await ops.list_pulls(
            r.client,
            r.full,
            state=state,
            head=head,
            base=base,
            sort=sort,
            direction=direction,
            per_page=per_page,
            page=page,
        )


@router.post(PULLS, response_model=PullDetail, status_code=status.HTTP_201_CREATED)
async def create_pull(r: RepoDep, body: PullCreate) -> PullDetail:
    with errors():
        ops.check_head(body.head)
        ops.check_branch(body.base)
        data = await r.write(
            "pr.create", None, "POST", "/pulls", body.model_dump(exclude_none=True)
        )
        return ops.pull_detail(r.client, ops._obj(data))


@router.get(PULLS + "/{n}", response_model=PullDetail)
async def get_pull(r: RepoDep, n: Number) -> PullDetail:
    with errors():
        return ops.pull_detail(r.client, await ops.get_pull(r.client, r.full, n))


@router.patch(PULLS + "/{n}", response_model=PullDetail)
async def update_pull(r: RepoDep, n: Number, body: PullUpdate) -> PullDetail:
    payload = body.model_dump(exclude_unset=True, exclude_none=True)
    if not payload:
        raise HTTPException(422, {"code": "empty_update", "detail": "nothing to update"})
    with errors():
        if body.base:
            ops.check_branch(body.base)
        data = await r.write("pr.update", f"pr#{n}", "PATCH", f"/pulls/{n}", payload)
        return ops.pull_detail(r.client, ops._obj(data))


@router.get(PULLS + "/{n}/files", response_model=list[PullFileOut])
async def pull_files(r: RepoDep, n: Number) -> list[PullFileOut]:
    with errors():
        return await ops.pull_files(r.client, r.full, n)


@router.get(PULLS + "/{n}/diff", response_model=DiffOut)
async def pull_diff(r: RepoDep, n: Number) -> DiffOut:
    with errors():
        return await ops.pull_diff(r.client, r.full, n, r.settings)


@router.get(PULLS + "/{n}/commits", response_model=list[CommitOut])
async def pull_commits(r: RepoDep, n: Number) -> list[Any]:
    with errors():
        return await ops.pull_commits(r.client, r.full, n)


@router.get(PULLS + "/{n}/merge-status", response_model=MergeStatusOut)
async def merge_status(r: RepoDep, n: Number) -> MergeStatusOut:
    with errors():
        return await ops.merge_status(r.client, r.full, n)


# --- comments + reviews -------------------------------------------------------------------


@router.get(PULLS + "/{n}/comments", response_model=list[ReviewCommentOut])
async def list_review_comments(r: RepoDep, n: Number) -> list[ReviewCommentOut]:
    with errors():
        return await ops.review_comments(r.client, r.full, n)


@router.post(PULLS + "/{n}/comments", response_model=ReviewCommentOut, status_code=201)
async def create_review_comment(
    r: RepoDep, n: Number, body: ReviewCommentCreate
) -> ReviewCommentOut:
    with errors():
        if body.in_reply_to is not None:
            data = await r.write(
                "pr.review_comment.reply",
                f"pr#{n}",
                "POST",
                f"/pulls/{n}/comments/{body.in_reply_to}/replies",
                {"body": body.body},
            )
        else:
            if body.path:
                gh.check_path(body.path)
            payload = body.model_dump(exclude_none=True, exclude={"in_reply_to"})
            data = await r.write(
                "pr.review_comment.create", f"pr#{n}", "POST", f"/pulls/{n}/comments", payload
            )
        return ops.review_comment_out(r.client, ops._obj(data))


@router.get(PULLS + "/{n}/issue-comments", response_model=list[CommentOut])
async def list_issue_comments(r: RepoDep, n: Number) -> list[CommentOut]:
    with errors():
        return await ops.issue_comments(r.client, r.full, n)


@router.post(PULLS + "/{n}/issue-comments", response_model=CommentOut, status_code=201)
async def create_issue_comment(r: RepoDep, n: Number, body: CommentCreate) -> CommentOut:
    with errors():
        data = await r.write(
            "pr.comment.create", f"pr#{n}", "POST", f"/issues/{n}/comments", {"body": body.body}
        )
        return ops.comment_out(r.client, ops._obj(data))


@router.get(PULLS + "/{n}/reviews", response_model=list[ReviewOut])
async def list_reviews(r: RepoDep, n: Number) -> list[ReviewOut]:
    with errors():
        return await ops.reviews(r.client, r.full, n)


@router.post(PULLS + "/{n}/reviews", response_model=ReviewOut, status_code=201)
async def create_review(r: RepoDep, n: Number, body: ReviewCreate) -> ReviewOut:
    if body.event == "REQUEST_CHANGES" and not body.body:
        raise HTTPException(
            422, {"code": "body_required", "detail": "REQUEST_CHANGES needs a body"}
        )
    with errors():
        for c in body.comments or []:
            gh.check_path(c.path)
        data = await r.write(
            "pr.review.create",
            f"pr#{n}",
            "POST",
            f"/pulls/{n}/reviews",
            body.model_dump(exclude_none=True),
        )
        return ops.review_out(r.client, ops._obj(data))


# --- gated operations (202) ---------------------------------------------------------------


@router.post(PULLS + "/{n}/merge", response_model=GatedOut, status_code=ACCEPTED)
async def merge_pull(r: RepoDep, n: Number, body: MergeRequest) -> GatedOut:
    with errors():
        return await ops.request_merge(
            r.maker,
            r.gate,
            r.client,
            r.actor,
            r.full,
            n,
            merge_method=body.merge_method,
            expected_head_sha=body.expected_head_sha,
            commit_title=body.commit_title,
            commit_message=body.commit_message,
        )


@router.post(REPO + "/force-push", response_model=GatedOut, status_code=ACCEPTED)
async def force_push(r: RepoDep, body: ForcePushRequest) -> GatedOut:
    with errors():
        ops.check_branch(body.branch)
        return await ops.request_force_update(
            r.maker, r.gate, r.client, r.actor, r.full, branch=body.branch, sha=body.sha
        )


async def _gated(r: Any, kind: str, payload: dict[str, Any]) -> GatedOut:
    with errors():
        if "branch" in payload:
            ops.check_branch(payload["branch"])
        return await ops.file_gated(
            r.maker, r.gate, r.client, r.actor, kind=kind, payload={"repo": r.full, **payload}
        )


@router.put(
    REPO + "/branch-protection/{branch:path}", response_model=GatedOut, status_code=ACCEPTED
)
async def update_protection(r: RepoDep, branch: Branch, body: ProtectionUpdate) -> GatedOut:
    return await _gated(
        r,
        "github.branch_protection.update",
        {"branch": branch, "protection": body.model_dump(exclude_unset=True)},
    )


@router.delete(
    REPO + "/branch-protection/{branch:path}", response_model=GatedOut, status_code=ACCEPTED
)
async def delete_protection(r: RepoDep, branch: Branch) -> GatedOut:
    return await _gated(r, "github.branch_protection.delete", {"branch": branch})


@router.delete(REPO + "/branches/{branch:path}", response_model=GatedOut, status_code=ACCEPTED)
async def delete_branch(r: RepoDep, branch: Branch) -> GatedOut:
    return await _gated(r, "github.branch.delete", {"branch": branch})


@router.patch(REPO + "/settings", response_model=GatedOut, status_code=ACCEPTED)
async def update_settings(r: RepoDep, body: RepoSettingsUpdate) -> GatedOut:
    settings = body.model_dump(exclude_unset=True, exclude_none=True)
    if not settings:
        raise HTTPException(422, {"code": "empty_update", "detail": "nothing to update"})
    if "default_branch" in settings:
        with errors():
            ops.check_branch(settings["default_branch"])
    return await _gated(r, "github.repo.update_settings", {"settings": settings})


@router.put(REPO + "/actions-permissions", response_model=GatedOut, status_code=ACCEPTED)
async def set_actions_permissions(r: RepoDep, body: ActionsPermissionsUpdate) -> GatedOut:
    return await _gated(
        r, "github.actions.set_permissions", {"permissions": body.model_dump(exclude_none=True)}
    )
