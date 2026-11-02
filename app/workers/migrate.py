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

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.logging import configure_logging, get_logger

log = get_logger("migrate")
REPORT_MARKER = "=== aureon rollback drill report ==="
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="wait for Postgres, then alembic upgrade head")
    ap.add_argument("--current-for", metavar="DB", help="only run `alembic current` on DB")
    ap.add_argument(
        "--rollback-drill", metavar="DB", help="downgrade/upgrade cycle on a SCRATCH database"
    )
    ap.add_argument("--steps", type=int, default=3, help="revisions to roll back (drill)")
    args = ap.parse_args(argv)
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    url = migrations_url()
    if not url:
        log.error("migrate_failed", reason="DATABASE_URL is not set")
        return 2
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
    return rc


if __name__ == "__main__":
    sys.exit(main())
