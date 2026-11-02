"""GitHub Actions: workflows, runs, jobs, sanitised logs, allowlisted dispatch / re-run,
cancel, the run/job tables and the scheduled sync (docs/20-github-actions-webhooks.md).

- Reads go through the Phase 15 client and the repo allowlist (checked by the router).
- Logs: GitHub answers a 302 to a pre-signed blob URL. That URL is fetched without the token,
  only from GITHUB_LOG_HOSTS, with a byte cap; run logs are a zip unpacked in memory under
  size / entry caps. The text is ANSI-stripped, cut tail-first, redacted and capped.
- Dispatch and re-run are allowed only for workflows in GITHUB_DISPATCH_ALLOWLIST
  ("owner/repo:workflow_file"); anything else is 403 before a write is sent. A production
  workflow (GITHUB_PROD_WORKFLOWS, or "deploy"/"prod" in its name or file) is never sent
  directly: it is filed with the ApprovalGate (kind `github.workflow_dispatch_prod`) and runs
  from the executor below once an admin approves. Everything else runs at once through
  `tracked_write` (a github.tracked_operations row + an audit event).
- Cancel stops a run; it needs only the repo allowlist (stopping is the safe direction).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.client import (
    REDIRECT_STATUSES,
    GitHubClient,
    GitHubError,
    GitHubNotFound,
)
from app.integrations.github.logs import LogArchiveError, SanitizedLog, sanitize, unzip_logs
from app.integrations.github.redact import redact_obj
from app.models.approvals import ApprovalRequest
from app.models.github import GitHubRepository, GitHubWorkflowJob, GitHubWorkflowRun
from app.schemas.github_actions import ActionOut, JobOut, RunOut, StepOut, WorkflowOut
from app.schemas.github_ops import ListPage
from app.services import github as gh
from app.services import github_ops as ops
from app.services.approvals import (
    REGISTRY,
    ApprovalError,
    ApprovalGate,
    ExecContext,
    ExecutorRegistry,
    ExecutorSpec,
)

log = get_logger("services.github_actions")

DISPATCH_KIND = "github.workflow_dispatch_prod"
PROD_KEYWORDS = ("deploy", "prod")
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
RUN_STATUSES = (
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
)
# A workflow id, or a workflow file name.
WORKFLOW_RE = re.compile(r"^(?:[0-9]{1,20}|[A-Za-z0-9_.-]{1,100}\.ya?ml)$")
MAX_STEPS = 200
SYNC_MAX_REPOS = 50
ACTION_OPS = {
    "dispatch": "actions.workflow_dispatch",
    "rerun": "actions.run_rerun",
    "rerun_failed": "actions.run_rerun_failed",
    "cancel": "actions.run_cancel",
}


class ActionsRefused(Exception):
    """A dispatch / re-run refused by policy (403) or state (409); nothing was sent."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- policy -------------------------------------------------------------------------------


def check_workflow(workflow: str) -> str:
    if not WORKFLOW_RE.fullmatch(workflow):
        raise gh.InvalidInput("invalid_workflow", "workflow must be an id or a .yml file name")
    return workflow


def workflow_file(path: str | None) -> str:
    """`.github/workflows/ci.yml@refs/heads/main` -> `ci.yml`."""
    return (path or "").split("@", 1)[0].rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    allow: frozenset[str]
    prod: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> DispatchPolicy:
        return cls(
            frozenset(e.strip().lower() for e in settings.GITHUB_DISPATCH_ALLOWLIST if e.strip()),
            frozenset(e.strip().lower() for e in settings.GITHUB_PROD_WORKFLOWS if e.strip()),
        )

    def allowed(self, full: str, file: str) -> bool:
        return bool(file) and f"{full.lower()}:{file.lower()}" in self.allow

    def is_prod(self, name: str | None, file: str | None) -> bool:
        n, f = (name or "").strip().lower(), (file or "").strip().lower()
        stem = f.rsplit(".", 1)[0] if f else ""
        if {n, f, stem} & self.prod:
            return True
        return any(k in n or k in f for k in PROD_KEYWORDS)


# --- converters ---------------------------------------------------------------------------


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str(v: Any, limit: int = 500) -> str | None:
    return str(v)[:limit] if isinstance(v, str | int) and not isinstance(v, bool) else None


def workflow_out(full: str, w: dict[str, Any], policy: DispatchPolicy) -> WorkflowOut:
    path = str(w.get("path") or "")
    file = workflow_file(path)
    name = str(w.get("name") or file)
    return WorkflowOut(
        id=int(w["id"]),
        name=name,
        path=path,
        file=file,
        state=w.get("state"),
        html_url=w.get("html_url"),
        dispatch_allowed=policy.allowed(full, file),
        production=policy.is_prod(name, file),
    )


def run_out(r: dict[str, Any]) -> RunOut:
    actor = r.get("actor") if isinstance(r.get("actor"), dict) else {}
    return RunOut(
        id=int(r["id"]),
        name=_str(r.get("name")),
        workflow_id=_int(r.get("workflow_id")),
        path=_str(r.get("path")),
        event=_str(r.get("event")),
        branch=_str(r.get("head_branch")),
        head_sha=_str(r.get("head_sha")),
        status=_str(r.get("status")),
        conclusion=_str(r.get("conclusion")),
        run_number=_int(r.get("run_number")),
        run_attempt=_int(r.get("run_attempt")),
        actor=_str((actor or {}).get("login")),
        html_url=_str(r.get("html_url"), 2000),
        created_at=_dt(r.get("created_at")),
        updated_at=_dt(r.get("updated_at")),
    )


def _steps(raw: Any) -> list[dict[str, Any]]:
    out = []
    for s in raw[:MAX_STEPS] if isinstance(raw, list) else []:
        if isinstance(s, dict):
            out.append(
                {
                    "number": _int(s.get("number")),
                    "name": _str(s.get("name"), 300),
                    "status": _str(s.get("status")),
                    "conclusion": _str(s.get("conclusion")),
                    "started_at": _str(s.get("started_at")),
                    "completed_at": _str(s.get("completed_at")),
                }
            )
    return out


def job_out(j: dict[str, Any]) -> JobOut:
    labels = j.get("labels") if isinstance(j.get("labels"), list) else []
    return JobOut(
        id=int(j["id"]),
        run_id=_int(j.get("run_id")),
        name=_str(j.get("name")),
        status=_str(j.get("status")),
        conclusion=_str(j.get("conclusion")),
        run_attempt=_int(j.get("run_attempt")),
        runner_name=_str(j.get("runner_name")),
        labels=[str(x) for x in labels or [] if isinstance(x, str)][:50],
        html_url=_str(j.get("html_url"), 2000),
        started_at=_dt(j.get("started_at")),
        completed_at=_dt(j.get("completed_at")),
        steps=[StepOut.model_validate(s) for s in _steps(j.get("steps"))],
    )


# --- reads --------------------------------------------------------------------------------


async def list_workflows(client: GitHubClient, full: str, settings: Settings) -> list[WorkflowOut]:
    policy = DispatchPolicy.from_settings(settings)
    items = await client.paginate(f"/repos/{full}/actions/workflows")
    return [workflow_out(full, w, policy) for w in items if isinstance(w, dict) and "id" in w]


async def list_runs(
    client: GitHubClient,
    full: str,
    *,
    workflow: str | None,
    branch: str | None,
    status: str | None,
    per_page: int,
    page: int,
) -> ListPage[RunOut]:
    params: dict[str, Any] = {"per_page": per_page, "page": page}
    if branch:
        params["branch"] = gh.check_ref(branch, "branch")
    if status:
        params["status"] = status
    path = f"/repos/{full}/actions/runs"
    if workflow:
        path = f"/repos/{full}/actions/workflows/{check_workflow(workflow)}/runs"
    resp = await client.request("GET", path, params=params)
    data = resp.data if isinstance(resp.data, dict) else {}
    runs = data.get("workflow_runs") if isinstance(data.get("workflow_runs"), list) else []
    return ListPage[RunOut](
        items=[run_out(r) for r in runs or [] if isinstance(r, dict) and "id" in r],
        page=page,
        per_page=per_page,
        has_next=ops._has_next(resp.headers),
    )


async def get_run(client: GitHubClient, full: str, run_id: int) -> dict[str, Any]:
    return ops._obj(await client.get_json(f"/repos/{full}/actions/runs/{run_id}", cache=False))


async def run_jobs(client: GitHubClient, full: str, run_id: int) -> list[JobOut]:
    items = await client.paginate(
        f"/repos/{full}/actions/runs/{run_id}/jobs", {"filter": "latest"}, max_pages=5
    )
    return [job_out(j) for j in items if isinstance(j, dict) and "id" in j]


# --- logs ---------------------------------------------------------------------------------


async def _download(
    client: GitHubClient, settings: Settings, path: str, *, tail_bytes: int | None
) -> tuple[bytes, bool]:
    """GET a logs endpoint: follow its redirect to the log blob (log hosts only, no token)."""
    cap = settings.GITHUB_LOG_DOWNLOAD_MAX_BYTES
    resp = await client.request("GET", path, raw=True, max_bytes=cap, return_redirect=True)
    if resp.status in REDIRECT_STATUSES and isinstance(resp.data, str):
        return await client.fetch_log_blob(
            resp.data,
            allowed_hosts=settings.GITHUB_LOG_HOSTS,
            max_bytes=cap,
            tail_bytes=tail_bytes,
        )
    data = resp.data if isinstance(resp.data, bytes) else b""
    if len(data) > cap:
        data = data[-tail_bytes:] if tail_bytes else data[:cap]
        return data, True
    return data, False


async def job_logs(
    client: GitHubClient, full: str, job_id: int, settings: Settings
) -> SanitizedLog:
    raw, truncated = await _download(
        client,
        settings,
        f"/repos/{full}/actions/jobs/{job_id}/logs",
        tail_bytes=2 * settings.GITHUB_LOG_MAX_BYTES,
    )
    return await _sanitize(client, raw.decode("utf-8", "replace"), settings, truncated)


async def run_logs(
    client: GitHubClient, full: str, run_id: int, settings: Settings
) -> SanitizedLog:
    raw, truncated = await _download(
        client, settings, f"/repos/{full}/actions/runs/{run_id}/logs", tail_bytes=None
    )
    if truncated:
        raise LogArchiveError(
            "log_archive_too_large",
            f"the run-log archive is over {settings.GITHUB_LOG_DOWNLOAD_MAX_BYTES} bytes; "
            "read the job logs instead",
        )
    text, capped = await asyncio.to_thread(
        unzip_logs,
        raw,
        max_total=settings.GITHUB_LOG_UNZIP_MAX_BYTES,
        max_entries=settings.GITHUB_LOG_ZIP_MAX_ENTRIES,
    )
    return await _sanitize(client, text, settings, capped)


async def _sanitize(
    client: GitHubClient, text: str, settings: Settings, truncated: bool
) -> SanitizedLog:
    out = await asyncio.to_thread(
        sanitize, text, max_bytes=settings.GITHUB_LOG_MAX_BYTES, truncated=truncated
    )
    # The configured token itself, wherever it might still appear.
    return SanitizedLog(client.redact(out.text), out.truncated, out.original_bytes)


# --- dispatch / rerun / cancel ------------------------------------------------------------


def _refuse_unlisted(full: str, file: str) -> ActionsRefused:
    return ActionsRefused(
        403,
        "workflow_not_allowlisted",
        f"{full}:{file or '?'} is not in GITHUB_DISPATCH_ALLOWLIST",
    )


async def ref_sha(client: GitHubClient, full: str, ref: str) -> str | None:
    try:
        c = ops._obj(
            await client.get_json(f"/repos/{full}/commits/{quote(ref, safe='/')}", cache=False)
        )
    except GitHubNotFound:
        return None
    sha = c.get("sha")
    return str(sha) if isinstance(sha, str) else None


async def dispatch_artifact(client: GitHubClient, payload: dict[str, Any]) -> dict[str, Any]:
    """What the approver sees, recomputed at approval: a change of the ref's commit, the
    workflow file or the run's attempt invalidates the request."""
    full = str(payload["repo"])
    action = str(payload["action"])
    if action == "dispatch":
        wf = ops._obj(
            await client.get_json(
                f"/repos/{full}/actions/workflows/{payload['workflow_id']}", cache=False
            )
        )
        return {
            "repo": full,
            "action": action,
            "workflow_id": payload["workflow_id"],
            "workflow_path": wf.get("path"),
            "workflow_state": wf.get("state"),
            "ref": payload["ref"],
            "ref_sha": await ref_sha(client, full, str(payload["ref"])),
            "inputs": payload.get("inputs") or {},
        }
    run = await get_run(client, full, int(payload["run_id"]))
    return {
        "repo": full,
        "action": action,
        "run_id": payload["run_id"],
        "workflow_file": workflow_file(run.get("path")),
        "head_sha": run.get("head_sha"),
        "run_attempt": run.get("run_attempt"),
        "status": run.get("status"),
    }


async def _send_or_gate(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: ops.Actor,
    *,
    prod: bool,
    payload: dict[str, Any],
    target: str,
    method_path: str,
    body: Any,
) -> ActionOut:
    full, action = str(payload["repo"]), str(payload["action"])
    op = ACTION_OPS[action]
    if prod:
        gated = await ops.file_gated(
            maker,
            gate,
            client,
            actor,
            kind=DISPATCH_KIND,
            payload=payload,
            artifact=await dispatch_artifact(client, payload),
            op=op,
            target=target,
        )
        log.info("github_actions_gated", action=action, repo=full, target=target)
        return ActionOut(
            status="pending_approval", action=action, repo=full, target=target, approval=gated
        )
    await ops.tracked_write(
        maker, client, actor, op=op, repo=full, target=target, method="POST", path=method_path,
        json=body,
    )  # fmt: skip
    return ActionOut(status="sent", action=action, repo=full, target=target)


async def dispatch(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: ops.Actor,
    settings: Settings,
    full: str,
    workflow: str,
    *,
    ref: str,
    inputs: dict[str, Any],
) -> ActionOut:
    check_workflow(workflow)
    gh.check_ref(ref)
    policy = DispatchPolicy.from_settings(settings)
    if not workflow.isdigit() and not policy.allowed(full, workflow):
        raise _refuse_unlisted(full, workflow)  # refused before any GitHub call
    wf = ops._obj(await client.get_json(f"/repos/{full}/actions/workflows/{workflow}", cache=False))
    file = workflow_file(str(wf.get("path") or ""))
    if not policy.allowed(full, file):
        raise _refuse_unlisted(full, file)
    if wf.get("state") not in (None, "active"):
        raise ActionsRefused(409, "workflow_not_active", f"workflow is {wf.get('state')}")
    name = str(wf.get("name") or file)
    wid = int(wf["id"])
    payload = {
        "repo": full,
        "action": "dispatch",
        "workflow_id": wid,
        "workflow_file": file,
        "workflow_name": name,
        "ref": ref,
        "inputs": inputs,
    }
    return await _send_or_gate(
        maker,
        gate,
        client,
        actor,
        prod=policy.is_prod(name, file),
        payload=payload,
        target=f"workflow:{file}@{ref}",
        method_path=f"/repos/{full}/actions/workflows/{wid}/dispatches",
        body={"ref": ref, "inputs": inputs},
    )


async def _run_workflow(client: GitHubClient, full: str, run: dict[str, Any]) -> tuple[str, str]:
    """(file, name) of a run's workflow; the workflow is fetched when the run has no path."""
    file = workflow_file(_str(run.get("path")))
    name = str(run.get("name") or "")
    if not file and _int(run.get("workflow_id")):
        wf = ops._obj(
            await client.get_json(
                f"/repos/{full}/actions/workflows/{run['workflow_id']}", cache=False
            )
        )
        file = workflow_file(_str(wf.get("path")))
        name = name or str(wf.get("name") or "")
    return file, name or file


async def rerun(
    maker: async_sessionmaker[AsyncSession],
    gate: ApprovalGate,
    client: GitHubClient,
    actor: ops.Actor,
    settings: Settings,
    full: str,
    run_id: int,
    *,
    failed_only: bool,
) -> ActionOut:
    policy = DispatchPolicy.from_settings(settings)
    run = await get_run(client, full, run_id)
    file, name = await _run_workflow(client, full, run)
    if not policy.allowed(full, file):
        raise _refuse_unlisted(full, file)
    if run.get("status") != "completed":
        raise ActionsRefused(409, "run_not_completed", "only a completed run can be re-run")
    action = "rerun_failed" if failed_only else "rerun"
    suffix = "rerun-failed-jobs" if failed_only else "rerun"
    payload = {
        "repo": full,
        "action": action,
        "run_id": run_id,
        "workflow_file": file,
        "workflow_name": name,
    }
    return await _send_or_gate(
        maker,
        gate,
        client,
        actor,
        prod=policy.is_prod(name, file),
        payload=payload,
        target=f"run:{run_id}",
        method_path=f"/repos/{full}/actions/runs/{run_id}/{suffix}",
        body=None,
    )


async def cancel(
    maker: async_sessionmaker[AsyncSession],
    client: GitHubClient,
    actor: ops.Actor,
    full: str,
    run_id: int,
) -> ActionOut:
    await ops.tracked_write(
        maker, client, actor, op=ACTION_OPS["cancel"], repo=full, target=f"run:{run_id}",
        method="POST", path=f"/repos/{full}/actions/runs/{run_id}/cancel",
    )  # fmt: skip
    return ActionOut(status="sent", action="cancel", repo=full, target=f"run:{run_id}")


# --- the gated executor -------------------------------------------------------------------


async def _recheck(ctx: ExecContext, req: ApprovalRequest) -> tuple[GitHubClient, str]:
    client, full = await ops._recheck(ctx, req)
    file = str(req.payload.get("workflow_file") or "")
    if not DispatchPolicy.from_settings(ctx.settings).allowed(full, file):
        raise ApprovalError(403, "workflow_not_allowlisted", f"{full}:{file} is not allowlisted")
    return client, full


async def _current_artifact(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    client, _full = await _recheck(ctx, req)
    return await dispatch_artifact(client, req.payload)


async def _execute(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    client, full = await _recheck(ctx, req)
    p = req.payload
    action = p["action"]
    if action == "dispatch":
        resp = await client.request(
            "POST",
            f"/repos/{full}/actions/workflows/{int(p['workflow_id'])}/dispatches",
            json={"ref": p["ref"], "inputs": p.get("inputs") or {}},
        )
    elif action in ("rerun", "rerun_failed"):
        suffix = "rerun-failed-jobs" if action == "rerun_failed" else "rerun"
        resp = await client.request(
            "POST", f"/repos/{full}/actions/runs/{int(p['run_id'])}/{suffix}"
        )
    else:  # pragma: no cover - only these actions are ever filed
        raise ApprovalError(500, "unknown_action", str(action))
    log.info("github_actions_gated_executed", action=action, repo=full, status=resp.status)
    out: dict[str, Any] = redact_obj(ops.summarize(resp.status, resp.data))
    return out


def register_executors(registry: ExecutorRegistry = REGISTRY) -> None:
    registry.register(
        ExecutorSpec(
            kind=DISPATCH_KIND,
            execute=_execute,
            current_artifact=_current_artifact,
            on_status=ops._on_status,
            requires=("github",),
        )
    )


register_executors()


# --- tables -------------------------------------------------------------------------------


def run_values(full: str, r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "repo": full[:201],
        "workflow_id": _int(r.get("workflow_id")),
        "name": _str(r.get("name")),
        "path": _str(r.get("path")),
        "event": _str(r.get("event"), 64),
        "branch": _str(r.get("head_branch"), 255),
        "head_sha": _str(r.get("head_sha"), 64),
        "status": _str(r.get("status"), 32),
        "conclusion": _str(r.get("conclusion"), 32),
        "run_attempt": _int(r.get("run_attempt")),
        "html_url": _str(r.get("html_url"), 2000),
        "created_at": _dt(r.get("created_at")),
        "updated_at": _dt(r.get("updated_at")),
    }


def job_values(full: str | None, j: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(j["id"]),
        "run_id": int(j["run_id"]),
        "repo": full[:201] if full else None,
        "name": _str(j.get("name")),
        "status": _str(j.get("status"), 32),
        "conclusion": _str(j.get("conclusion"), 32),
        "run_attempt": _int(j.get("run_attempt")),
        "html_url": _str(j.get("html_url"), 2000),
        "started_at": _dt(j.get("started_at")),
        "completed_at": _dt(j.get("completed_at")),
        "steps": _steps(j.get("steps")),
    }


async def upsert_run(db: AsyncSession, values: dict[str, Any]) -> None:
    """Insert or update; an older `updated_at` (an out-of-order event) never overwrites."""
    t = GitHubWorkflowRun
    ins = insert(t).values(**values, synced_at=func.now())
    await db.execute(
        ins.on_conflict_do_update(
            index_elements=[t.id],
            set_={k: ins.excluded[k] for k in values if k != "id"} | {"synced_at": func.now()},
            where=or_(
                t.updated_at.is_(None),
                ins.excluded.updated_at.is_(None),
                ins.excluded.updated_at >= t.updated_at,
            ),
        )
    )


async def upsert_job(db: AsyncSession, values: dict[str, Any]) -> None:
    """Insert or update; a completed job is not moved back by a late in_progress event of
    the same attempt."""
    t = GitHubWorkflowJob
    ins = insert(t).values(**values, synced_at=func.now())
    regress = and_(
        t.status == "completed",
        ins.excluded.status != "completed",
        func.coalesce(ins.excluded.run_attempt, 0) <= func.coalesce(t.run_attempt, 0),
    )
    await db.execute(
        ins.on_conflict_do_update(
            index_elements=[t.id],
            set_={k: ins.excluded[k] for k in values if k != "id"} | {"synced_at": func.now()},
            where=not_(regress),
        )
    )


async def sync_actions(
    maker: async_sessionmaker[AsyncSession], client: GitHubClient, settings: Settings
) -> dict[str, Any]:
    """Recent runs of every allowlisted (synced, not archived) repo -> github.workflow_runs."""
    async with maker() as db:
        login = await gh.known_login(db, client)
        q = gh.visible_repos_query(settings, login).where(GitHubRepository.archived.is_(False))
        repos = [
            r.full_name
            for r in (
                await db.scalars(
                    q.order_by(GitHubRepository.pushed_at.desc().nulls_last()).limit(SYNC_MAX_REPOS)
                )
            ).all()
        ]
    upserted, skipped, errors = 0, [], []
    for full in repos:
        try:
            resp = await client.request(
                "GET",
                f"/repos/{full}/actions/runs",
                params={"per_page": settings.GITHUB_ACTIONS_SYNC_RUNS},
            )
        except GitHubNotFound:
            skipped.append(full)
            continue
        except GitHubError as exc:
            if exc.upstream_status == 403:  # Actions disabled / not permitted for the token
                skipped.append(full)
                continue
            errors.append(f"{full}: {exc.code}")
            continue
        data = resp.data if isinstance(resp.data, dict) else {}
        runs = [r for r in data.get("workflow_runs") or [] if isinstance(r, dict) and "id" in r]
        async with maker() as db, db.begin():
            for r in runs:
                await upsert_run(db, run_values(full, r))
        upserted += len(runs)
    log.info("github_actions_sync", repos=len(repos), runs=upserted, errors=len(errors))
    return {"repos": len(repos), "runs_upserted": upserted, "skipped": skipped, "errors": errors}


__all__ = [
    "DISPATCH_KIND",
    "FAILED_CONCLUSIONS",
    "RUN_STATUSES",
    "ActionsRefused",
    "DispatchPolicy",
    "cancel",
    "check_workflow",
    "dispatch",
    "job_logs",
    "job_values",
    "list_runs",
    "list_workflows",
    "rerun",
    "run_jobs",
    "run_logs",
    "run_values",
    "sync_actions",
    "upsert_job",
    "upsert_run",
    "workflow_file",
]
