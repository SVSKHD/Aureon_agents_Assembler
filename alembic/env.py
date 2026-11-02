"""Async Alembic environment.

Safety: the `spasta` database already holds tables this app does not own yet
(public.trades, public.expenses, ...). `include_object` limits autogenerate to tables
declared in our metadata, so a `--autogenerate` run can never propose dropping them.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  (registers every table on Base.metadata)
from alembic import context
from app.db.base import Base

# Own version table name so we never collide with another tool's `alembic_version`
# in the shared `spasta` database.
VERSION_TABLE = "aureon_alembic_version"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    # Reflected-only tables (exist in DB, absent from our models) are never touched.
    return not (type_ == "table" and reflected and compare_to is None)


def _configure(**kw: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        include_schemas=True,
        compare_type=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
