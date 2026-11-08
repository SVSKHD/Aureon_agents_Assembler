"""One-shot `migrate` service: wait for Postgres, then `alembic upgrade head`.

Compose runs it on every `up` (after Redis is healthy, before the API starts), so a reboot or
an upgrade brings the schema to head before anything serves traffic. Migrations are additive
and idempotent at head. The existing Postgres container lives in another compose project, so
compose cannot order on it directly; this waits for it instead.

    python -m app.workers.migrate            # wait up to MIGRATE_WAIT_SECONDS (default 180)

`docker compose run --rm migrate <cmd>` still runs any other command (e.g. the admin bootstrap).

    python -m app.workers.migrate --current-for spasta_drill_x   # `alembic current` on another
                                                                 # DB on the same server
                                                                 # (scripts/ops/restore-drill.sh)
    python -m app.workers.migrate --check    # read-only: is the database at head? prints a JSON
                                             # report after CHECK_MARKER; exit 0 at head,
                                             # 3 pending, 4 diverged, 1 unreachable
    python -m app.workers.migrate --rollback-drill aureon_rbk_x --steps 3
        # upgrade head -> downgrade -1 (x3) -> upgrade head on a SCRATCH db, then an app smoke
        # test; prints a JSON report (scripts/ops/rollback-drill.sh, docs/24)

Phase 22: the URL is MIGRATIONS_DATABASE_URL (the migrator role, owner of the app schemas)
when set, else DATABASE_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.logging import configure_logging, get_logger

log = get_logger("migrate")
REPORT_MARKER = "=== aureon rollback drill report ==="
CHECK_MARKER = "=== aureon migration check ==="
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
VERSION_TABLE = "public.aureon_alembic_version"  # alembic/env.py VERSION_TABLE(_SCHEMA)
# --check exit codes
CHECK_AT_HEAD, CHECK_UNREACHABLE, CHECK_PENDING, CHECK_DIVERGED = 0, 1, 3, 4
DB_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def url_for_database(url: str, database: str) -> str:
    """DATABASE_URL with only the database name swapped (credentials stay in-process)."""
    if not DB_NAME.match(database):
        raise ValueError("invalid database name")
    return make_url(url).set(database=database).render_as_string(hide_password=False)


async def wait_for_postgres(url: str, wait_seconds: float, interval: float = 3.0) -> bool:
    deadline = time.monotonic() + wait_seconds
    attempt = 0
    while True:
        attempt += 1
        engine = create_async_engine(url, pool_pre_ping=False)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            # Exception type only: messages can carry the DSN.
            log.info("postgres_not_ready", attempt=attempt, error=type(exc).__name__)
        finally:
            await engine.dispose()
        if time.monotonic() + interval > deadline:
            return False
        await asyncio.sleep(interval)


def migrations_url(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("MIGRATIONS_DATABASE_URL") or env.get("DATABASE_URL") or ""


def alembic(url: str, *args: str) -> int:
    # Fixed argv, no shell; the URL goes through the environment, never the command line.
    env = {**os.environ, "DATABASE_URL": url, "MIGRATIONS_DATABASE_URL": url}
    cmd = [sys.executable, "-m", "alembic", *args]
    return subprocess.run(cmd, check=False, env=env).returncode  # noqa: S603 (fixed argv)


async def read_applied(url: str) -> list[str] | None:
    """The revisions recorded in the database's version table; None when it has none (a
    database Alembic never ran on). Read-only."""
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(text("SELECT to_regclass(:t)"), {"t": VERSION_TABLE})
            ).scalar_one()
            if exists is None:
                return None
            rows = await conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}"))  # noqa: S608
            return sorted(str(r[0]) for r in rows)
    finally:
        await engine.dispose()


def classify(applied: list[str] | None, ini: Path = ALEMBIC_INI) -> dict[str, Any]:
    """Compare the database's applied revisions with the scripts this image ships.

    state  at_head   every head is applied, nothing else is recorded
           pending   behind head (or never migrated); `pending` lists what upgrade would run
           diverged  a recorded revision this image does not know (a newer build migrated it,
                     or the table was edited), or the scripts have several heads: an upgrade
                     cannot be trusted to converge, a human decides
    Postgres runs each upgrade in one transaction, so a failed `upgrade head` leaves the last
    good revision recorded: it shows up here as `pending`, never as a half-applied state."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = sorted(script.get_heads())
    known = {rev.revision for rev in script.walk_revisions()}
    report: dict[str, Any] = {
        "heads": heads,
        "applied": applied or [],
        "pending": [],
        "unknown": [],
    }
    if len(heads) != 1:
        return {**report, "state": "diverged", "reason": "the migration scripts have several heads"}
    unknown = sorted(r for r in (applied or []) if r not in known)
    if unknown:
        return {
            **report,
            "unknown": unknown,
            "state": "diverged",
            "reason": "the database records revisions this build does not have",
        }
    base = tuple(applied) if applied else None
    pending = [rev.revision for rev in script.iterate_revisions(heads[0], base)]
    pending.reverse()  # oldest first, the order upgrade applies them
    if pending:
        return {**report, "pending": pending, "state": "pending", "reason": None}
    return {**report, "state": "at_head", "reason": None}


def check(url: str) -> int:
    try:
        applied = asyncio.run(read_applied(url))
    except Exception as exc:  # type only: messages can carry the DSN
        report: dict[str, Any] = {"state": "unreachable", "reason": type(exc).__name__}
        code = CHECK_UNREACHABLE
    else:
        report = classify(applied)
        code = {"at_head": CHECK_AT_HEAD, "pending": CHECK_PENDING}.get(
            report["state"], CHECK_DIVERGED
        )
    log.info("migrate_check", state=report["state"])
    print(CHECK_MARKER)
    print(json.dumps(report, sort_keys=True), flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wait for Postgres, then alembic upgrade head")
    ap.add_argument("--current-for", metavar="DB", help="only run `alembic current` on DB")
    ap.add_argument(
        "--rollback-drill", metavar="DB", help="downgrade/upgrade cycle on a SCRATCH database"
    )
    ap.add_argument("--steps", type=int, default=3, help="revisions to roll back (drill)")
    ap.add_argument(
        "--check", action="store_true", help="read-only: report whether the database is at head"
    )
    args = ap.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    url = migrations_url()
    if not url:
        log.error("migrate_failed", reason="DATABASE_URL is not set")
        return 2
    if args.check:
        return check(url)
    if args.rollback_drill:
        from app.workers.rollback_drill import run_drill

        try:
            scratch = url_for_database(url, args.rollback_drill)
        except ValueError:
            log.error("migrate_failed", reason="invalid database name")
            return 2
        if make_url(scratch).database == make_url(url).database:
            log.error("migrate_failed", reason="the drill never runs on the configured database")
            return 2
        report = run_drill(scratch, args.steps)
        # Logs share stdout: the report follows a fixed marker line.
        print(REPORT_MARKER)
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)
        return 0 if report["passed"] else 1
    if args.current_for:
        try:
            other = url_for_database(url, args.current_for)
        except ValueError:
            log.error("migrate_failed", reason="invalid database name")
            return 2
        return alembic(other, "current")
    wait_s = float(os.environ.get("MIGRATE_WAIT_SECONDS", "180"))
    if not asyncio.run(wait_for_postgres(url, wait_s)):
        log.error("migrate_failed", reason=f"postgres unreachable after {wait_s:.0f}s")
        return 1
    rc = alembic(url, "upgrade", "head")
    log.info("migrate_done", returncode=rc)
    if rc != 0:
        return rc
    # Never report success on alembic's exit code alone: verify the recorded state is head.
    try:
        report = classify(asyncio.run(read_applied(url)))
    except Exception as exc:
        log.error("migrate_verify_failed", error=type(exc).__name__)
        return 1
    if report["state"] != "at_head":
        log.error("migrate_not_at_head", state=report["state"], pending=report["pending"],
                  unknown=report["unknown"])  # fmt: skip
        return CHECK_PENDING if report["state"] == "pending" else CHECK_DIVERGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
