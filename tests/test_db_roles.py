"""deploy/sql/roles.sql (Phase 22): scoped roles applied to a scratch database on the test
cluster (we are superuser there), with unique role names. The app role has no DDL, the audit
trail is append-only for it, and normal CRUD (plus the real app over HTTP) still works."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL
from tests.test_ops_scripts import Ops

ROOT = Path(__file__).resolve().parents[1]
ROLES_SQL = ROOT / "deploy" / "sql" / "roles.sql"
PASSWORD = "correct horse battery staple"


# --- static checks (no DB) -----------------------------------------------------------------


def _code_lines() -> list[str]:
    return [ln.split("--", 1)[0] for ln in ROLES_SQL.read_text().splitlines()]


def test_roles_sql_never_drops_or_deletes() -> None:
    code = "\n".join(_code_lines()).upper()
    for word in ("DROP ", "DELETE FROM", "TRUNCATE TABLE", "ALTER TABLE .* DROP"):
        assert not re.search(word, code), word
    # REVOKE TRUNCATE is a privilege name, never a TRUNCATE statement.
    assert not re.search(r"^\s*TRUNCATE\b", code, re.M)
    # No password literal in the file: only psql variables.
    assert re.search(r"PASSWORD\s+:'app_password'", ROLES_SQL.read_text())
    assert not re.search(r"PASSWORD\s+'", ROLES_SQL.read_text(), re.I)


def test_every_audit_table_is_classified() -> None:
    import app.models  # noqa: F401
    from app.db.base import Base

    src = ROLES_SQL.read_text()
    append_only = set(re.search(r"AUDIT_APPEND_ONLY:\s*(.+)", src).group(1).split())  # type: ignore[union-attr]
    mutable = set(re.search(r"AUDIT_MUTABLE:\s*(.+)", src).group(1).split())  # type: ignore[union-attr]
    audit = {t.name for t in Base.metadata.sorted_tables if t.schema == "audit"}
    assert audit == append_only | mutable, "classify new audit tables in deploy/sql/roles.sql"
    assert not append_only & mutable
    assert {"audit_events", "approval_events"} <= append_only
    for t in append_only:
        assert f"'{t}'" in src.split("AUDIT_MUTABLE")[1], t  # really in the REVOKE loop
    # Every schema with app tables gets ownership + grants.
    schemas = {t.schema for t in Base.metadata.sorted_tables}
    for s in schemas:
        assert f"'{s}'" in src, s


# --- against the cluster -------------------------------------------------------------------


@dataclass
class Cluster:
    admin: str  # libpq URL of the scratch DB as superuser
    db: str
    roles: dict[str, str]
    passwords: dict[str, str]

    def url(self, who: str, driver: str = "postgresql+asyncpg") -> str:
        u = urlsplit(self.admin)
        return (
            f"{driver}://{self.roles[who]}:{self.passwords[who]}@{u.hostname}:{u.port or 5432}"
            f"/{self.db}"
        )


def _psql(url: str, sql: str) -> str:
    r = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", url, "-c", sql],
                       capture_output=True, text=True, check=True)  # fmt: skip
    return r.stdout.strip()


def _apply(c: Cluster, tmp: Path, **pw: str) -> subprocess.CompletedProcess[str]:
    """Through the wrapper (scripts/ops/apply-db-roles.sh), docker shimmed to local psql."""
    tmp.mkdir(parents=True, exist_ok=True)
    ops = Ops(tmp)
    u = urlsplit(c.admin)
    env = {
        "SHIM_MODE": "local",
        "SHIM_DATABASE_URL": "unused",
        "PGHOST": u.hostname or "127.0.0.1",
        "PGPORT": str(u.port or 5432),
        "PGPASSWORD": u.password or "",
        "PG_USER": u.username or "postgres",
        "PG_DB": c.db,
        "AUREON_APP_ROLE": c.roles["app"],
        "AUREON_MIGRATOR_ROLE": c.roles["migrator"],
        "AUREON_READONLY_ROLE": c.roles["readonly"],
        "AUREON_AUDIT_WRITER_ROLE": c.roles["audit_writer"],
        **pw,
    }
    return ops.run("apply-db-roles.sh", **env)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set (run `make test-db`)")
    base = TEST_DATABASE_URL.replace("+asyncpg", "")
    server = base.rsplit("/", 1)[0]
    tag = f"r{os.getpid()}_{uuid.uuid4().hex[:6]}"
    db = f"aureon_roles_{tag}"
    roles = {k: f"t{tag}_{k}" for k in ("app", "migrator", "readonly", "audit_writer")}
    passwords = {k: uuid.uuid4().hex + "Aa" for k in ("app", "migrator", "readonly")}
    _psql(f"{server}/postgres", f'CREATE DATABASE "{db}"')
    async_server = server.replace("postgresql:", "postgresql+asyncpg:")
    c = Cluster(f"{server}/{db}", db, roles, passwords)
    try:
        # The pre-roles state: everything migrated (and owned) by the superuser.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True, capture_output=True, cwd=ROOT,
            env={**os.environ, "DATABASE_URL": f"{async_server}/{db}",
                 "MIGRATIONS_DATABASE_URL": ""},
        )  # fmt: skip
        tmp = tmp_path_factory.mktemp("roles")
        pw = {
            "AUREON_APP_DB_PASSWORD": passwords["app"],
            "AUREON_MIGRATOR_DB_PASSWORD": passwords["migrator"],
            "AUREON_READONLY_DB_PASSWORD": passwords["readonly"],
        }
        r = _apply(c, tmp / "a", **pw)
        assert r.returncode == 0, r.stderr
        assert passwords["app"] not in r.stdout + r.stderr
        r = _apply(c, tmp / "b")  # idempotent; no passwords = keep them
        assert r.returncode == 0, r.stderr
        yield c
    finally:
        _psql(f"{server}/postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for name in roles.values():
            _psql(f"{server}/postgres", f'DROP ROLE IF EXISTS "{name}"')


async def _engine(c: Cluster, who: str) -> AsyncEngine:
    return create_async_engine(c.url(who))


async def _denied(engine: AsyncEngine, sql: str) -> str:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError) as e:
            await conn.execute(text(sql))
        await conn.rollback()
    return str(e.value.orig)


@pytest.fixture
async def app_engine(cluster: Cluster) -> AsyncIterator[AsyncEngine]:
    e = await _engine(cluster, "app")
    yield e
    await e.dispose()


@pytest.mark.db
async def test_role_attributes_and_ownership(cluster: Cluster) -> None:
    r = cluster.roles
    names = ",".join(f"'{n}'" for n in r.values())
    attrs = _psql(
        cluster.admin,
        "SELECT rolname || ':' || rolsuper || rolcreatedb || rolcreaterole || rolcanlogin "
        f"|| rolbypassrls FROM pg_roles WHERE rolname IN ({names}) ORDER BY 1",
    )
    got = {
        k: v.replace("true", "t").replace("false", "f")
        for k, v in (line.split(":") for line in attrs.splitlines())
    }
    assert got[r["app"]] == "ffftt"  # login, BYPASSRLS (D-A7), nothing else
    assert got[r["migrator"]] == "ffftf"
    assert got[r["readonly"]] == "ffftt"
    assert got[r["audit_writer"]] == "fffff"  # NOLOGIN group
    owners = _psql(
        cluster.admin,
        "SELECT DISTINCT nspowner::regrole FROM pg_namespace WHERE nspname IN "
        "('core','ai','agents','trading','dhan','mt5','github','audit','spasta')",
    )
    assert owners == r["migrator"]
    tables = _psql(
        cluster.admin,
        "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname IN "
        "('core','ai','agents','trading','dhan','mt5','github','audit')",
    )
    assert tables == r["migrator"]
    # spasta: created empty and documented.
    assert _psql(cluster.admin, "SELECT count(*) FROM pg_tables WHERE schemaname='spasta'") == "0"
    assert "Reserved" in _psql(cluster.admin, "SELECT obj_description('spasta'::regnamespace)")
    assert _psql(cluster.admin, "SELECT tableowner FROM pg_tables WHERE "
                 "tablename='aureon_alembic_version'") == r["migrator"]  # fmt: skip


@pytest.mark.db
async def test_app_role_has_no_ddl(app_engine: AsyncEngine) -> None:
    for sql in (
        "CREATE TABLE core.evil (i int)",
        "CREATE TABLE public.evil (i int)",
        "CREATE TABLE spasta.evil (i int)",
        "CREATE TABLE audit.evil (i int)",
        "CREATE SCHEMA evil",
        "DROP TABLE core.users",
        "DROP TABLE audit.audit_events",
        "DROP TABLE public.expenses",
        "ALTER TABLE core.users ADD COLUMN evil int",
        "TRUNCATE core.users",
        "TRUNCATE public.trades",
        "CREATE INDEX evil ON core.users (email)",
    ):
        msg = await _denied(app_engine, sql)
        assert "permission denied" in msg or "must be owner" in msg, (sql, msg)


@pytest.mark.db
async def test_audit_is_append_only_for_the_app(app_engine: AsyncEngine) -> None:
    async with app_engine.begin() as conn:
        aid = await conn.scalar(
            text("INSERT INTO audit.audit_events (event_type, details) "
                 "VALUES ('roles.test', '{}') RETURNING id")
        )  # fmt: skip
        assert aid is not None
        assert await conn.scalar(text("SELECT count(*) FROM audit.audit_events")) >= 1
    for sql in (
        f"UPDATE audit.audit_events SET details = '{{}}' WHERE id = {aid}",
        f"DELETE FROM audit.audit_events WHERE id = {aid}",
        "UPDATE audit.approval_events SET request_id = request_id",
        "DELETE FROM audit.approval_events",
        "DELETE FROM audit.approval_artifacts",
        "TRUNCATE audit.audit_events",
    ):
        msg = await _denied(app_engine, sql)
        assert "permission denied" in msg, (sql, msg)
    # The mutable approval tables stay usable.
    async with app_engine.begin() as conn:
        await conn.execute(text("UPDATE audit.approval_policies SET enabled = enabled"))
        await conn.execute(text("UPDATE audit.approval_requests SET status = status"))


@pytest.mark.db
async def test_app_role_crud(app_engine: AsyncEngine) -> None:
    async with app_engine.begin() as conn:
        uid = await conn.scalar(
            text("INSERT INTO core.users (email, password_hash, role_id) "
                 "VALUES ('roles@spasta.online', 'x', 2) RETURNING id")
        )  # fmt: skip
        await conn.execute(
            text("UPDATE core.users SET password_hash = 'y' WHERE id = :u"), {"u": uid}
        )
        await conn.execute(
            text(
                "INSERT INTO public.expenses (user_id, id, date, amount, category, note, kind, "
                "created_at, updated_at, aureon_user_id) VALUES ('u', 'e1', '2026-10-01', 1.5, "
                "'Food', '', 'one-off', now(), now(), :u)"
            ),
            {"u": uid},
        )
        await conn.execute(text("UPDATE public.expenses SET note = 'n' WHERE id = 'e1'"))
        await conn.execute(text("DELETE FROM public.expenses WHERE id = 'e1'"))
        await conn.execute(text("INSERT INTO agents.agents (id, name, kind, state) "
                                "VALUES ('roles-test', 'r', 'test', 'idle')"))  # fmt: skip
        await conn.execute(text("DELETE FROM agents.agents WHERE id = 'roles-test'"))
        await conn.execute(text("DELETE FROM core.users WHERE id = :u"), {"u": uid})
        assert await conn.scalar(text("SELECT version_num FROM public.aureon_alembic_version"))


@pytest.mark.db
async def test_readonly_role_only_reads(cluster: Cluster) -> None:
    e = await _engine(cluster, "readonly")
    try:
        async with e.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM core.users")) is not None
            assert await conn.scalar(text("SELECT count(*) FROM public.trades")) is not None
        for sql in (
            "INSERT INTO agents.agents (id, name, kind, state) VALUES ('x','x','x','idle')",
            "DELETE FROM core.users",
            "CREATE TABLE core.evil (i int)",
        ):
            assert "permission denied" in await _denied(e, sql)
    finally:
        await e.dispose()


@pytest.mark.db
async def test_the_real_app_runs_as_the_app_role(cluster: Cluster, settings: Settings) -> None:
    s = Settings.model_validate({**settings.model_dump(), "DATABASE_URL": cluster.url("app")})
    engine = create_async_engine(cluster.url("app"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_admin(maker, "roles-admin@spasta.online", PASSWORD)
    await engine.dispose()
    app = create_app(s)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        r = await c.post(
            "/api/auth/login", json={"email": "roles-admin@spasta.online", "password": PASSWORD}
        )
        assert r.status_code == 200, r.text
        h = {"x-csrf-token": r.json()["csrf_token"]}
        r = await c.post("/api/finance/expenses", headers=h,
                         json={"id": "e-roles", "date": "2026-10-02", "amount": "120.50",
                               "category": "Food"})  # fmt: skip
        assert r.status_code == 201, r.text
        assert (await c.get("/api/finance/expenses")).json()["total"] >= 1
        r = await c.patch("/api/finance/expenses/e-roles", headers=h, json={"note": "lunch"})
        assert r.status_code == 200, r.text
        assert (await c.delete("/api/finance/expenses/e-roles", headers=h)).status_code == 204
        assert (await c.get("/api/v1/audit/events")).status_code == 200
        assert (await c.post("/api/auth/logout", headers=h)).status_code == 204
    n = _psql(
        cluster.admin,
        "SELECT count(*) FROM audit.audit_events WHERE event_type IN ('login', 'logout')",
    )
    assert int(n) >= 2  # login + logout were recorded by the app role


@pytest.mark.db
def test_migrator_runs_migrations_and_grants_follow(cluster: Cluster, tmp_path: Path) -> None:
    """The migrate service path: MIGRATIONS_DATABASE_URL (the migrator) wins over
    DATABASE_URL (the app role, which could not run DDL)."""
    env = {**os.environ, "DATABASE_URL": cluster.url("app"),
           "MIGRATIONS_DATABASE_URL": cluster.url("migrator")}  # fmt: skip
    for step in (["downgrade", "-1"], ["upgrade", "head"]):
        r = subprocess.run([sys.executable, "-m", "alembic", *step], cwd=ROOT, env=env,
                           capture_output=True, text=True)  # fmt: skip
        assert r.returncode == 0, r.stderr[-2000:]
    # Tables the migrator just re-created: the append-only ones are still append-only for
    # the app (fail closed), and plain-schema tables got DML through default privileges.
    priv = _psql(
        cluster.admin,
        f"SELECT has_table_privilege('{cluster.roles['app']}', 'audit.approval_events', "
        f"'UPDATE')::text || has_table_privilege('{cluster.roles['app']}', "
        f"'audit.approval_events', 'INSERT')::text",
    )
    assert priv == "falsetrue"
    # Re-running roles.sql after a migration restores the explicit audit grants.
    r = _apply(cluster, tmp_path / "re")
    assert r.returncode == 0, r.stderr
    assert (
        _psql(
            cluster.admin,
            f"SELECT has_table_privilege('{cluster.roles['app']}', 'audit.approval_policies', "
            "'UPDATE')",
        )
        == "t"
    )
    # The app role is refused by alembic DDL.
    env_app = {**os.environ, "DATABASE_URL": cluster.url("app"), "MIGRATIONS_DATABASE_URL": ""}
    r = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "-1"], cwd=ROOT,
                       env=env_app, capture_output=True, text=True)  # fmt: skip
    assert r.returncode != 0 and "must be owner" in r.stderr + r.stdout
