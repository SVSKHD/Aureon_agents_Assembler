"""The combined Actions monitor (docs/20-github-actions-webhooks.md, "Actions monitor").

One timeline of:
- host deploys: `deployer` agent runs written by app/workers/deploy_record.py around every
  deploy-poll.sh deploy (task `deploy:<target>`, details {target, sha, from_sha, log});
- GitHub Actions CI runs: github.workflow_runs, kept fresh by the webhook worker and the
  github_actions_sync job.

Read-only and DB-only: it never calls GitHub, so polling it costs no rate limit. CI rows are
filtered by the GitHub allowlist (D-15-3) at read time; with no allowlist set, the login of
the last successful connection check is used, never a live call.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agents import AgentRun
from app.models.github import GitHubConnection, GitHubWorkflowRun
from app.schemas.github_actions import MonitorItemOut, MonitorOut
from app.services import github as gh

DEPLOYER_ID = "deployer"
KINDS = ("deploy", "ci")


def deploy_target(settings: Settings, target: str | None) -> tuple[str | None, str | None]:
    """(repo, branch) for a deploy target from DEPLOY_TARGET_REPOS ("owner/repo@branch")."""
    spec = settings.DEPLOY_TARGET_REPOS.get(target or "") if target else None
    if not spec:
        return None, None
    repo, _, branch = str(spec).partition("@")
    return (repo or None), (branch or None)


def _deploy_status(status: str) -> str:
    return {"succeeded": "success", "failed": "failure"}.get(status, status)


def ci_status(status: str | None, conclusion: str | None) -> str:
    if status != "completed":
        return "running" if status == "in_progress" else ("queued" if status else "unknown")
    if conclusion == "success":
        return "success"
    if conclusion in ("cancelled", "skipped", "neutral", "stale"):
        return "cancelled"
    if conclusion in ("failure", "timed_out", "startup_failure", "action_required"):
        return "failure"
    return "unknown"


def deploy_item(settings: Settings, run: AgentRun) -> MonitorItemOut:
    d: dict[str, Any] = run.details or {}
    target = d.get("target") if isinstance(d.get("target"), str) else None
    if target is None and run.task.startswith("deploy:"):
        target = run.task.split(":", 1)[1]
    repo, branch = deploy_target(settings, target)
    sha = d.get("sha") if isinstance(d.get("sha"), str) else None
    url = f"https://github.com/{repo}/commit/{sha}" if repo and sha else None
    return MonitorItemOut(
        kind="deploy",
        id=f"deploy:{run.id}",
        name=run.task,
        repo=repo,
        branch=branch,
        sha=sha,
        status=_deploy_status(run.status),
        raw_status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        url=url,
        error=run.error,
        rolled_back=bool(d["rolled_back"]) if "rolled_back" in d else None,
    )


def ci_item(run: GitHubWorkflowRun) -> MonitorItemOut:
    finished = run.updated_at if run.status == "completed" else None
    duration = None
    if finished and run.created_at:
        duration = max(0, int((finished - run.created_at).total_seconds() * 1000))
    return MonitorItemOut(
        kind="ci",
        id=f"ci:{run.id}",
        name=run.name or run.path or f"run {run.id}",
        repo=run.repo,
        branch=run.branch,
        sha=run.head_sha,
        status=ci_status(run.status, run.conclusion),
        raw_status=run.status,
        conclusion=run.conclusion,
        event=run.event,
        started_at=run.created_at,
        finished_at=finished,
        duration_ms=duration,
        url=run.html_url,
    )


async def _login(db: AsyncSession) -> str | None:
    return await db.scalar(
        select(GitHubConnection.login)
        .where(GitHubConnection.ok.is_(True), GitHubConnection.login.is_not(None))
        .order_by(GitHubConnection.last_checked_at.desc().nulls_last())
        .limit(1)
    )


async def monitor(
    db: AsyncSession,
    settings: Settings,
    *,
    limit: int = 50,
    kind: str | None = None,
    repo: str | None = None,
) -> MonitorOut:
    items: list[MonitorItemOut] = []
    want = repo.lower() if repo else None
    if kind in (None, "deploy"):
        runs = (
            await db.scalars(
                select(AgentRun)
                .where(AgentRun.agent_id == DEPLOYER_ID)
                .order_by(AgentRun.started_at.desc())
                .limit(limit)
            )
        ).all()
        for r in runs:
            it = deploy_item(settings, r)
            if want is None or (it.repo or "").lower() == want:
                items.append(it)
    if kind in (None, "ci"):
        allow = gh.Allowlist.from_settings(settings)
        login = None if (allow.repos or allow.owners) else await _login(db)
        q = select(GitHubWorkflowRun)
        if want:
            q = q.where(func.lower(GitHubWorkflowRun.repo) == want)
        # Over-fetch a little: rows outside the allowlist are dropped below.
        rows = (
            await db.scalars(
                q.order_by(GitHubWorkflowRun.created_at.desc().nulls_last()).limit(limit * 2)
            )
        ).all()
        n = 0
        for row in rows:
            if not allow.allows(row.repo, login):
                continue
            items.append(ci_item(row))
            n += 1
            if n >= limit:
                break
    items.sort(key=lambda i: i.started_at.timestamp() if i.started_at else 0.0, reverse=True)
    items = items[:limit]
    latest = max((i.started_at for i in items if i.started_at), default=None)
    return MonitorOut(items=items, latest_at=latest)
