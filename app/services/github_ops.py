"""GitHub issues, pull requests, issue <-> task links, tracked operations and the gated
operations behind the ApprovalGate (docs/19-github-issues-prs.md).

- Every write goes through `tracked_write`: a github.tracked_operations row (`running` ->
  `succeeded` | `failed`, payload redacted) and an audit event, whatever the outcome.
- Merge, force-push (a forced ref update), branch-protection update/delete, branch delete,
  repo settings and Actions enable/disable are never sent directly. `file_gated` stores an
  approval request (with the artifact the approver will be shown) and a tracked operation in
  `pending_approval`; the executors below run only from `ApprovalGate.approve`, after the
  artifact was re-computed and found unchanged.
- The allowlist is checked by the router before any call, and again by every executor.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.client import (
    GitHubClient,
    GitHubError,
    GitHubNotConfigured,
    GitHubNotFound,
)
from app.integrations.github.redact import redact_obj
from app.models.approvals import ApprovalRequest
from app.models.github import GitHubIssueLink, GitHubTrackedOperation
from app.schemas.github_ops import (
    BranchRef,
    CommentOut,
    DiffOut,
    GatedOut,
    IssueDetail,
    IssueLinkOut,
    IssueOut,
    ListPage,
    MergeStatusOut,
    MilestoneOut,
    PullDetail,
    PullFileOut,
    PullOut,
    ReviewCommentOut,
    ReviewOut,
)
from app.services import audit
from app.services import github as gh
from app.services.approvals import (
    REGISTRY,
    ApprovalError,
    ApprovalGate,
    ExecContext,
    ExecutorRegistry,
    ExecutorSpec,
)
from app.services.audit import ClientInfo

log = get_logger("services.github_ops")

DIFF_ACCEPT = "application/vnd.github.diff"
MAX_PAYLOAD_TEXT = 4000
MAX_PATCH_CHARS = 100_000
MAX_COMMENT_PAGES = 3
# The merge artifact hashes at most this much of the diff (the head sha covers the rest).
MERGE_HASH_MAX_BYTES = 32 * 1024 * 1024
SUMMARY_KEYS = (
    "id",
    "number",
    "node_id",
    "state",
    "html_url",
    "sha",
    "merged",
    "message",
    "ref",
    "full_name",
    "visibility",
    "default_branch",
    "locked",
)

# approval kind -> tracked operation name
KIND_OPS = {
    "github.pr.merge": "pr.merge",
    "github.ref.force_update": "ref.force_update",
    "github.branch_protection.update": "branch_protection.update",
    "github.branch_protection.delete": "branch_protection.delete",
    "github.branch.delete": "branch.delete",
    "github.repo.update_settings": "repo.update_settings",
    "github.actions.set_permissions": "actions.set_permissions",
}
# approval status -> tracked operation status
_OP_STATUS = {
    "approved": "running",
    "executed": "succeeded",
    "failed": "failed",
    "denied": "denied",
    "expired": "expired",
    "invalidated": "invalidated",
}


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID
    client: ClientInfo


# --- small helpers ------------------------------------------------------------------------


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _login(u: Any) -> str | None:
    return str(u["login"]) if isinstance(u, dict) and u.get("login") else None


def _has_next(headers: Any) -> bool:
    return 'rel="next"' in str(headers.get("link") or "")


def _cap(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_PAYLOAD_TEXT else value[:MAX_PAYLOAD_TEXT] + "…"
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v) for v in value[:200]]
    return value


def safe_payload(client: GitHubClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Token-redacted, size-capped copy of a request body for the DB."""
    out: dict[str, Any] = _cap(client.safe(payload))
    return out


def summarize(status: int, data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status}
    if isinstance(data, dict):
        for k in SUMMARY_KEYS:
            v = data.get(k)
            if isinstance(v, str | int | bool):
                out[k] = v[:500] if isinstance(v, str) else v
        obj = data.get("object")
        if isinstance(obj, dict) and isinstance(obj.get("sha"), str):
            out["sha"] = obj["sha"]
    elif isinstance(data, list):
        out["count"] = len(data)
    return out


def check_head(head: str) -> str:
    """A PR head: `branch` or `owner:branch`."""
    owner, sep, branch = head.rpartition(":")
    if sep:
        gh.check_name(owner, "owner")
    gh.check_ref(branch, "head")
    return head


def check_branch(branch: str) -> str:
    gh.check_ref(branch, "branch")
    return branch


def _branch_path(branch: str) -> str:
    return quote(branch, safe="/")


# --- tracked writes -----------------------------------------------------------------------


async def tracked_write(
    maker: async_sessionmaker[AsyncSession],
    client: GitHubClient,
    actor: Actor,
    *,
    op: str,
    repo: str,
    target: str | None,
    method: str,
    path: str,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Send one write, recording it in github.tracked_operations and the audit log."""
    payload = json if isinstance(json, dict) else ({"items": json} if json is not None else {})
    async with maker() as db, db.begin():
        row = GitHubTrackedOperation(
            op=op,
            repo=repo,
            target=target,
            request_payload=safe_payload(client, payload),
            status="running",
            actor=actor.user_id,
        )
        db.add(row)
        await db.flush()
        op_id = row.id
    summary: dict[str, Any] | None = None
    error: str | None = None
    failure: GitHubError | None = None
    data: Any = None
    try:
        resp = await client.request(method, path, json=json, params=params)
        data = resp.data
        summary = summarize(resp.status, data)
    except GitHubError as exc:
        error, failure = f"{exc.code}: {exc.message}"[:2000], exc
    status = "failed" if failure is not None else "succeeded"
    async with maker() as db, db.begin():
        await db.execute(
            update(GitHubTrackedOperation)
            .where(GitHubTrackedOperation.id == op_id)
            .values(
                status=status,
                github_response_summary=summary,
                error=error,
                finished_at=func.now(),
            )
        )
        audit.record(
            db,
            "github_" + op.replace(".", "_"),
            actor.client,
            actor_user_id=actor.user_id,
            details={"repo": repo, "target": target, "operation_id": op_id, "status": status}
            | ({"error": error} if error else {}),
        )
    log.info("github_write", op=op, repo=repo, target=target, status=status)
    if failure is not None:
        raise failure
    return data


async def list_operations(
    db: AsyncSession,
    *,
    repo: str | None,
    op: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[GitHubTrackedOperation], int]:
    q = select(GitHubTrackedOperation)
    if repo:
        q = q.where(func.lower(GitHubTrackedOperation.repo) == repo.lower())
    if op:
        q = q.where(GitHubTrackedOperation.op == op)
    if status:
        q = q.where(GitHubTrackedOperation.status == status)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = (
        await db.scalars(q.order_by(GitHubTrackedOperation.id.desc()).limit(limit).offset(offset))
    ).all()
    return list(rows), total or 0


# --- converters ---------------------------------------------------------------------------


def issue_out(client: GitHubClient, d: dict[str, Any]) -> IssueOut:
    ms = d.get("milestone")
    return IssueOut(
        number=int(d["number"]),
        node_id=d.get("node_id"),
        title=client.redact(str(d.get("title") or "")),
        body=client.redact(d["body"]) if isinstance(d.get("body"), str) else None,
        state=str(d.get("state") or ""),
        state_reason=d.get("state_reason"),
        locked=bool(d.get("locked")),
        active_lock_reason=d.get("active_lock_reason"),
        labels=[
            str(lb["name"] if isinstance(lb, dict) else lb) for lb in d.get("labels") or [] if lb
        ],
        assignees=[x for a in d.get("assignees") or [] if (x := _login(a))],
        milestone=MilestoneOut(number=ms["number"], title=ms.get("title"), state=ms.get("state"))
        if isinstance(ms, dict) and "number" in ms
        else None,
        user=_login(d.get("user")),
        comments=int(d.get("comments") or 0),
        is_pull_request="pull_request" in d,
        created_at=_dt(d.get("created_at")),
        updated_at=_dt(d.get("updated_at")),
        closed_at=_dt(d.get("closed_at")),
        html_url=d.get("html_url"),
    )


def comment_out(client: GitHubClient, d: dict[str, Any]) -> CommentOut:
    return CommentOut(
        id=int(d["id"]),
        body=client.redact(d["body"]) if isinstance(d.get("body"), str) else None,
        user=_login(d.get("user")),
        created_at=_dt(d.get("created_at")),
        updated_at=_dt(d.get("updated_at")),
        html_url=d.get("html_url"),
    )


def review_comment_out(client: GitHubClient, d: dict[str, Any]) -> ReviewCommentOut:
    base = comment_out(client, d).model_dump()
    return ReviewCommentOut(
        **base,
        path=d.get("path"),
        line=d.get("line"),
        side=d.get("side"),
        start_line=d.get("start_line"),
        commit_id=d.get("commit_id"),
        in_reply_to_id=d.get("in_reply_to_id"),
        pull_request_review_id=d.get("pull_request_review_id"),
        diff_hunk=d.get("diff_hunk"),
    )


def _branch_ref(d: Any) -> BranchRef:
    if not isinstance(d, dict):
        return BranchRef()
    repo = d.get("repo")
    return BranchRef(
        ref=d.get("ref"),
        sha=d.get("sha"),
        label=d.get("label"),
        repo=repo.get("full_name") if isinstance(repo, dict) else None,
    )


def pull_out(client: GitHubClient, d: dict[str, Any]) -> PullOut:
    return PullOut(**_pull_fields(client, d))


def _pull_fields(client: GitHubClient, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": int(d["number"]),
        "node_id": d.get("node_id"),
        "title": client.redact(str(d.get("title") or "")),
        "body": client.redact(d["body"]) if isinstance(d.get("body"), str) else None,
        "state": str(d.get("state") or ""),
        "draft": bool(d.get("draft")),
        "merged": bool(d.get("merged")) or bool(d.get("merged_at")),
        "locked": bool(d.get("locked")),
        "user": _login(d.get("user")),
        "head": _branch_ref(d.get("head")),
        "base": _branch_ref(d.get("base")),
        "labels": [str(lb["name"]) for lb in d.get("labels") or [] if isinstance(lb, dict)],
        "requested_reviewers": [x for r in d.get("requested_reviewers") or [] if (x := _login(r))],
        "created_at": _dt(d.get("created_at")),
        "updated_at": _dt(d.get("updated_at")),
        "closed_at": _dt(d.get("closed_at")),
        "merged_at": _dt(d.get("merged_at")),
        "merge_commit_sha": d.get("merge_commit_sha"),
        "html_url": d.get("html_url"),
    }


def pull_detail(client: GitHubClient, d: dict[str, Any]) -> PullDetail:
    return PullDetail(
        **_pull_fields(client, d),
        mergeable=d.get("mergeable"),
        mergeable_state=d.get("mergeable_state"),
        rebaseable=d.get("rebaseable"),
        commits=d.get("commits"),
        additions=d.get("additions"),
        deletions=d.get("deletions"),
        changed_files=d.get("changed_files"),
        comments=d.get("comments"),
        review_comments=d.get("review_comments"),
    )


def review_out(client: GitHubClient, d: dict[str, Any]) -> ReviewOut:
    return ReviewOut(
        id=int(d["id"]),
        user=_login(d.get("user")),
        state=d.get("state"),
        body=client.redact(d["body"]) if isinstance(d.get("body"), str) else None,
        commit_id=d.get("commit_id"),
        submitted_at=_dt(d.get("submitted_at")),
        html_url=d.get("html_url"),
    )


def _dicts(data: Any) -> list[dict[str, Any]]:
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _obj(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GitHubError("unexpected response from GitHub")
    return data


# --- issues -------------------------------------------------------------------------------


async def list_issues(
    client: GitHubClient,
    full: str,
    *,
    state: str,
    labels: str | None,
    assignee: str | None,
    since: datetime | None,
    sort: str,
    direction: str,
    per_page: int,
    page: int,
    include_pulls: bool,
) -> ListPage[IssueOut]:
    params: dict[str, Any] = {
        "state": state,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    if labels:
        params["labels"] = labels
    if assignee:
        params["assignee"] = assignee
    if since:
        params["since"] = since.isoformat()
    resp = await client.request("GET", f"/repos/{full}/issues", params=params)
    items = [issue_out(client, d) for d in _dicts(resp.data) if "number" in d]
    if not include_pulls:
        items = [i for i in items if not i.is_pull_request]
    return ListPage(items=items, page=page, per_page=per_page, has_next=_has_next(resp.headers))


async def issue_detail(
    db: AsyncSession, client: GitHubClient, full: str, number: int
) -> IssueDetail:
    d = _obj(await client.get_json(f"/repos/{full}/issues/{number}"))
    comments, complete = await client.paginate_all(
        f"/repos/{full}/issues/{number}/comments", max_pages=MAX_COMMENT_PAGES
    )
    links = await list_links(db, repo=full, issue_number=number)
    return IssueDetail(
        **issue_out(client, d).model_dump(),
        comment_list=[comment_out(client, c) for c in _dicts(comments)],
        comments_truncated=not complete,
        links=links,
    )


# --- links --------------------------------------------------------------------------------


async def create_link(
    db: AsyncSession,
    client: GitHubClient,
    full: str,
    number: int,
    *,
    task_ref: str,
    task_kind: str,
    actor: Actor,
) -> tuple[GitHubIssueLink, bool]:
    """-> (link, created). The issue must exist on GitHub; linking twice is a no-op."""
    issue = _obj(await client.get_json(f"/repos/{full}/issues/{number}", cache=False))
    repo = full.lower()
    stmt = (
        insert(GitHubIssueLink)
        .values(
            repo_full_name=repo,
            issue_number=number,
            issue_node_id=str(issue.get("node_id") or "")[:100] or None,
            task_ref=task_ref,
            task_kind=task_kind,
            created_by=actor.user_id,
        )
        .on_conflict_do_nothing(constraint="uq_issue_task")
        .returning(GitHubIssueLink)
    )
    row = (await db.scalars(stmt)).one_or_none()
    created = row is not None
    if row is None:
        row = (
            await db.scalars(
                select(GitHubIssueLink).where(
                    GitHubIssueLink.repo_full_name == repo,
                    GitHubIssueLink.issue_number == number,
                    GitHubIssueLink.task_ref == task_ref,
                )
            )
        ).one()
    if created:
        audit.record(
            db,
            "github_issue_link_created",
            actor.client,
            actor_user_id=actor.user_id,
            details={"repo": repo, "issue": number, "task_ref": task_ref, "task_kind": task_kind},
        )
    await db.commit()
    return row, created


async def delete_link(
    db: AsyncSession, full: str, number: int, *, task_ref: str, actor: Actor
) -> bool:
    repo = full.lower()
    row = (
        await db.scalars(
            select(GitHubIssueLink).where(
                GitHubIssueLink.repo_full_name == repo,
                GitHubIssueLink.issue_number == number,
                GitHubIssueLink.task_ref == task_ref,
            )
        )
    ).one_or_none()
    if row is None:
        return False
    await db.delete(row)
    audit.record(
        db,
        "github_issue_link_deleted",
        actor.client,
        actor_user_id=actor.user_id,
        details={"repo": repo, "issue": number, "task_ref": task_ref, "link_id": row.id},
    )
    await db.commit()
    return True


async def list_links(
    db: AsyncSession,
    *,
    task_ref: str | None = None,
    repo: str | None = None,
    issue_number: int | None = None,
    limit: int = 200,
) -> list[IssueLinkOut]:
    q = select(GitHubIssueLink)
    if task_ref is not None:
        q = q.where(GitHubIssueLink.task_ref == task_ref)
    if repo is not None:
        q = q.where(GitHubIssueLink.repo_full_name == repo.lower())
    if issue_number is not None:
        q = q.where(GitHubIssueLink.issue_number == issue_number)
    rows = (await db.scalars(q.order_by(GitHubIssueLink.id).limit(limit))).all()
    return [IssueLinkOut.model_validate(r) for r in rows]


# --- pull requests ------------------------------------------------------------------------


async def list_pulls(
    client: GitHubClient,
    full: str,
    *,
    state: str,
    head: str | None,
    base: str | None,
    sort: str,
    direction: str,
    per_page: int,
    page: int,
) -> ListPage[PullOut]:
    params: dict[str, Any] = {
        "state": state,
        "sort": sort,
        "direction": direction,
        "per_page": per_page,
        "page": page,
    }
    if head:
        params["head"] = head
    if base:
        params["base"] = base
    resp = await client.request("GET", f"/repos/{full}/pulls", params=params)
    items = [pull_out(client, d) for d in _dicts(resp.data) if "number" in d]
    return ListPage(items=items, page=page, per_page=per_page, has_next=_has_next(resp.headers))


async def get_pull(client: GitHubClient, full: str, number: int, *, cache: bool = True) -> Any:
    return _obj(await client.get_json(f"/repos/{full}/pulls/{number}", cache=cache))


async def pull_files(client: GitHubClient, full: str, number: int) -> list[PullFileOut]:
    items = await client.paginate(f"/repos/{full}/pulls/{number}/files")
    out = []
    for f in _dicts(items):
        patch = f.get("patch") if isinstance(f.get("patch"), str) else None
        cut = patch is not None and len(patch) > MAX_PATCH_CHARS
        out.append(
            PullFileOut(
                filename=str(f.get("filename") or ""),
                status=f.get("status"),
                additions=int(f.get("additions") or 0),
                deletions=int(f.get("deletions") or 0),
                changes=int(f.get("changes") or 0),
                sha=f.get("sha"),
                previous_filename=f.get("previous_filename"),
                patch=client.redact(patch[:MAX_PATCH_CHARS]) if patch is not None else None,
                patch_truncated=cut,
            )
        )
    return out


async def pull_diff(client: GitHubClient, full: str, number: int, settings: Settings) -> DiffOut:
    cap = settings.GITHUB_DIFF_MAX_BYTES
    try:
        resp = await client.request(
            "GET", f"/repos/{full}/pulls/{number}", accept=DIFF_ACCEPT, raw=True, max_bytes=cap
        )
    except GitHubError as exc:
        if exc.upstream_status == 406:  # GitHub: "diff is too large"
            return DiffOut(
                number=number,
                diff="",
                bytes_returned=0,
                max_bytes=cap,
                truncated=True,
                too_large_upstream=True,
            )
        raise
    raw = resp.data if isinstance(resp.data, bytes) else b""
    truncated = len(raw) > cap
    body = raw[:cap]
    return DiffOut(
        number=number,
        diff=client.redact(body.decode("utf-8", errors="replace")),
        bytes_returned=len(body),
        max_bytes=cap,
        truncated=truncated,
    )


async def pull_commits(client: GitHubClient, full: str, number: int) -> list[Any]:
    items = await client.paginate(f"/repos/{full}/pulls/{number}/commits")
    return [gh.commit_out(client, c) for c in _dicts(items) if "sha" in c]


async def review_comments(client: GitHubClient, full: str, number: int) -> list[ReviewCommentOut]:
    items = await client.paginate(f"/repos/{full}/pulls/{number}/comments")
    return [review_comment_out(client, c) for c in _dicts(items) if "id" in c]


async def issue_comments(client: GitHubClient, full: str, number: int) -> list[CommentOut]:
    items = await client.paginate(f"/repos/{full}/issues/{number}/comments")
    return [comment_out(client, c) for c in _dicts(items) if "id" in c]


async def reviews(client: GitHubClient, full: str, number: int) -> list[ReviewOut]:
    items = await client.paginate(f"/repos/{full}/pulls/{number}/reviews")
    return [review_out(client, r) for r in _dicts(items) if "id" in r]


async def merge_status(client: GitHubClient, full: str, number: int) -> MergeStatusOut:
    d = await get_pull(client, full, number, cache=False)
    head, base = d.get("head") or {}, d.get("base") or {}
    return MergeStatusOut(
        number=number,
        state=str(d.get("state") or ""),
        merged=bool(d.get("merged")),
        draft=bool(d.get("draft")),
        mergeable=d.get("mergeable"),
        mergeable_state=d.get("mergeable_state"),
        rebaseable=d.get("rebaseable"),
        head_ref=head.get("ref"),
        head_sha=head.get("sha"),
        base_ref=base.get("ref"),
    )


# --- gated operations: artifacts ----------------------------------------------------------


async def merge_artifact(client: GitHubClient, full: str, number: int) -> dict[str, Any]:
    """Head sha + sha256 of the unified diff: what a merge approval is bound to."""
    pr = await get_pull(client, full, number, cache=False)
    head, base = pr.get("head") or {}, pr.get("base") or {}
    try:
        resp = await client.request(
            "GET",
            f"/repos/{full}/pulls/{number}",
            accept=DIFF_ACCEPT,
            raw=True,
            max_bytes=MERGE_HASH_MAX_BYTES,
        )
        raw = resp.data if isinstance(resp.data, bytes) else b""
        diff_sha = hashlib.sha256(raw).hexdigest()
    except GitHubError as exc:
        if exc.upstream_status != 406:
            raise
        diff_sha = "too_large"
    return {
        "number": number,
        "head_sha": head.get("sha"),
        "base_ref": base.get("ref"),
        "state": pr.get("state"),
        "diff_sha256": diff_sha,
    }


async def ref_sha(client: GitHubClient, full: str, branch: str) -> str | None:
    data = _obj(
        await client.get_json(f"/repos/{full}/git/ref/heads/{_branch_path(branch)}", cache=False)
    )
    obj = data.get("object") or {}
    sha = obj.get("sha") if isinstance(obj, dict) else None
    return str(sha) if sha else None


async def protection_artifact(client: GitHubClient, full: str, branch: str) -> dict[str, Any]:
    try:
        data = await client.get_json(
            f"/repos/{full}/branches/{_branch_path(branch)}/protection", cache=False
        )
    except GitHubNotFound:
        data = None  # "Branch not protected" (or no such branch: the executor will 404)
    return {"branch": branch, "protection": data}


SETTINGS_KEYS = (
    "visibility",
    "private",
    "default_branch",
    "allow_merge_commit",
    "allow_squash_merge",
    "allow_rebase_merge",
    "allow_auto_merge",
    "allow_update_branch",
    "delete_branch_on_merge",
    "has_issues",
    "has_wiki",
    "has_projects",
    "archived",
)


async def settings_artifact(client: GitHubClient, full: str) -> dict[str, Any]:
    r = _obj(await client.get_json(f"/repos/{full}", cache=False))
    return {"repo": full, "settings": {k: r.get(k) for k in SETTINGS_KEYS}}


async def actions_artifact(client: GitHubClient, full: str) -> dict[str, Any]:
    r = _obj(await client.get_json(f"/repos/{full}/actions/permissions", cache=False))
    return {"repo": full, "actions": r}


async def artifact_for(client: GitHubClient, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The artifact of a gated request; the same function runs at request and approval time."""
    full = str(payload["repo"])
    if kind == "github.pr.merge":
        return await merge_artifact(client, full, int(payload["number"]))
    if kind == "github.ref.force_update":
        branch = str(payload["branch"])
        return {
            "branch": branch,
            "current_sha": await ref_sha(client, full, branch),
            "target_sha": payload["sha"],
        }
    if kind in ("github.branch_protection.update", "github.branch_protection.delete"):
        return await protection_artifact(client, full, str(payload["branch"]))
    if kind == "github.branch.delete":
        branch = str(payload["branch"])
        return {"branch": branch, "current_sha": await ref_sha(client, full, branch)}
    if kind == "github.repo.update_settings":
        return await settings_artifact(client, full)
    if kind == "github.actions.set_permissions":
        return await actions_artifact(client, full)
    raise ApprovalError(500, "unknown_approval_kind", f"no artifact for {kind!r}")


# --- gated operations: filing -------------------------------------------------------------


def _target(kind: str, payload: dict[str, Any]) -> str:
    if "number" in payload:
        return f"pr#{payload['number']}"
    if "branch" in payload:
        return f"branch:{payload['branch']}"
    return {"github.repo.update_settings": "settings", "github.actions.set_permissions": "actions"}[
        kind
    ]


async def file_gated(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: Actor,
    *,
    kind: str,
    payload: dict[str, Any],
    artifact: dict[str, Any] | None = None,
    op: str | None = None,
    target: str | None = None,
) -> GatedOut:
    """Store a pending approval request + a `pending_approval` tracked operation. Nothing is
    written to GitHub here (only GETs, to compute the artifact).

    Kinds registered outside this module (e.g. github.workflow_dispatch_prod) pass their own
    `artifact`, `op` and `target`."""
    if artifact is None:
        artifact = await artifact_for(client, kind, payload)
    full = str(payload["repo"])
    target = target or _target(kind, payload)
    op_name = op or KIND_OPS[kind]
    # The approval keeps the exact payload (it is what gets executed); the tracked operation
    # keeps the redacted, size-capped copy.
    exact = payload | {"artifact": artifact}
    stored = safe_payload(client, exact)
    async with maker() as db, db.begin():
        req = await gate.request(
            kind,
            f"{full} {target}",
            exact,
            artifact,
            requested_by=actor.user_id,
            client=actor.client,
            db=db,
        )
        row = GitHubTrackedOperation(
            op=op_name,
            repo=full,
            target=target,
            request_payload=stored,
            status="pending_approval",
            approval_request_id=req.id,
            actor=actor.user_id,
        )
        db.add(row)
        await db.flush()
        audit.record(
            db,
            "github_" + op_name.replace(".", "_") + "_requested",
            actor.client,
            actor_user_id=actor.user_id,
            details={"repo": full, "target": target, "approval_id": str(req.id), "op_id": row.id},
        )
        return GatedOut(
            approval_id=req.id,
            status=req.status,
            kind=kind,
            target=req.target,
            artifact_sha256=req.artifact_sha256,
            expires_at=req.expires_at,
            operation_id=row.id,
            artifact=artifact,
        )


async def request_merge(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: Actor,
    full: str,
    number: int,
    *,
    merge_method: str,
    expected_head_sha: str,
    commit_title: str | None,
    commit_message: str | None,
) -> GatedOut:
    artifact = await merge_artifact(client, full, number)
    if artifact["state"] != "open":
        raise ApprovalError(409, "pr_not_open", "only an open pull request can be merged")
    if artifact["head_sha"] != expected_head_sha:
        raise ApprovalError(
            409, "head_sha_mismatch", "the pull request head is not the expected sha"
        )
    payload: dict[str, Any] = {
        "repo": full,
        "number": number,
        "merge_method": merge_method,
        "sha": expected_head_sha,
    }
    if commit_title is not None:
        payload["commit_title"] = commit_title
    if commit_message is not None:
        payload["commit_message"] = commit_message
    return await file_gated(
        maker, gate, client, actor, kind="github.pr.merge", payload=payload, artifact=artifact
    )


async def request_force_update(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: Actor,
    full: str,
    *,
    branch: str,
    sha: str,
) -> GatedOut:
    try:  # the target commit must exist in the repo
        await client.get_json(f"/repos/{full}/git/commits/{sha}", cache=False)
    except GitHubNotFound:
        raise ApprovalError(
            422, "unknown_sha", "that commit does not exist in the repository"
        ) from None
    return await file_gated(
        maker,
        gate,
        client,
        actor,
        kind="github.ref.force_update",
        payload={"repo": full, "branch": branch, "sha": sha},
    )


# --- gated operations: executors ----------------------------------------------------------


def _github(ctx: ExecContext) -> GitHubClient:
    client = ctx.deps.get("github")
    if not isinstance(client, GitHubClient):
        raise GitHubNotConfigured("GitHub is not configured")
    return client


async def _recheck(ctx: ExecContext, req: ApprovalRequest) -> tuple[GitHubClient, str]:
    client = _github(ctx)
    owner, _, name = str(req.payload["repo"]).partition("/")
    async with ctx.maker() as db:
        full = await gh.require_allowed(db, client, ctx.settings, owner, name)
    return client, full


async def _current_artifact(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    client, _full = await _recheck(ctx, req)
    return await artifact_for(client, req.kind, req.payload)


async def _execute(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    client, full = await _recheck(ctx, req)
    p = req.payload
    kind = req.kind
    if kind == "github.pr.merge":
        body = {"sha": p["sha"], "merge_method": p["merge_method"]}
        for k in ("commit_title", "commit_message"):
            if k in p:
                body[k] = p[k]
        resp = await client.request("PUT", f"/repos/{full}/pulls/{p['number']}/merge", json=body)
    elif kind == "github.ref.force_update":
        resp = await client.request(
            "PATCH",
            f"/repos/{full}/git/refs/heads/{_branch_path(p['branch'])}",
            json={"sha": p["sha"], "force": True},
        )
    elif kind == "github.branch_protection.update":
        resp = await client.request(
            "PUT",
            f"/repos/{full}/branches/{_branch_path(p['branch'])}/protection",
            json=p["protection"],
        )
    elif kind == "github.branch_protection.delete":
        resp = await client.request(
            "DELETE", f"/repos/{full}/branches/{_branch_path(p['branch'])}/protection"
        )
    elif kind == "github.branch.delete":
        resp = await client.request(
            "DELETE", f"/repos/{full}/git/refs/heads/{_branch_path(p['branch'])}"
        )
    elif kind == "github.repo.update_settings":
        resp = await client.request("PATCH", f"/repos/{full}", json=p["settings"])
    elif kind == "github.actions.set_permissions":
        resp = await client.request(
            "PUT", f"/repos/{full}/actions/permissions", json=p["permissions"]
        )
    else:  # pragma: no cover - only registered kinds reach here
        raise ApprovalError(500, "unknown_approval_kind", kind)
    log.info("github_gated_executed", kind=kind, repo=full, status=resp.status)
    out: dict[str, Any] = redact_obj(summarize(resp.status, resp.data))
    return out


async def _on_status(db: AsyncSession, req: ApprovalRequest) -> None:
    op_status = _OP_STATUS.get(req.status)
    if op_status is None:
        return
    values: dict[str, Any] = {"status": op_status}
    if op_status != "running":
        values["finished_at"] = func.now()
    result = req.result or {}
    if req.status == "executed":
        values["github_response_summary"] = result
    elif req.status in ("failed", "invalidated"):
        values["error"] = str(result.get("error") or result.get("reason") or req.status)[:2000]
    await db.execute(
        update(GitHubTrackedOperation)
        .where(GitHubTrackedOperation.approval_request_id == req.id)
        .values(**values)
    )


def register_executors(registry: ExecutorRegistry = REGISTRY) -> None:
    for kind in KIND_OPS:
        registry.register(
            ExecutorSpec(
                kind=kind,
                execute=_execute,
                current_artifact=_current_artifact,
                on_status=_on_status,
                requires=("github",),
            )
        )


register_executors()
