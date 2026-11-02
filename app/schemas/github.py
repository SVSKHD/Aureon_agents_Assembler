from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RateLimitOut(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    used: int | None = None
    reset_at: datetime | None = None
    resource: str | None = None
    observed_at: datetime | None = None


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    repos_seen: int
    repos_upserted: int
    repos_removed: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ConnectionOut(BaseModel):
    """Connection health. Never carries the token, only its sha256 prefix."""

    configured: bool
    api_url: str
    fingerprint: str | None = None
    token_kind: str | None = None
    login: str | None = None
    scopes: list[str] = []
    ok: bool | None = None
    detail: str | None = None
    last_checked_at: datetime | None = None
    rate_limit: RateLimitOut | None = None
    allowed_owners: list[str] = []
    allowed_repos: list[str] = []
    last_sync: SyncRunOut | None = None


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    owner: str
    name: str
    description: str | None
    private: bool
    default_branch: str | None
    archived: bool
    pushed_at: datetime | None
    html_url: str
    synced_at: datetime


class RepositoryDetail(BaseModel):
    id: int
    full_name: str
    owner: str
    name: str
    description: str | None = None
    private: bool
    visibility: str | None = None
    default_branch: str | None = None
    archived: bool = False
    disabled: bool = False
    fork: bool = False
    language: str | None = None
    size_kb: int | None = None
    open_issues_count: int | None = None
    pushed_at: datetime | None = None
    updated_at: datetime | None = None
    html_url: str
    permissions: dict[str, bool] = {}


class BranchOut(BaseModel):
    name: str
    sha: str | None
    protected: bool


class CommitOut(BaseModel):
    sha: str
    message: str
    author_name: str | None = None
    author_login: str | None = None
    authored_at: datetime | None = None
    committer_name: str | None = None
    committed_at: datetime | None = None
    html_url: str | None = None


class TreeEntry(BaseModel):
    path: str
    type: str
    mode: str | None = None
    sha: str | None = None
    size: int | None = None


class TreeOut(BaseModel):
    ref: str
    path: str
    sha: str | None
    recursive: bool
    # True when GitHub truncated the tree or it was cut at GITHUB_MAX_TREE_ENTRIES.
    truncated: bool
    entries: list[TreeEntry]


class ContentEntry(BaseModel):
    name: str
    path: str
    type: str
    size: int | None = None
    sha: str | None = None


class ContentOut(BaseModel):
    path: str
    ref: str | None
    type: Literal["file", "dir", "symlink", "submodule"]
    name: str | None = None
    size: int | None = None
    sha: str | None = None
    html_url: str | None = None
    # file: "text" (decoded below), "binary" or "too_large" (metadata only)
    content_kind: Literal["text", "binary", "too_large"] | None = None
    encoding: Literal["utf-8"] | None = None
    text: str | None = None
    entries: list[ContentEntry] | None = None
    target: str | None = None  # symlink target / submodule url
