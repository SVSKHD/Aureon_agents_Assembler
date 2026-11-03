"""GitHub Actions (docs/20-github-actions-webhooks.md). Admin only; POSTs need CSRF; repos
outside the allowlist are 404 and GitHub is not called for them.

    GET  …/{owner}/{repo}/actions/workflows
    GET  …/{owner}/{repo}/actions/runs                 ?workflow&branch&status&per_page&page
    GET  …/{owner}/{repo}/actions/runs/{run_id}
    GET  …/{owner}/{repo}/actions/runs/{run_id}/jobs
    GET  …/{owner}/{repo}/actions/runs/{run_id}/logs   text/plain, sanitised, tail-first
    GET  …/{owner}/{repo}/actions/jobs/{job_id}/logs   text/plain, sanitised, tail-first
    POST …/{owner}/{repo}/actions/workflows/{workflow}/dispatch   {ref, inputs}
    POST …/{owner}/{repo}/actions/runs/{run_id}/rerun | /rerun-failed | /cancel
    GET  /api/v1/github/actions/monitor   ?limit&kind&repo   deploys + CI runs, one timeline

Dispatch and re-run need GITHUB_DISPATCH_ALLOWLIST (else 403). Production workflows answer
202 with an approval request (nothing sent to GitHub until approved); the others answer
202 `sent`, tracked in github.tracked_operations.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import PlainTextResponse

from app.api.v1.github_issues import PageNo, PerPage, RepoDep, errors
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.integrations.github.logs import LogArchiveError, SanitizedLog
from app.schemas.github_actions import (
    ActionOut,
    DispatchRequest,
    JobOut,
    MonitorOut,
    RunOut,
    WorkflowOut,
)
from app.schemas.github_ops import ListPage
from app.services import actions_monitor
from app.services import github_actions as actions

router = APIRouter(prefix="/v1/github", tags=["github"])

A = "/repositories/{owner}/{repo}/actions"
RunId = Annotated[int, Path(ge=1, le=2**63 - 1)]
JobId = Annotated[int, Path(ge=1, le=2**63 - 1)]
Workflow = Annotated[str, Path(min_length=1, max_length=120)]
RunStatus = Literal[
    "completed",
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "success",
    "timed_out",
    "in_progress",
    "queued",
    "requested",
    "waiting",
    "pending",
]
ACCEPTED = status.HTTP_202_ACCEPTED
TEXT: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/plain": {}}, "description": "sanitised log text"}
}


@contextlib.contextmanager
def actions_errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except actions.ActionsRefused as exc:
            raise HTTPException(exc.status, {"code": exc.code, "detail": exc.message}) from None
        except LogArchiveError as exc:
            code = 413 if exc.code != "log_archive_invalid" else 502
            raise HTTPException(code, {"code": exc.code, "detail": exc.message}) from None


def _text(out: SanitizedLog) -> PlainTextResponse:
    return PlainTextResponse(
        out.text,
        headers={
            "X-Log-Truncated": "true" if out.truncated else "false",
            "X-Log-Original-Bytes": str(out.original_bytes),
            "Cache-Control": "no-store",
        },
    )


@router.get(A + "/workflows", response_model=list[WorkflowOut])
async def list_workflows(r: RepoDep) -> list[WorkflowOut]:
    with actions_errors():
        return await actions.list_workflows(r.client, r.full, r.settings)


@router.get(A + "/runs", response_model=ListPage[RunOut])
async def list_runs(
    r: RepoDep,
    workflow: Annotated[str | None, Query(max_length=120)] = None,
    branch: Annotated[str | None, Query(max_length=255)] = None,
    status: RunStatus | None = None,
    per_page: PerPage = 30,
    page: PageNo = 1,
) -> ListPage[RunOut]:
    with actions_errors():
        return await actions.list_runs(
            r.client,
            r.full,
            workflow=workflow,
            branch=branch,
            status=status,
            per_page=per_page,
            page=page,
        )


@router.get(A + "/runs/{run_id}", response_model=RunOut)
async def get_run(r: RepoDep, run_id: RunId) -> RunOut:
    with actions_errors():
        return actions.run_out(await actions.get_run(r.client, r.full, run_id))


@router.get(A + "/runs/{run_id}/jobs", response_model=list[JobOut])
async def run_jobs(r: RepoDep, run_id: RunId) -> list[JobOut]:
    with actions_errors():
        return await actions.run_jobs(r.client, r.full, run_id)


@router.get(A + "/runs/{run_id}/logs", response_class=PlainTextResponse, responses=TEXT)
async def run_logs(r: RepoDep, run_id: RunId) -> PlainTextResponse:
    with actions_errors():
        return _text(await actions.run_logs(r.client, r.full, run_id, r.settings))


@router.get(A + "/jobs/{job_id}/logs", response_class=PlainTextResponse, responses=TEXT)
async def job_logs(r: RepoDep, job_id: JobId) -> PlainTextResponse:
    with actions_errors():
        return _text(await actions.job_logs(r.client, r.full, job_id, r.settings))


@router.post(A + "/workflows/{workflow}/dispatch", response_model=ActionOut, status_code=ACCEPTED)
async def dispatch(r: RepoDep, workflow: Workflow, body: DispatchRequest) -> ActionOut:
    with actions_errors():
        return await actions.dispatch(
            r.maker,
            r.gate,
            r.client,
            r.actor,
            r.settings,
            r.full,
            workflow,
            ref=body.ref,
            inputs=body.inputs,
        )


@router.post(A + "/runs/{run_id}/rerun", response_model=ActionOut, status_code=ACCEPTED)
async def rerun(r: RepoDep, run_id: RunId) -> ActionOut:
    with actions_errors():
        return await actions.rerun(
            r.maker, r.gate, r.client, r.actor, r.settings, r.full, run_id, failed_only=False
        )


@router.post(A + "/runs/{run_id}/rerun-failed", response_model=ActionOut, status_code=ACCEPTED)
async def rerun_failed(r: RepoDep, run_id: RunId) -> ActionOut:
    with actions_errors():
        return await actions.rerun(
            r.maker, r.gate, r.client, r.actor, r.settings, r.full, run_id, failed_only=True
        )


@router.post(A + "/runs/{run_id}/cancel", response_model=ActionOut, status_code=ACCEPTED)
async def cancel(r: RepoDep, run_id: RunId) -> ActionOut:
    with actions_errors():
        return await actions.cancel(r.maker, r.client, r.actor, r.full, run_id)


@router.get("/actions/monitor", response_model=MonitorOut)
async def monitor(
    _: AdminRequired,
    db: DbSession,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    kind: Literal["deploy", "ci"] | None = None,
    repo: Annotated[str | None, Query(max_length=201)] = None,
) -> MonitorOut:
    """Host deploys (`deployer` agent runs) and GitHub Actions CI runs, newest first. DB only:
    never calls GitHub (the webhook worker and the sync job keep the runs fresh)."""
    return await actions_monitor.monitor(db, settings, limit=limit, kind=kind, repo=repo)
