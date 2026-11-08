"""DevOps monitoring: ingestion (docs/32-devops-monitoring.md).

Turns provider facts into devops.* rows and, only on a real state transition, one feed event
(devops.events). Every write is keyed by a stable provider identity (`dedup_key` /
`external_id`), so a webhook redelivery, an out-of-order delivery and the backfill all land
on the same rows and never notify twice.

Sources:
- GitHub webhooks (app/services/github_webhooks.py): pull_request, workflow_run,
  deployment, deployment_status.
- GitHub API backfill (`backfill`, scheduler job devops_backfill): recent PRs and
  deployments of the allowlisted repos; Actions runs come from github_actions_sync, which
  shares `on_workflow_run` with the webhook path.
- Host deploys (app/workers/deploy_record.py, called by scripts/ops/deploy-poll.sh).
- The host runtime collector (scripts/ops/runtime-collect.sh -> app/workers/runtime_record.py).

Statuses are what was verified, never inferred: a merged PR or a green CI run is not a
deployment, and a server deploy is `succeeded` only with a recorded health verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.client import GitHubClient, GitHubError, GitHubRateLimited
from app.models.devops import (
    Deployment,
    DevopsEvent,
    RepoEvent,
    Service,
    ServiceSnapshot,
)
from app.models.github import GitHubRepository
from app.services import github as gh
from app.services.actions_monitor import deploy_target

log = get_logger("services.devops")

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
UNHEALTHY = frozenset({"unhealthy", "stopped"})
MAX_TEXT = 300


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return None


def _s(v: Any, limit: int = 255) -> str | None:
    return str(v)[:limit] if isinstance(v, str | int) and not isinstance(v, bool) else None


def _login(v: Any) -> str | None:
    return _s(v.get("login"), 100) if isinstance(v, dict) else None


def one_line(v: Any, limit: int = MAX_TEXT) -> str | None:
    if v is None:
        return None
    text = " ".join(str(v).split())
    return text[:limit] or None


def fresh(settings: Settings, at: datetime | None) -> bool:
    """Only facts this recent create feed events (a first backfill must not flood the feed)."""
    if at is None:
        return False
    return at >= _now() - timedelta(hours=settings.DEVOPS_EVENT_MAX_AGE_HOURS)


# --- writes ---------------------------------------------------------------------------------


async def emit(
    db: AsyncSession,
    settings: Settings,
    *,
    type: str,
    dedup_key: str,
    title: str,
    severity: str,
    occurred_at: datetime | None,
    repo: str | None = None,
    branch: str | None = None,
    sha: str | None = None,
    environment: str | None = None,
    service: str | None = None,
    url: str | None = None,
    ref: Mapping[str, Any] | None = None,
) -> int | None:
    """One feed event per transition. Returns its id, or None when it already exists (a
    repeat) or the fact is too old to notify about."""
    if not fresh(settings, occurred_at):
        return None
    ins = (
        insert(DevopsEvent)
        .values(
            dedup_key=dedup_key[:300],
            type=type,
            severity=severity,
            repo=repo,
            branch=branch,
            sha=sha,
            environment=environment,
            service=service,
            title=title[:500],
            url=url,
            ref=dict(ref or {}),
            occurred_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=[DevopsEvent.dedup_key])
        .returning(DevopsEvent.id)
    )
    event_id: int | None = await db.scalar(ins)
    if event_id is not None:
        log.info("devops_event", type=type, repo=repo, service=service, id=event_id)
    return event_id


async def record_repo_event(db: AsyncSession, values: dict[str, Any]) -> bool:
    """Insert one normalized provider fact; False when it was already recorded."""
    ins = (
        insert(RepoEvent)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[RepoEvent.dedup_key])
        .returning(RepoEvent.id)
    )
    return await db.scalar(ins) is not None


# --- pull requests --------------------------------------------------------------------------


def pr_facts(
    full: str, pr: Mapping[str, Any], action: str | None, source: str, delivery_id: str | None
) -> list[dict[str, Any]]:
    """The repo events a PR payload (webhook) or PR list item (backfill) proves. Keys are the
    same from both sources, so they deduplicate against each other."""
    number = pr.get("number")
    if not isinstance(number, int):
        return []
    base = {
        "provider": "github",
        "repo": full[:201],
        "number": number,
        "title": one_line(pr.get("title"), 500),
        "actor": _login(pr.get("user")),
        "url": _s(pr.get("html_url"), 2000),
        "branch": _s((pr.get("base") or {}).get("ref"))
        if isinstance(pr.get("base"), dict)
        else None,
        "source": source,
        "delivery_id": delivery_id,
    }
    raw_head = pr.get("head")
    head: dict[str, Any] = raw_head if isinstance(raw_head, dict) else {}
    key = f"github:pr:{full}#{number}"
    out: list[dict[str, Any]] = []
    created = _dt(pr.get("created_at"))
    if created:  # every PR payload proves it was opened (same key from webhook and backfill)
        out.append({**base, "dedup_key": f"{key}:opened", "kind": "pr.opened", "state": "open",
                    "sha": _s(head.get("sha"), 64), "occurred_at": created,
                    "details": {"head_ref": _s(head.get("ref"))}})  # fmt: skip
    if action == "reopened":
        at = _dt(pr.get("updated_at")) or _now()
        out.append({**base, "dedup_key": f"{key}:reopened:{at.isoformat()}", "kind": "pr.reopened",
                    "state": "open", "sha": _s(head.get("sha"), 64), "occurred_at": at,
                    "details": {}})  # fmt: skip
    merged_at = _dt(pr.get("merged_at"))
    closed_at = _dt(pr.get("closed_at"))
    if action in (None, "closed"):
        if merged_at is not None:
            out.append({**base, "dedup_key": f"{key}:merged", "kind": "pr.merged",
                        "state": "merged", "sha": _s(pr.get("merge_commit_sha"), 64),
                        "actor": _login(pr.get("merged_by")) or base["actor"],
                        "occurred_at": merged_at,
                        "details": {"head_ref": _s(head.get("ref"))}})  # fmt: skip
        elif closed_at is not None:
            out.append({**base, "dedup_key": f"{key}:closed:{closed_at.isoformat()}",
                        "kind": "pr.closed", "state": "closed", "sha": _s(head.get("sha"), 64),
                        "occurred_at": closed_at, "details": {}})  # fmt: skip
    return out


async def on_pull_request(
    db: AsyncSession,
    settings: Settings,
    full: str,
    pr: Mapping[str, Any],
    action: str | None,
    *,
    source: str = "webhook",
    delivery_id: str | None = None,
) -> int:
    """Record what the PR payload proves; a merge emits `pr.merged` once. Returns the number
    of new repo events."""
    new = 0
    for fact in pr_facts(full, pr, action, source, delivery_id):
        new += await record_repo_event(db, fact)
        if fact["kind"] == "pr.merged":
            await emit(
                db, settings, type="pr.merged", dedup_key=f"pr.merged:{full}#{fact['number']}",
                severity="info", occurred_at=fact["occurred_at"], repo=full,
                branch=fact["branch"], sha=fact["sha"], url=fact["url"],
                title=f"PR #{fact['number']} merged into {fact['branch'] or '?'}: "
                f"{fact['title'] or ''}".strip(),
                ref={"pr": fact["number"]},
            )  # fmt: skip
    return new


# --- workflow runs (builds) -----------------------------------------------------------------


def build_state(status: str | None, conclusion: str | None) -> str:
    """queued | running | succeeded | failed | cancelled | unknown (CI status)."""
    if status in ("queued", "requested", "waiting", "pending"):
        return "queued"
    if status == "in_progress":
        return "running"
    if status != "completed":
        return "unknown"
    if conclusion == "success":
        return "succeeded"
    if conclusion in FAILED_CONCLUSIONS:
        return "failed"
    if conclusion in ("cancelled", "skipped", "neutral", "stale"):
        return "cancelled"
    return "unknown"


async def on_workflow_run(db: AsyncSession, settings: Settings, row: Mapping[str, Any]) -> None:
    """Feed events for the stored (post-upsert) state of a run. Called only when the upsert
    applied, i.e. the data was not older than what was stored: a late `in_progress` after
    `completed` is rejected by the upsert and never gets here."""
    run_id, attempt = row["id"], row.get("run_attempt") or 1
    state = build_state(row.get("status"), row.get("conclusion"))
    name = row.get("name") or "workflow"
    branch = row.get("branch")
    common: dict[str, Any] = {
        "repo": row.get("repo"),
        "branch": branch,
        "sha": row.get("head_sha"),
        "url": row.get("html_url"),
        "ref": {"workflow_run_id": run_id, "run_attempt": attempt},
    }
    if state == "running":
        await emit(
            db, settings, type="build.started", severity="info",
            dedup_key=f"build.started:{run_id}:{attempt}",
            title=f"{name} started on {branch or '?'}",
            occurred_at=row.get("updated_at"), **common,
        )  # fmt: skip
    elif state in ("succeeded", "failed", "cancelled"):
        failed = state == "failed"
        await emit(
            db, settings, type="build.failed" if failed else "build.completed",
            severity="error" if failed else ("success" if state == "succeeded" else "warning"),
            # One "done" key per attempt: a run can never be both completed and failed.
            dedup_key=f"build.done:{run_id}:{attempt}",
            title=f"{name} {'failed' if failed else state} on {branch or '?'}"
            + (f" ({row.get('conclusion')})" if failed else ""),
            occurred_at=row.get("updated_at"), **common,
        )  # fmt: skip


# --- GitHub deployments ---------------------------------------------------------------------

GITHUB_DEPLOY_STATES = {
    "queued": "running",
    "pending": "running",
    "in_progress": "running",
    "success": "succeeded",
    "failure": "failed",
    "error": "failed",
}


async def on_github_deployment(
    db: AsyncSession,
    settings: Settings,
    full: str,
    dep: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    *,
    source: str = "webhook",
    delivery_id: str | None = None,
) -> None:
    """A GitHub deployment (and optionally one of its statuses). Status updates apply only
    when newer than the last one applied (out-of-order deliveries)."""
    dep_id = dep.get("id")
    if not isinstance(dep_id, int):
        return
    ext = f"github:{full}:{dep_id}"
    env = _s(dep.get("environment"), 100) or "unknown"
    created = _dt(dep.get("created_at")) or _now()
    sha = _s(dep.get("sha"), 64)
    branch = _s(dep.get("ref"))
    await record_repo_event(db, {
        "dedup_key": f"github:deployment:{full}:{dep_id}", "provider": "github",
        "kind": "deployment", "repo": full, "branch": branch, "sha": sha,
        "environment": env, "actor": _login(dep.get("creator")),
        "url": _s(dep.get("url"), 2000), "state": "created", "occurred_at": created,
        "source": source, "delivery_id": delivery_id,
        "title": one_line(dep.get("description"), 500), "details": {"deployment_id": dep_id},
    })  # fmt: skip
    row = await db.scalar(select(Deployment).where(Deployment.external_id == ext).with_for_update())
    if row is None:
        row = Deployment(
            source="github", external_id=ext, repo=full, environment=env, sha=sha,
            branch=branch, status="running", started_at=created, details={},
        )  # fmt: skip
        db.add(row)
        await db.flush()
        await emit(
            db, settings, type="deployment.started", severity="info",
            dedup_key=f"deployment.started:{ext}", occurred_at=created, repo=full,
            branch=branch, sha=sha, environment=env,
            title=f"GitHub deployment to {env} started ({(sha or '?')[:7]})",
            ref={"deployment_id": row.id, "source": "github"},
        )  # fmt: skip
    if not status:
        return
    st_id = status.get("id")
    st_at = _dt(status.get("created_at")) or _now()
    state = _s(status.get("state"), 32) or "unknown"
    if isinstance(st_id, int):
        await record_repo_event(db, {
            "dedup_key": f"github:deployment_status:{full}:{st_id}", "provider": "github",
            "kind": "deployment_status", "repo": full, "branch": branch, "sha": sha,
            "environment": env, "actor": _login(status.get("creator")),
            "url": _s(status.get("target_url") or status.get("log_url"), 2000),
            "state": state, "occurred_at": st_at, "source": source,
            "delivery_id": delivery_id, "title": one_line(status.get("description"), 500),
            "details": {"deployment_id": dep_id},
        })  # fmt: skip
    last = _dt((row.details or {}).get("last_status_at"))
    if last is not None and st_at < last:
        return  # an older status delivered late: never moves the deployment back
    details = dict(row.details or {})
    details |= {"last_status_at": st_at.isoformat(), "github_state": state}
    row.details = details
    row.updated_at = _now()
    if state == "inactive":  # superseded by a newer deployment; its outcome stays
        return
    mapped = GITHUB_DEPLOY_STATES.get(state, "unknown")
    row.status = mapped
    row.url = _s(status.get("target_url") or status.get("log_url"), 2000) or row.url
    if mapped in ("succeeded", "failed"):
        row.finished_at = st_at
        if mapped == "failed":
            row.failure_reason = one_line(status.get("description")) or state
        await emit(
            db, settings,
            type="deployment.completed" if mapped == "succeeded" else "deployment.failed",
            severity="success" if mapped == "succeeded" else "error",
            dedup_key=f"deployment.done:{ext}", occurred_at=st_at, repo=full, branch=branch,
            sha=sha, environment=env, url=row.url,
            title=f"GitHub deployment to {env} {'succeeded' if mapped == 'succeeded' else 'failed'}"
            f" ({(sha or '?')[:7]}; reported by GitHub)",
            ref={"deployment_id": row.id, "source": "github"},
        )  # fmt: skip


# --- server deploys (deploy_record) ---------------------------------------------------------

PHASE_KEYS = (
    "build_started",
    "build_finished",
    "migrate_finished",
    "deploy_started",
    "deploy_finished",
    "health_ok",
    "rollback_started",
    "rollback_finished",
)


def parse_phases(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The phases deploy.sh recorded: known keys only, ISO timestamps, a one-line failure."""
    out: dict[str, Any] = {}
    for k in PHASE_KEYS:
        if (d := _dt(raw.get(k))) is not None:
            out[k] = d
    if raw.get("failure"):
        out["failure"] = one_line(raw.get("failure"))
    return out


async def server_deploy_started(
    db: AsyncSession,
    settings: Settings,
    *,
    agent_run_id: int,
    target: str,
    sha: str,
    previous_sha: str | None,
    log_path: str | None,
    environment: str | None = None,
) -> Deployment:
    repo, branch = deploy_target(settings, target)
    env = environment or settings.DEVOPS_ENVIRONMENT
    now = _now()
    row = Deployment(
        source="server", external_id=f"agent-run:{agent_run_id}", repo=repo, target=target,
        environment=env, branch=branch, sha=sha, previous_sha=previous_sha, status="running",
        started_at=now, agent_run_id=agent_run_id, details={"log": log_path},
    )  # fmt: skip
    db.add(row)
    await db.flush()
    await emit(
        db, settings, type="deployment.started", severity="info",
        dedup_key=f"deployment.started:{row.external_id}", occurred_at=now, repo=repo,
        branch=branch, sha=sha, environment=env,
        title=f"Deploying {target} {sha[:7]} to {env}",
        ref={"deployment_id": row.id, "source": "server", "target": target},
    )  # fmt: skip
    return row


def server_outcome(exit_code: int, phases: Mapping[str, Any]) -> tuple[str, bool, str | None]:
    """(status, rolled_back, rollback_status) from deploy.sh's exit code and phases.

    0 = promoted. It is `succeeded` only with a recorded health verification (`health_ok`);
        without one (e.g. a deploy script that records no phases) it is `unverified`.
    1 = failed and rolled back to a healthy previous release.
    2 = failed and the rollback is unhealthy too.
    other = failed (no rollback recorded)."""
    if exit_code == 0:
        return ("succeeded" if phases.get("health_ok") else "unverified"), False, None
    if exit_code == 1:
        return "rolled_back", True, "healthy"
    if exit_code == 2:
        return "failed", True, "unhealthy"
    return "failed", bool(phases.get("rollback_started")), None


async def server_deploy_finished(
    db: AsyncSession,
    settings: Settings,
    *,
    agent_run_id: int,
    exit_code: int,
    phases: Mapping[str, Any],
    error: str | None,
) -> Deployment | None:
    row = await db.scalar(
        select(Deployment)
        .where(Deployment.external_id == f"agent-run:{agent_run_id}")
        .with_for_update()
    )
    if row is None:
        return None
    now = _now()
    status, rolled_back, rollback_status = server_outcome(exit_code, phases)
    row.status, row.rolled_back, row.rollback_status = status, rolled_back, rollback_status
    row.finished_at = now
    row.build_started_at = phases.get("build_started")
    row.build_finished_at = phases.get("build_finished")
    row.deploy_started_at = phases.get("deploy_started")
    row.deploy_finished_at = phases.get("deploy_finished")
    row.health_verified_at = phases.get("health_ok") if exit_code == 0 else None
    if status not in ("succeeded", "unverified"):
        row.failure_reason = phases.get("failure") or one_line(error) or f"exit {exit_code}"
    row.details = {**(row.details or {}), "exit_code": exit_code} | (
        {"migrate_finished": phases["migrate_finished"].isoformat()}
        if phases.get("migrate_finished")
        else {}
    )
    row.updated_at = now
    label = f"{row.target or 'deploy'} {(row.sha or '?')[:7]} to {row.environment}"
    common: dict[str, Any] = {
        "repo": row.repo, "branch": row.branch, "sha": row.sha, "environment": row.environment,
        "occurred_at": now, "dedup_key": f"deployment.done:{row.external_id}",
        "ref": {"deployment_id": row.id, "source": "server", "target": row.target},
    }  # fmt: skip
    if status == "succeeded":
        await emit(db, settings, type="deployment.completed", severity="success",
                   title=f"Deployed {label}; health verified", **common)  # fmt: skip
    elif status == "unverified":
        await emit(db, settings, type="deployment.completed", severity="warning",
                   title=f"Deployed {label}; health NOT verified", **common)  # fmt: skip
    elif status == "rolled_back":
        await emit(db, settings, type="deployment.rolled_back", severity="error",
                   title=f"Deploy of {label} failed and was rolled back to "
                   f"{(row.previous_sha or '?')[:7]}: {row.failure_reason}", **common)  # fmt: skip
    else:
        how = " (rollback unhealthy: needs a human)" if rollback_status == "unhealthy" else ""
        await emit(db, settings, type="deployment.failed", severity="error",
                   title=f"Deploy of {label} failed{how}: {row.failure_reason}",
                   **common)  # fmt: skip
    return row


# --- runtime (host collector) ---------------------------------------------------------------

RUNTIME_SCHEMA = "aureon.runtime/v1"
MAX_SERVICES = 100


class ServiceObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    container_id: str | None = Field(default=None, max_length=80)
    container_name: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=32)
    health: str | None = Field(default=None, max_length=16)
    exit_code: int | None = None
    image: str | None = Field(default=None, max_length=500)
    image_id: str | None = Field(default=None, max_length=100)
    revision: str | None = Field(default=None, max_length=64)
    started_at: datetime | None = None

    @field_validator("state", "health", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("revision", mode="before")
    @classmethod
    def _rev(cls, v: Any) -> Any:
        # A label that was never set reads "" or "<no value>".
        return v if isinstance(v, str) and len(v) >= 7 and v.isalnum() else None


class Readiness(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service: str = Field(default="api", max_length=100)
    http_status: int | None = None
    ok: bool = False
    failing: list[str] = Field(default_factory=list, max_length=20)


class RuntimeReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_: Literal["aureon.runtime/v1"] = Field(alias="schema")
    host: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    observed_at: datetime
    services: list[ServiceObservation] = Field(max_length=MAX_SERVICES)
    # Services that must be present; one missing from `services` is recorded as stopped.
    expected: list[str] = Field(default_factory=list, max_length=MAX_SERVICES)
    readiness: Readiness | None = None


def service_status(
    obs: ServiceObservation, oneshot: frozenset[str], readiness: Readiness | None
) -> tuple[str, str | None]:
    """(status, failure reason) from what the collector saw. Never guessed: an unrecognised
    docker state is `unknown`."""
    state, health = obs.state or "", obs.health or ""
    if state == "missing":
        return "stopped", "container not found"
    if obs.service in oneshot:
        if state == "exited":
            return (("healthy", None) if obs.exit_code == 0
                    else ("unhealthy", f"one-shot exited with code {obs.exit_code}"))  # fmt: skip
        if state in ("running", "created", "restarting"):
            return "starting", None
    if readiness is not None and obs.service == readiness.service and state == "running":
        if not readiness.ok:
            failing = ", ".join(readiness.failing) or "not ready"
            return "unhealthy", f"readiness failed (HTTP {readiness.http_status}): {failing}"
    if state == "running":
        if health == "unhealthy":
            return "unhealthy", "container healthcheck failing"
        if health == "starting":
            return "starting", None
        return "healthy", None
    if state == "restarting":
        return "unhealthy", "container is restarting"
    if state in ("exited", "dead"):
        return "stopped", f"container {state}" + (
            f" (exit code {obs.exit_code})" if obs.exit_code is not None else ""
        )
    if state in ("paused", "created"):
        return "stopped", f"container {state}"
    return "unknown", None


async def record_runtime(
    db: AsyncSession, settings: Settings, report: RuntimeReport
) -> dict[str, Any]:
    """Upsert each observed service, append a snapshot, and emit service.unhealthy /
    service.recovered on a status change (never on a repeat of the same status)."""
    oneshot = frozenset(settings.DEVOPS_ONESHOT_SERVICES)
    observed = _dt(report.observed_at) or _now()
    seen = {o.service: o for o in report.services}
    for name in report.expected:
        if name not in seen:
            seen[name] = ServiceObservation(service=name, state="missing")
    out = {"services": 0, "events": 0}
    for name, obs in sorted(seen.items()):
        status, reason = service_status(obs, oneshot, report.readiness)
        row = await db.scalar(
            select(Service)
            .where(Service.host == report.host, Service.service == name)
            .with_for_update()
        )
        previous = row.status if row is not None else None
        if row is None:
            row = Service(host=report.host, service=name, status=status, status_since=observed)
            db.add(row)
        elif observed < row.last_seen_at:
            continue  # an older report delivered late
        changed = previous != status
        row.container_id = obs.container_id
        row.container_name = obs.container_name
        row.state = obs.state
        row.health = obs.health
        row.exit_code = obs.exit_code
        row.image = obs.image
        row.image_id = obs.image_id
        row.revision = obs.revision
        row.started_at = _dt(obs.started_at)
        row.failure_reason = reason
        row.last_seen_at = observed
        if status == "healthy":
            row.last_healthy_at = observed
        if changed:
            row.status = status
            row.status_since = observed
        row.updated_at = _now()
        await db.flush()
        db.add(ServiceSnapshot(
            service_id=row.id, observed_at=observed, status=status, state=obs.state,
            health=obs.health, revision=obs.revision, failure_reason=reason,
        ))  # fmt: skip
        out["services"] += 1
        if not changed:
            continue
        key = f"{report.host}:{name}:{observed.isoformat()}"
        if status in UNHEALTHY:
            out["events"] += bool(await emit(
                db, settings, type="service.unhealthy", severity="error",
                dedup_key=f"service.unhealthy:{key}", occurred_at=observed, service=name,
                title=f"{name} on {report.host} is {status}: {reason}",
                ref={"service_id": row.id, "host": report.host},
            ))  # fmt: skip
        elif status == "healthy" and previous in UNHEALTHY:
            out["events"] += bool(await emit(
                db, settings, type="service.recovered", severity="success",
                dedup_key=f"service.recovered:{key}", occurred_at=observed, service=name,
                title=f"{name} on {report.host} recovered",
                ref={"service_id": row.id, "host": report.host},
            ))  # fmt: skip
    return out


# --- backfill (after an outage) -------------------------------------------------------------


async def backfill(
    maker: async_sessionmaker[AsyncSession], client: GitHubClient, settings: Settings
) -> dict[str, Any]:
    """Re-read recent PRs and deployments of the allowlisted repos. Idempotent: every fact
    lands on its dedup key, so what the webhooks already delivered is not recorded twice.
    Stops early on a rate limit (the next run continues)."""
    async with maker() as db:
        login = await gh.known_login(db, client)
        q = gh.visible_repos_query(settings, login).where(GitHubRepository.archived.is_(False))
        repos = [
            r.full_name
            for r in (
                await db.scalars(
                    q.order_by(GitHubRepository.pushed_at.desc().nulls_last()).limit(
                        settings.DEVOPS_BACKFILL_MAX_REPOS
                    )
                )
            ).all()
        ]
    out: dict[str, Any] = {"repos": len(repos), "pr_events": 0, "deployments": 0,
                           "errors": [], "rate_limited": False}  # fmt: skip
    per = settings.DEVOPS_BACKFILL_PER_REPO
    for full in repos:
        try:
            prs = await client.request(
                "GET", f"/repos/{full}/pulls",
                params={"state": "all", "sort": "updated", "direction": "desc", "per_page": per},
            )  # fmt: skip
            deps = await client.request(
                "GET", f"/repos/{full}/deployments", params={"per_page": min(per, 10)}
            )
            statuses: dict[int, dict[str, Any]] = {}
            for d in deps.data if isinstance(deps.data, list) else []:
                if isinstance(d, dict) and isinstance(d.get("id"), int):
                    st = await client.request(
                        "GET", f"/repos/{full}/deployments/{d['id']}/statuses",
                        params={"per_page": 1},
                    )  # fmt: skip
                    if isinstance(st.data, list) and st.data and isinstance(st.data[0], dict):
                        statuses[d["id"]] = st.data[0]
        except GitHubRateLimited:
            out["rate_limited"] = True
            log.warning("devops_backfill_rate_limited", repo=full)
            break
        except GitHubError as exc:
            out["errors"].append(f"{full}: {exc.code}")
            continue
        async with maker() as db, db.begin():
            for pr in prs.data if isinstance(prs.data, list) else []:
                if isinstance(pr, dict):
                    out["pr_events"] += await on_pull_request(
                        db, settings, full, pr, None, source="backfill"
                    )
            for d in deps.data if isinstance(deps.data, list) else []:
                if isinstance(d, dict) and isinstance(d.get("id"), int):
                    await on_github_deployment(
                        db, settings, full, d, statuses.get(d["id"]), source="backfill"
                    )
                    out["deployments"] += 1
    log.info("devops_backfill", **{k: v for k, v in out.items() if k != "errors"},
             errors=len(out["errors"]))  # fmt: skip
    return out


# --- retention ------------------------------------------------------------------------------


async def purge(db: AsyncSession, settings: Settings) -> dict[str, int]:
    now = _now()
    events = now - timedelta(days=settings.DEVOPS_RETENTION_DAYS)
    snaps = now - timedelta(days=settings.DEVOPS_SNAPSHOT_RETENTION_DAYS)
    deploys = now - timedelta(days=settings.DEVOPS_DEPLOYMENT_RETENTION_DAYS)
    r1 = await db.execute(delete(DevopsEvent).where(DevopsEvent.occurred_at < events))
    r2 = await db.execute(delete(RepoEvent).where(RepoEvent.occurred_at < events))
    r3 = await db.execute(delete(ServiceSnapshot).where(ServiceSnapshot.observed_at < snaps))
    r4 = await db.execute(
        delete(Deployment).where(Deployment.started_at < deploys, Deployment.status != "running")
    )
    return {
        "events": int(getattr(r1, "rowcount", 0) or 0),
        "repo_events": int(getattr(r2, "rowcount", 0) or 0),
        "snapshots": int(getattr(r3, "rowcount", 0) or 0),
        "deployments": int(getattr(r4, "rowcount", 0) or 0),
    }
