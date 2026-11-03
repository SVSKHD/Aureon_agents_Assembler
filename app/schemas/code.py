"""Coding agent API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

RepoName = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class RepoOut(BaseModel):
    name: str
    base: str
    available: bool
    head: str | None = None
    branches: int | None = None
    tests: list[list[str]]
    detail: str | None = None


class TreeItem(BaseModel):
    name: str
    path: str
    type: str  # blob | tree | commit
    mode: str
    size: str


class FileOut(BaseModel):
    path: str
    ref: str
    size: int
    binary: bool
    truncated: bool
    content: str | None


class CommitOut(BaseModel):
    sha: str
    author: str
    date: str
    subject: str


class AnalyzeIn(BaseModel):
    repo: str = RepoName
    question: str = Field(min_length=1, max_length=8000)
    paths: list[str] = Field(default_factory=list, max_length=40)
    ref: str | None = Field(default=None, max_length=100)


class TaskIn(BaseModel):
    repo: str = RepoName
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=16000)
    paths: list[str] = Field(default_factory=list, max_length=40)


class ApproveIn(BaseModel):
    # Optional extra name for the approved commit: feature/<name> or fix/<name>.
    feature_branch: str | None = Field(default=None, max_length=100)


class CodeTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    repo: str
    title: str
    instruction: str
    paths: list[str]
    status: str
    base_ref: str
    base_sha: str | None
    branch: str | None
    commit_sha: str | None
    model: str | None
    result: dict[str, Any]
    tests: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    decided_at: datetime | None


class DiffOut(BaseModel):
    task_id: uuid.UUID
    files: list[str]
    diff: str


# --- git ops (clone / pull / push + PR) -----------------------------------------------------

FullName = Field(
    min_length=3, max_length=201, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$"
)


class GitRepoIn(BaseModel):
    repo: str = FullName


class GitPushIn(BaseModel):
    repo: str = FullName
    branch: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=20_000)


class GitOpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    op: str
    repo: str
    task_id: uuid.UUID | None = None
    branch: str | None
    title: str | None
    status: str
    head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    result: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class GitRepoStateOut(BaseModel):
    """One allowlisted repo: whether it is cloned, and the coding-repo name it has."""

    repo: str
    cloned: bool
    coding_repo: str
    default_branch: str | None
    push_prefixes: list[str]


class ApproveOut(CodeTaskOut):
    """The approved task plus the queued `task` git op (poll GET /api/code/git/ops/{id})."""

    git_op: GitOpOut
