"""GET /api/v1/system/integrations: the Spasta health card (docs/24-production-integration.md).

One summary of every external piece Aureon depends on, for an admin:

| section        | source (nothing here writes, and only Ollama/Claude are probed live)      |
|----------------|----------------------------------------------------------------------------|
| mt5            | agents.agents `mt5-bot` heartbeat (= the bot export's generated_at)        |
| dhan           | settings + `dhan-feed` agent + the `dhan-reconcile` job's last status      |
| github         | settings + the `github` agent (written by the github_sync task)            |
| webhook_worker | webhook secret + the `github-webhooks` agent + pending/failed deliveries   |
| claude_cli     | the cached availability check (`claude --version`, only when enabled)       |
| ollama         | GET /api/tags (cached) and whether every configured model is installed     |
| discord        | whether DISCORD_WEBHOOK_URL is set (never its value)                       |
| database       | the connected role: a superuser means the scoped roles are not in use     |
| backups        | the backup status file written by scripts/ops/backup.sh                    |

Each section has a `status`: ok | degraded | down | not_configured | disabled. Errors carry
only an exception type, never a message (messages can contain hosts or DSNs).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient
from app.agents.registry import effective_state
from app.core.config import Settings
from app.integrations.claude_cli import ClaudeCLI
from app.models.agents import Agent, ScheduledJob
from app.models.github import GitHubWebhookEvent
from app.services import dhan_auth

Section = dict[str, Any]
SECTION_LIMIT_S = 4.0
DOWN_STATES = ("stale", "error", "stopped")


async def _guard(fn: Callable[[], Awaitable[Section]]) -> Section:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(SECTION_LIMIT_S):
            out = await fn()
    except TimeoutError:
        out = {"status": "down", "error": f"timeout after {SECTION_LIMIT_S:g}s"}
    except Exception as exc:
        out = {"status": "down", "error": type(exc).__name__}
    out["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return out


def _agent_view(agent: Agent | None, stale_after: float, now: datetime) -> Section:
    if agent is None:
        return {"registered": False, "state": None, "last_heartbeat_at": None, "age_s": None}
    age = (now - agent.last_heartbeat_at).total_seconds() if agent.last_heartbeat_at else None
    return {
        "registered": True,
        "state": effective_state(agent, stale_after, now),
        "last_heartbeat_at": agent.last_heartbeat_at.isoformat()
        if agent.last_heartbeat_at
        else None,
        "age_s": round(age, 1) if age is not None else None,
    }


def read_backup_status(path: Path, max_age_hours: float, now: datetime) -> Section:
    """The status file of scripts/ops/backup.sh. Unreadable = `unknown` (not an alarm)."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {"status": "unknown", "readable": False, "reason": "missing"}
    except (OSError, ValueError) as exc:
        return {"status": "unknown", "readable": False, "reason": type(exc).__name__}
    if not isinstance(doc, dict):
        return {"status": "unknown", "readable": False, "reason": "invalid"}
    out: Section = {
        "readable": True,
        "last_status": doc.get("status"),
        "finished_at": doc.get("finished_at"),
        "last_success_at": doc.get("last_success_at"),
        "last_success_backup": doc.get("last_success_backup"),
        "max_age_hours": max_age_hours,
    }
    age_h: float | None = None
    try:
        last_ok = datetime.fromisoformat(str(doc.get("last_success_at")))
        if last_ok.tzinfo is None:
            last_ok = last_ok.replace(tzinfo=UTC)
        age_h = (now - last_ok).total_seconds() / 3600
    except ValueError:
        pass
    out["age_hours"] = round(age_h, 2) if age_h is not None else None
    problems = []
    if doc.get("status") != "ok":
        problems.append("last backup failed")
    if age_h is None:
        problems.append("no successful backup recorded")
    elif age_h > max_age_hours:
        problems.append(f"last successful backup is {age_h:.1f} h old")
    out["problems"] = problems
    out["status"] = "down" if problems else "ok"
    return out


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in d.items()}


class IntegrationsSummary:
    def __init__(
        self,
        settings: Settings,
        maker: async_sessionmaker[AsyncSession],
        ollama: OllamaClient | None,
        claude: ClaudeCLI | None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.settings = settings
        self.maker = maker
        self.ollama = ollama
        self.claude = claude
        self.now = now or datetime.now(UTC)

    async def _agents(self, *ids: str) -> dict[str, Agent]:
        async with self.maker() as db:
            rows = (await db.scalars(select(Agent).where(Agent.id.in_(ids)))).all()
        return {a.id: a for a in rows}

    async def mt5(self) -> Section:
        s = self.settings
        if not s.mt5_configured:
            return {"status": "not_configured", "configured": False}
        agent = (await self._agents("mt5-bot")).get("mt5-bot")
        view = _agent_view(agent, s.MT5_STALE_SECONDS, self.now)
        fresh = view["registered"] and view["state"] not in DOWN_STATES
        return {
            "configured": True,
            "source": "url" if s.MT5_STATUS_URL else "file",
            "read_only": True,
            "fresh": bool(fresh),
            "stale_after_s": s.MT5_STALE_SECONDS,
            "agent": view,
            "status": "ok" if fresh else "down",
        }

    async def dhan(self) -> Section:
        s = self.settings
        session = dhan_auth.status(s, self.now)
        if not s.DHAN_CLIENT_ID:
            return {"status": "not_configured", "configured": False}
        if session["data_stale"]:
            # Client id set but no usable token (login expired / never done): every
            # Dhan-derived number is stale until someone logs in again.
            return {
                "configured": True,
                "read_only": True,
                "session": _jsonable(session),
                "problems": ["no valid Dhan session: log in to Dhan on /settings"],
                "status": "down",
            }
        feed = (await self._agents("dhan-feed")).get("dhan-feed")
        async with self.maker() as db:
            job = await db.get(ScheduledJob, "dhan-reconcile")
        view = _agent_view(feed, s.AGENT_STALE_SECONDS, self.now)
        problems = []
        if job is not None and job.last_status == "failed":
            problems.append("dhan-reconcile failed")
        if view["registered"] and view["state"] in DOWN_STATES:
            problems.append(f"dhan-feed {view['state']}")
        if session["expires_soon"]:
            problems.append("Dhan session expires soon")
        return {
            "configured": True,
            "read_only": True,
            "session": _jsonable(session),
            "feed": view,
            "reconcile": {
                "last_status": job.last_status if job else None,
                "last_run_at": job.last_run_at.isoformat() if job and job.last_run_at else None,
            },
            "problems": problems,
            "status": "degraded" if problems else "ok",
        }

    async def github(self) -> Section:
        s = self.settings
        if not s.github_configured:
            return {"status": "not_configured", "configured": False}
        agent = (await self._agents("github")).get("github")
        view = _agent_view(agent, s.GITHUB_AGENT_STALE_SECONDS, self.now)
        details = (agent.details or {}) if agent else {}
        ok = bool(details.get("ok")) and view["state"] not in DOWN_STATES
        return {
            "configured": True,
            "login": details.get("login"),
            "connection_ok": details.get("ok"),
            "rate_limit": details.get("rate_limit"),
            "agent": view,
            "status": "ok" if ok else ("degraded" if agent else "down"),
        }

    async def webhook_worker(self) -> Section:
        s = self.settings
        if not s.github_webhook_configured:
            return {"status": "not_configured", "configured": False}
        agent = (await self._agents("github-webhooks")).get("github-webhooks")
        async with self.maker() as db:
            counts = dict(
                (
                    await db.execute(
                        select(GitHubWebhookEvent.status, func.count())
                        .where(GitHubWebhookEvent.status.in_(("pending", "failed")))
                        .group_by(GitHubWebhookEvent.status)
                    )
                )
                .tuples()
                .all()
            )
        view = _agent_view(agent, s.AGENT_STALE_SECONDS, self.now)
        alive = view["registered"] and view["state"] not in DOWN_STATES
        return {
            "configured": True,
            "worker": view,
            "pending": int(counts.get("pending", 0)),
            "failed": int(counts.get("failed", 0)),
            "status": "ok" if alive else "down",
        }

    async def claude_cli(self) -> Section:
        if not self.settings.CLAUDE_REVIEW_ENABLED or self.claude is None:
            return {"status": "disabled", "enabled": False, "advisory_only": True}
        a = await self.claude.availability()
        return {
            "enabled": True,
            "available": a.available,
            "reason": a.reason,
            "version": a.version,
            "advisory_only": True,
            "status": "ok" if a.available else "down",
        }

    async def ollama_models(self) -> Section:
        s = self.settings
        wanted = {
            "CODE_MODEL": s.CODE_MODEL,
            "REPAIR_MODEL": s.REPAIR_MODEL,
            "RESEARCH_AI_MODEL": s.RESEARCH_AI_MODEL,
            "VOICE_OLLAMA_MODEL": s.VOICE_OLLAMA_MODEL,
        }
        if self.ollama is None:
            return {"status": "down", "error": "no client"}
        models = await self.ollama.list_models()
        names = sorted(m.name for m in models)
        have = set(names) | {n.removesuffix(":latest") for n in names}
        missing = {k: v for k, v in wanted.items() if v and v not in have}
        return {
            "reachable": True,
            "models": names,
            "configured_models": {k: v for k, v in wanted.items() if v},
            "missing": missing,
            "max_concurrent": s.OLLAMA_MAX_CONCURRENT,
            "status": "degraded" if missing or not names else "ok",
        }

    async def discord(self) -> Section:
        on = bool(self.settings.discord_webhook_url)
        return {
            "configured": on,
            "alerts_cron": self.settings.ALERTS_CRON or None,
            "status": "ok" if on else "not_configured",
        }

    async def database(self) -> Section:
        async with self.maker() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT current_user, r.rolsuper, "
                        "has_database_privilege(current_database(), 'CREATE') "
                        "FROM pg_roles r WHERE r.rolname = current_user"
                    )
                )
            ).one()
        role, superuser, can_create = str(row[0]), bool(row[1]), bool(row[2])
        scoped = not superuser and not can_create
        return {
            "role": role,
            "superuser": superuser,
            "can_create_schemas": can_create,
            "scoped": scoped,
            "status": "ok" if scoped else "degraded",
            "hint": None if scoped else "apply deploy/sql/roles.sql and use the aureon_app role",
        }

    async def backups(self) -> Section:
        s = self.settings
        return await asyncio.to_thread(
            read_backup_status, s.BACKUP_STATUS_FILE, s.BACKUP_MAX_AGE_HOURS, self.now
        )

    async def collect(self) -> Section:
        names: list[tuple[str, Callable[[], Awaitable[Section]]]] = [
            ("mt5", self.mt5),
            ("dhan", self.dhan),
            ("github", self.github),
            ("webhook_worker", self.webhook_worker),
            ("claude_cli", self.claude_cli),
            ("ollama", self.ollama_models),
            ("discord", self.discord),
            ("database", self.database),
            ("backups", self.backups),
        ]
        results = await asyncio.gather(*(_guard(fn) for _, fn in names))
        sections = dict(zip((n for n, _ in names), results, strict=True))
        problems = sorted(n for n, sec in sections.items() if sec["status"] in ("down", "degraded"))
        return {
            "generated_at": self.now.isoformat(),
            "ok": not problems,
            "problems": problems,
            "integrations": sections,
        }
