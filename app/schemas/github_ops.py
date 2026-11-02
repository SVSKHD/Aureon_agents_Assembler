"""Issues, PRs, links, tracked operations and gated requests (docs/19-github-issues-prs.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Body = Field(default=None, max_length=65536)  # GitHub's own body limit


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- outputs ------------------------------------------------------------------------------


class MilestoneOut(BaseModel):
    number: int
    title: str | None = None
    state: str | None = None


class IssueOut(BaseModel):
    number: int
    node_id: str | None = None
    title: str
    body: str | None = None
    state: str
    state_reason: str | None = None
    locked: bool = False
    active_lock_reason: str | None = None
    labels: list[str] = []
    assignees: list[str] = []
    milestone: MilestoneOut | None = None
    user: str | None = None
    comments: int = 0
    is_pull_request: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    html_url: str | None = None


class CommentOut(BaseModel):
    id: int
    body: str | None = None
    user: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    html_url: str | None = None


class ReviewCommentOut(CommentOut):
    path: str | None = None
    line: int | None = None
    side: str | None = None
    start_line: int | None = None
    commit_id: str | None = None
    in_reply_to_id: int | None = None
    pull_request_review_id: int | None = None
    diff_hunk: str | None = None


class IssueDetail(IssueOut):
    comment_list: list[CommentOut] = []
    comments_truncated: bool = False
    links: list[IssueLinkOut] = []


class ListPage[T](BaseModel):
    """One GitHub page: `has_next` comes from the Link header."""

    items: list[T]
    page: int
    per_page: int
    has_next: bool


class BranchRef(BaseModel):
    ref: str | None = None
    sha: str | None = None
    repo: str | None = None
    label: str | None = None


class PullOut(BaseModel):
    number: int
    node_id: str | None = None
    title: str
    body: str | None = None
    state: str
    draft: bool = False
    merged: bool = False
    locked: bool = False
    user: str | None = None
    head: BranchRef
    base: BranchRef
    labels: list[str] = []
    requested_reviewers: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    html_url: str | None = None


class PullDetail(PullOut):
    mergeable: bool | None = None
    mergeable_state: str | None = None
    rebaseable: bool | None = None
    commits: int | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    comments: int | None = None
    review_comments: int | None = None


class PullFileOut(BaseModel):
    filename: str
    status: str | None = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    sha: str | None = None
    previous_filename: str | None = None
    patch: str | None = None
    patch_truncated: bool = False


class DiffOut(BaseModel):
    number: int
    diff: str
    bytes_returned: int
    max_bytes: int
    # True when the diff was cut at GITHUB_DIFF_MAX_BYTES, or GitHub refused it as too large.
    truncated: bool
    too_large_upstream: bool = False


class ReviewOut(BaseModel):
    id: int
    user: str | None = None
    state: str | None = None
    body: str | None = None
    commit_id: str | None = None
    submitted_at: datetime | None = None
    html_url: str | None = None


class MergeStatusOut(BaseModel):
    number: int
    state: str
    merged: bool
    draft: bool
    mergeable: bool | None
    mergeable_state: str | None
    rebaseable: bool | None
    head_ref: str | None
    head_sha: str | None
    base_ref: str | None


class IssueLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_full_name: str
    issue_number: int
    issue_node_id: str | None
    task_ref: str
    task_kind: str
    created_by: uuid.UUID | None
    created_at: datetime


class TrackedOperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    op: str
    repo: str
    target: str | None
    request_payload: dict[str, Any]
    status: str
    approval_request_id: uuid.UUID | None
    github_response_summary: dict[str, Any] | None
    error: str | None
    actor: uuid.UUID | None
    created_at: datetime
    finished_at: datetime | None


class GatedOut(BaseModel):
    """202 answer of a gated endpoint: nothing was sent to GitHub yet."""

    approval_id: uuid.UUID
    status: str
    kind: str
    target: str
    artifact_sha256: str | None
    expires_at: datetime
    operation_id: int
    artifact: dict[str, Any] = {}


# --- inputs -------------------------------------------------------------------------------

Login = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+(\[bot\])?$")
LabelName = Field(min_length=1, max_length=100)
StateReason = Literal["completed", "not_planned", "reopened"]


class IssueCreate(Strict):
    title: str = Field(min_length=1, max_length=256)
    body: str | None = Body
    labels: list[str] | None = Field(default=None, max_length=100)
    assignees: list[str] | None = Field(default=None, max_length=10)
    milestone: int | None = Field(default=None, ge=1)


class IssueUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Body
    state: Literal["open", "closed"] | None = None
    state_reason: StateReason | None = None
    labels: list[str] | None = Field(default=None, max_length=100)
    assignees: list[str] | None = Field(default=None, max_length=10)
    # null clears the milestone; omit the key to leave it.
    milestone: int | None = Field(default=None, ge=1)


class IssueClose(Strict):
    state_reason: Literal["completed", "not_planned"] = "completed"


class CommentCreate(Strict):
    body: str = Field(min_length=1, max_length=65536)


class LabelsAdd(Strict):
    labels: list[str] = Field(min_length=1, max_length=100)


class LockIn(Strict):
    lock_reason: Literal["off-topic", "too heated", "resolved", "spam"] | None = None


class LinkCreate(Strict):
    task_ref: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-/]+$")
    task_kind: str = Field(default="spasta_task", min_length=1, max_length=50,
                           pattern=r"^[a-z0-9_]+$")  # fmt: skip


class PullCreate(Strict):
    head: str = Field(min_length=1, max_length=356)
    base: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=256)
    body: str | None = Body
    draft: bool = False


class PullUpdate(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Body
    base: str | None = Field(default=None, min_length=1, max_length=255)
    state: Literal["open", "closed"] | None = None


class ReviewCommentCreate(Strict):
    body: str = Field(min_length=1, max_length=65536)
    commit_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    path: str | None = Field(default=None, max_length=1024)
    line: int | None = Field(default=None, ge=1)
    side: Literal["LEFT", "RIGHT"] | None = None
    start_line: int | None = Field(default=None, ge=1)
    start_side: Literal["LEFT", "RIGHT"] | None = None
    in_reply_to: int | None = Field(default=None, ge=1)


class ReviewDraftComment(Strict):
    path: str = Field(min_length=1, max_length=1024)
    body: str = Field(min_length=1, max_length=65536)
    line: int | None = Field(default=None, ge=1)
    side: Literal["LEFT", "RIGHT"] | None = None
    start_line: int | None = Field(default=None, ge=1)


class ReviewCreate(Strict):
    event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"]
    body: str | None = Body
    commit_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    comments: list[ReviewDraftComment] | None = Field(default=None, max_length=50)


Sha = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class MergeRequest(Strict):
    merge_method: Literal["merge", "squash", "rebase"] = "merge"
    expected_head_sha: str = Sha
    commit_title: str | None = Field(default=None, max_length=256)
    commit_message: str | None = Field(default=None, max_length=65536)


class ForcePushRequest(Strict):
    branch: str = Field(min_length=1, max_length=255)
    sha: str = Sha


class ProtectionUpdate(Strict):
    """The body of GitHub's PUT /branches/{branch}/protection, passed through as given."""

    required_status_checks: dict[str, Any] | None
    enforce_admins: bool | None
    required_pull_request_reviews: dict[str, Any] | None
    restrictions: dict[str, Any] | None
    required_linear_history: bool | None = None
    allow_force_pushes: bool | None = None
    allow_deletions: bool | None = None
    block_creations: bool | None = None
    required_conversation_resolution: bool | None = None
    lock_branch: bool | None = None
    allow_fork_syncing: bool | None = None


class RepoSettingsUpdate(Strict):
    visibility: Literal["public", "private", "internal"] | None = None
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    allow_merge_commit: bool | None = None
    allow_squash_merge: bool | None = None
    allow_rebase_merge: bool | None = None
    allow_auto_merge: bool | None = None
    allow_update_branch: bool | None = None
    delete_branch_on_merge: bool | None = None
    has_issues: bool | None = None
    has_wiki: bool | None = None
    has_projects: bool | None = None
    archived: bool | None = None


class ActionsPermissionsUpdate(Strict):
    enabled: bool
    allowed_actions: Literal["all", "local_only", "selected"] | None = None


IssueDetail.model_rebuild()
