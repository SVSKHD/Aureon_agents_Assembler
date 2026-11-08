"""Live deploy progress (docs/33-deploy-progress.md).

The deploy scripts (scripts/deploy.sh, ASTRA-FRONTEND/scripts/deploy.sh) append one line per
stage transition to their phases file as they actually happen:

    stage=<name>|<running|succeeded|failed|skipped>|<ISO time>|<one-line detail>

scripts/ops/deploy-poll.sh, which runs the deploy, sends what is in that file plus the deploy's
own log lines to `python -m app.workers.deploy_progress` inside the running API container every
few seconds (a backend deploy restarts the API: the reports it misses are re-sent, since every
report carries the whole file), and once more, through a one-shot container, when the deploy
ends. Everything here is idempotent: a stage only moves forward, a finished stage never
changes, and nothing is ever inferred: a stage the script has not reported is "pending", and a
deploy is only successful once deploy_record says so (after the health verification).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger, scrub
from app.models.devops import Deployment, DeploymentStage, RepoEvent

log = get_logger("services.deploy_progress")

# The stages each target's deploy script reports, in order. `rolling_back` only appears when a
# release had to be put back.
PLANS: dict[str, tuple[str, ...]] = {
    "backend": (
        "queued", "pulling", "validating", "building", "migrating", "restarting",
        "health_checking", "verifying",
    ),
    "frontend": (
        "queued", "pulling", "validating", "building", "restarting", "health_checking",
        "verifying",
    ),
}  # fmt: skip
DEFAULT_PLAN = PLANS["backend"]
EXTRA = ("rolling_back",)
LABELS = {
    "queued": "Queued",
    "pulling": "Pulling",
    "validating": "Validating",
    "building": "Building",
    "migrating": "Migrating",
    "restarting": "Restarting",
    "health_checking": "Health checking",
    "verifying": "Verifying",
    "rolling_back": "Rolling back",
}
FINISHED = frozenset({"succeeded", "failed", "skipped"})
MAX_LOG_BYTES = 128 * 1024
MAX_LOG_LINES = 600
REPORT_SCHEMA = "aureon.deploy.progress/v1"


def one_line(v: Any, limit: int = 300) -> str | None:
    if v is None:
        return None
    text = " ".join(str(v).split())
    return text[:limit] or None


def plan_for(target: str | None) -> tuple[str, ...]:
    return PLANS.get(target or "", DEFAULT_PLAN)


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(a: datetime | None, b: datetime | None) -> int | None:
    if a is None or b is None:
        return None
    return max(0, int((b - a).total_seconds() * 1000))


# --- the report the host sends ----------------------------------------------------------------


class StageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Literal[
        "queued", "pulling", "validating", "building", "migrating", "restarting",
        "health_checking", "verifying", "rolling_back",
    ]  # fmt: skip
    status: Literal["running", "succeeded", "failed", "skipped"]
    at: datetime
    detail: str | None = Field(default=None, max_length=2000)

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=UTC)


class ProgressReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_: Literal["aureon.deploy.progress/v1"] = Field(alias="schema")
    run_id: int = Field(ge=1)
    target: Literal["backend", "frontend"]
    stages: list[StageEvent] = Field(default_factory=list, max_length=200)
    # The commit the script checked out (`sha=` line) and the PR its merge subject names.
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    pr: int | None = Field(default=None, ge=1, le=10**9)
    log: str | None = Field(default=None, max_length=4 * MAX_LOG_BYTES)
    log_truncated: bool = False


def sanitize_log(text: str | None, truncated: bool = False) -> tuple[str | None, bool]:
    """Scrubbed (known secrets, credentials, one-time ids), at most MAX_LOG_LINES lines and
    MAX_LOG_BYTES, keeping the end (where a failure usually is)."""
    if text is None:
        return None, truncated
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) > MAX_LOG_LINES:
        lines, truncated = lines[-MAX_LOG_LINES:], True
    out = scrub("\n".join(lines))
    raw = out.encode()
    if len(raw) > MAX_LOG_BYTES:
        out = raw[-MAX_LOG_BYTES:].decode(errors="ignore").split("\n", 1)[-1]
        truncated = True
    return out, truncated


# --- writes -----------------------------------------------------------------------------------


async def _stages(db: AsyncSession, deployment_id: int) -> dict[str, DeploymentStage]:
    rows = (
        await db.scalars(
            select(DeploymentStage)
            .where(DeploymentStage.deployment_id == deployment_id)
            .with_for_update()
        )
    ).all()
    return {r.name: r for r in rows}


def _seq(plan: tuple[str, ...], name: str) -> int:
    order = (*plan, *EXTRA)
    return order.index(name) if name in order else len(order)


async def start_queued(db: AsyncSession, deployment: Deployment, at: datetime) -> None:
    """The poller opened the deploy: it waits for the deploy lock until `pulling` starts."""
    plan = plan_for(deployment.target)
    db.add(DeploymentStage(
        deployment_id=deployment.id, name="queued", seq=_seq(plan, "queued"), status="running",
        started_at=at, updated_at=_now(),
    ))  # fmt: skip
    await db.flush()


async def apply_events(
    db: AsyncSession, deployment: Deployment, events: Iterable[StageEvent]
) -> int:
    """Move stages forward; returns how many rows changed. A finished stage never changes."""
    plan = plan_for(deployment.target)
    rows = await _stages(db, deployment.id)
    changed = 0
    for ev in sorted(events, key=lambda e: (e.at, e.status not in FINISHED)):
        # Anything after `queued` means the deploy got its lock and started.
        q = rows.get("queued")
        if ev.name != "queued" and q is not None and q.status == "running":
            q.status, q.finished_at = "succeeded", ev.at
            q.duration_ms = _ms(q.started_at, ev.at)
            q.updated_at = _now()
            changed += 1
        row = rows.get(ev.name)
        if row is not None and row.status in FINISHED:
            continue
        if row is None:
            row = DeploymentStage(deployment_id=deployment.id, name=ev.name,
                                  seq=_seq(plan, ev.name), status="pending")  # fmt: skip
            db.add(row)
            rows[ev.name] = row
        if ev.status == "running":
            row.status = "running"
            row.started_at = row.started_at or ev.at
        else:
            row.status = ev.status
            row.started_at = row.started_at or ev.at
            row.finished_at = ev.at
            row.duration_ms = _ms(row.started_at, ev.at)
            if ev.status == "failed":
                row.error = one_line(scrub(ev.detail or ""), 500) or "failed"
        row.updated_at = _now()
        changed += 1
    await db.flush()
    return changed


async def close_open(db: AsyncSession, deployment: Deployment, at: datetime) -> None:
    """At the end of a deploy: a stage still `running` never reported an end (the script was
    killed, or the host lost it). It is recorded as failed, never as succeeded."""
    for row in (await _stages(db, deployment.id)).values():
        if row.status == "running":
            row.status, row.finished_at = "failed", at
            row.duration_ms = _ms(row.started_at, at)
            row.error = row.error or "the deploy ended without reporting the end of this stage"
            row.updated_at = _now()
    await db.flush()


async def pr_for(db: AsyncSession, repo: str | None, sha: str | None) -> int | None:
    """The PR a merge commit came from, from a recorded pr.merged event."""
    if not repo or not sha:
        return None
    n: int | None = await db.scalar(
        select(RepoEvent.number)
        .where(RepoEvent.kind == "pr.merged", RepoEvent.sha == sha, RepoEvent.repo == repo)
        .limit(1)
    )
    return n


async def apply_report(
    db: AsyncSession, settings: Settings, report: ProgressReport
) -> dict[str, Any]:
    row = await db.scalar(
        select(Deployment)
        .where(Deployment.external_id == f"agent-run:{report.run_id}")
        .with_for_update()
    )
    if row is None:
        return {"applied": False, "reason": "unknown deploy run"}
    if row.target and row.target != report.target:
        return {"applied": False, "reason": "target mismatch"}
    changed = await apply_events(db, row, report.stages)
    if report.sha:
        row.deployed_sha = report.sha
    if report.sha or report.pr:
        row.pr_number = await pr_for(db, row.repo, row.deployed_sha) or report.pr or row.pr_number
    if report.log is not None:
        row.log_tail, row.log_truncated = sanitize_log(report.log, report.log_truncated)
    row.progress_at = _now()
    row.updated_at = _now()
    await db.flush()
    return {"applied": True, "stages_changed": changed}


# --- read view --------------------------------------------------------------------------------


def stale(settings: Settings, row: Deployment, now: datetime | None = None) -> bool:
    """A running deploy whose host has not reported for DEVOPS_DEPLOY_STALE_SECONDS: the
    progress shown cannot be trusted to be current (the poller or the host may be gone)."""
    if row.status != "running":
        return False
    last = row.progress_at or row.started_at
    return last < (now or _now()) - timedelta(seconds=settings.DEVOPS_DEPLOY_STALE_SECONDS)


def stage_view(
    plan: tuple[str, ...], rows: Mapping[str, DeploymentStage], terminal: bool, now: datetime
) -> list[dict[str, Any]]:
    """Every planned stage (pending when not reported), plus rolling_back when it happened."""
    names = list(plan) + [n for n in EXTRA if n in rows]
    names += sorted((n for n in rows if n not in names), key=lambda n: rows[n].seq)
    out = []
    for n in names:
        r = rows.get(n)
        status = r.status if r is not None else ("skipped" if terminal else "pending")
        if r is not None and r.status == "pending" and terminal:
            status = "skipped"
        running_ms = _ms(r.started_at, now) if r is not None and r.status == "running" else None
        out.append({
            "name": n,
            "label": LABELS.get(n, n.replace("_", " ").capitalize()),
            "status": status,
            "started_at": r.started_at if r else None,
            "finished_at": r.finished_at if r else None,
            "duration_ms": (r.duration_ms if r and r.duration_ms is not None else running_ms),
            "error": r.error if r else None,
        })  # fmt: skip
    return out


async def view(
    db: AsyncSession, settings: Settings, row: Deployment, now: datetime | None = None
) -> dict[str, Any]:
    now = now or _now()
    rows = {
        r.name: r
        for r in (
            await db.scalars(select(DeploymentStage).where(DeploymentStage.deployment_id == row.id))
        ).all()
    }
    terminal = row.status != "running"
    stages = stage_view(plan_for(row.target), rows, terminal, now)
    planned = [s for s in stages if s["name"] not in EXTRA]
    done = sum(1 for s in planned if s["status"] == "succeeded")
    current = next((s["name"] for s in stages if s["status"] == "running"), None)
    verify = rows.get("verifying")
    commit_verified: bool | None = None
    if verify is not None and verify.status in FINISHED:
        commit_verified = (
            verify.status == "succeeded"
            and row.deployed_sha is not None
            and row.sha is not None
            and row.deployed_sha == row.sha
        )
    return {
        "stages": stages,
        "completed": done,
        "total": len(planned),
        "current_stage": current,
        "stale": stale(settings, row, now),
        "commit_verified": commit_verified,
    }
