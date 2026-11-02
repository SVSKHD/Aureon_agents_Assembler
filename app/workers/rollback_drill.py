"""Migration rollback drill (docs/24-production-integration.md, Phase 22).

    python -m app.workers.migrate --rollback-drill aureon_rbk_<ts> --steps 3

On a SCRATCH database only (the caller refuses the configured one):

1. `alembic upgrade head`, then a schema snapshot (columns, indexes, constraints of every
   app schema and the public tables this app touches);
2. `alembic downgrade -1`, `steps` times (each revision's downgrade must work);
3. `alembic upgrade +1` back to head, one revision at a time (each upgrade must work from the
   state its own downgrade left);
4. the snapshot again: it must be identical to step 1 (the round trip loses nothing);
5. an app smoke test: every mapped table answers a `SELECT <mapped columns> ... LIMIT 0`, so
   the models match the schema, and the version table says head.

Everything runs in this one process (the alembic command API), so a full cycle over every
revision takes seconds. Error text is redacted (no DSN ever reaches the report).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.integrations.github.redact import redact

ROOT = Path(__file__).resolve().parents[2]
VERSION_SQL = "SELECT version_num FROM public.aureon_alembic_version"
APP_SCHEMAS = ("core", "ai", "agents", "trading", "dhan", "mt5", "github", "audit", "spasta")
PUBLIC_TABLES = (
    "trades",
    "expenses",
    "secured",
    "astra_documents",
    "portfolio_profiles",
    "aureon_alembic_version",
)

_SNAPSHOT_SQL = {
    "columns": """
        SELECT table_schema || '.' || table_name || '.' || column_name, data_type, udt_name,
               is_nullable, coalesce(column_default, ''),
               coalesce(character_maximum_length::text, ''),
               coalesce(numeric_precision::text, ''), coalesce(numeric_scale::text, ''),
               is_identity
        FROM information_schema.columns
        WHERE table_schema = ANY(:schemas)
           OR (table_schema = 'public' AND table_name = ANY(:tables))
    """,
    "indexes": """
        SELECT schemaname || '.' || indexname, indexdef FROM pg_indexes
        WHERE schemaname = ANY(:schemas)
           OR (schemaname = 'public' AND tablename = ANY(:tables))
    """,
    "constraints": """
        SELECT n.nspname || '.' || c.relname || '.' || k.conname, k.contype::text,
               pg_get_constraintdef(k.oid)
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(:schemas)
           OR (n.nspname = 'public' AND c.relname = ANY(:tables))
    """,
}


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _err(exc: BaseException, url: str) -> str:
    pw = make_url(url).password
    return redact(f"{type(exc).__name__}: {exc}", [pw if isinstance(pw, str) else None])[:500]


async def snapshot(url: str) -> dict[str, list[tuple[str, ...]]]:
    engine = create_async_engine(url)
    params = {"schemas": list(APP_SCHEMAS), "tables": list(PUBLIC_TABLES)}
    try:
        async with engine.connect() as conn:
            out: dict[str, list[tuple[str, ...]]] = {}
            for name, sql in _SNAPSHOT_SQL.items():
                rows = (await conn.execute(text(sql), params)).all()
                out[name] = sorted(tuple(str(v) for v in r) for r in rows)
            return out
    finally:
        await engine.dispose()


def diff(a: dict[str, list[tuple[str, ...]]], b: dict[str, list[tuple[str, ...]]]) -> list[str]:
    out: list[str] = []
    for kind in sorted(set(a) | set(b)):
        sa, sb = set(a.get(kind, [])), set(b.get(kind, []))
        out += [f"{kind} lost: {r[0]}" for r in sorted(sa - sb)]
        out += [f"{kind} new/changed: {r[0]}" for r in sorted(sb - sa)]
    return out


async def smoke(url: str) -> dict[str, Any]:
    """Every mapped table answers a select of its mapped columns; the version is head."""
    import app.models  # noqa: F401  (registers every table)
    from app.db.base import Base
    from app.models import finance

    engine = create_async_engine(url)
    errors: list[str] = []
    tables = [*Base.metadata.sorted_tables, *finance.LEGACY.sorted_tables]
    try:
        async with engine.connect() as conn:
            for t in tables:
                try:
                    await conn.execute(select(t).limit(0))
                except Exception as exc:
                    errors.append(f"{t.fullname}: {_err(exc, url)}")
                    await conn.rollback()
            version = await conn.scalar(text(VERSION_SQL))
    finally:
        await engine.dispose()
    return {"tables": len(tables), "errors": errors, "version": version}


def _current(cfg: Config, url: str) -> str | None:
    async def q() -> str | None:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                v = await conn.scalar(text(VERSION_SQL))
                return str(v) if v is not None else None
        finally:
            await engine.dispose()

    return asyncio.run(q())


def run_drill(url: str, steps: int) -> dict[str, Any]:
    cfg = alembic_config(url)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    chain = [r.revision for r in script.walk_revisions()]  # head first
    steps = max(0, min(steps, len(chain) - 1))
    report: dict[str, Any] = {
        "database": make_url(url).database,
        "head": head,
        "steps_requested": steps,
        "downgrades": [],
        "upgrades": [],
        "passed": False,
    }
    try:
        command.upgrade(cfg, "head")
        before = asyncio.run(snapshot(url))
        for _ in range(steps):
            rev = _current(cfg, url)
            command.downgrade(cfg, "-1")
            report["downgrades"].append({"from": rev, "to": _current(cfg, url), "ok": True})
        while (rev := _current(cfg, url)) != head:
            command.upgrade(cfg, "+1")
            report["upgrades"].append({"from": rev, "to": _current(cfg, url), "ok": True})
        after = asyncio.run(snapshot(url))
    except Exception as exc:
        report["error"] = _err(exc, url)
        report["current"] = _current(cfg, url)
        return report
    changes = diff(before, after)
    report["schema_identical"] = not changes
    report["schema_diff"] = changes[:50]
    report["objects"] = {k: len(v) for k, v in before.items()}
    report["smoke"] = asyncio.run(smoke(url))
    report["passed"] = (
        not changes
        and not report["smoke"]["errors"]
        and report["smoke"]["version"] == head
        and len(report["downgrades"]) == steps
    )
    return report
