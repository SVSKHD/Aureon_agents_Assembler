"""Rollback drill (Phase 22): every migration from 0010 to head downgrades and upgrades again
on a scratch database of the test cluster, and the round trip loses nothing. Also the ops
wrapper scripts/ops/rollback-drill.sh (docker shimmed), which never touches the live DB."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.conftest import TEST_DATABASE_URL
from tests.test_ops_scripts import Ops

ROOT = Path(__file__).resolve().parents[1]
MARKER = "=== aureon rollback drill report ==="


def revisions_since(first: str) -> list[str]:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    chain = [r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()]
    return chain[: chain.index(first) + 1]  # head first


def test_the_chain_is_linear_and_every_revision_has_a_downgrade() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    for rev in script.walk_revisions():
        src = Path(rev.path).read_text()  # type: ignore[arg-type]
        assert "def downgrade()" in src, rev.revision
    assert revisions_since("0010")[-1] == "0010"


def _psql(url: str, sql: str) -> str:
    r = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", url, "-c", sql],
                       capture_output=True, text=True, check=True)  # fmt: skip
    return r.stdout.strip()


@pytest.mark.db
def test_every_revision_from_0010_to_head_is_reversible() -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    revs = revisions_since("0010")
    server = TEST_DATABASE_URL.replace("+asyncpg", "").rsplit("/", 1)[0]
    scratch = f"aureon_rbk_t{os.getpid()}_{uuid.uuid4().hex[:6]}"
    _psql(f"{server}/postgres", f'CREATE DATABASE "{scratch}"')
    try:
        r = subprocess.run(
            [sys.executable, "-m", "app.workers.migrate", "--rollback-drill", scratch,
             "--steps", str(len(revs))],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL, "MIGRATIONS_DATABASE_URL": ""},
        )  # fmt: skip
        assert MARKER in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]
        report = json.loads(r.stdout.split(MARKER, 1)[1])
        assert r.returncode == 0 and report["passed"], json.dumps(report, indent=2)[:4000]
        assert [d["from"] for d in report["downgrades"]] == revs
        assert report["downgrades"][-1]["to"] == "0009"
        assert [u["to"] for u in report["upgrades"]] == list(reversed(revs))
        assert report["schema_identical"] and report["objects"]["columns"] > 300
        assert report["smoke"]["errors"] == [] and report["smoke"]["version"] == revs[0]
        pw = urlsplit(TEST_DATABASE_URL).password
        assert not pw or f":{pw}@" not in r.stdout + r.stderr  # the DSN never leaks
    finally:
        _psql(f"{server}/postgres", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


def test_the_drill_refuses_the_configured_database() -> None:
    url = "postgresql+asyncpg://u:p@127.0.0.1:1/spasta"
    for db in ("spasta", "Bad-Name"):
        r = subprocess.run(
            [sys.executable, "-m", "app.workers.migrate", "--rollback-drill", db],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
            env={**os.environ, "DATABASE_URL": url, "MIGRATIONS_DATABASE_URL": ""},
        )  # fmt: skip
        assert r.returncode == 2, (db, r.stdout, r.stderr)


def test_migrations_url_prefers_the_migrator() -> None:
    from app.workers.migrate import migrations_url

    assert migrations_url({"DATABASE_URL": "a"}) == "a"
    assert migrations_url({"DATABASE_URL": "a", "MIGRATIONS_DATABASE_URL": "m"}) == "m"
    assert migrations_url({"DATABASE_URL": "a", "MIGRATIONS_DATABASE_URL": ""}) == "a"


# --- the ops wrapper ---------------------------------------------------------------------


def test_rollback_drill_script_creates_runs_and_drops_only_its_scratch_db(tmp_path: Path) -> None:
    ops = Ops(tmp_path)
    r = ops.run("rollback-drill.sh", "--steps", "2", AUREON_MIGRATOR_ROLE="aureon_migrator")
    # The fake shim answers no report: the drill must FAIL, and still drop its scratch db.
    assert r.returncode == 1
    calls = [" ".join(c) for c in ops.calls()]
    create = [c for c in calls if "CREATE DATABASE" in c]
    drop = [c for c in calls if "DROP DATABASE" in c]
    assert len(create) == 1 and '"aureon_rbk_' in create[0]
    assert 'OWNER "aureon_migrator"' in create[0]
    assert len(drop) == 1 and '"aureon_rbk_' in drop[0]
    run = [c for c in ops.calls() if "run" in c and "migrate" in c]
    assert "--rollback-drill" in run[0] and run[0][-1] == "2"
    assert "spasta" not in " ".join(run[0]).replace("spasta-data", "")


@pytest.mark.parametrize("steps", ["0", "x", "999"])
def test_rollback_drill_script_validates_steps(tmp_path: Path, steps: str) -> None:
    r = Ops(tmp_path).run("rollback-drill.sh", "--steps", steps)
    assert r.returncode == 2


@pytest.mark.db
def test_rollback_drill_script_end_to_end(tmp_path: Path) -> None:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    ops = Ops(tmp_path)
    u = urlsplit(TEST_DATABASE_URL.replace("+asyncpg", ""))
    env = {
        "SHIM_MODE": "local",
        "SHIM_DATABASE_URL": TEST_DATABASE_URL,
        "PGHOST": u.hostname or "127.0.0.1",
        "PGPORT": str(u.port or 5432),
        "PGPASSWORD": u.password or "",
        "PG_USER": u.username or "postgres",
        "PG_DB": u.path.lstrip("/"),
        "MIGRATIONS_DATABASE_URL": "",
    }
    r = ops.run("rollback-drill.sh", "--steps", "2", **env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    report = json.loads(r.stdout[r.stdout.index("{") :])
    assert report["passed"] and len(report["downgrades"]) == 2
    (saved,) = (ops.backups / "drills").glob("rollback-*.json")
    assert json.loads(saved.read_text())["passed"]
    left = _psql(
        TEST_DATABASE_URL.replace("+asyncpg", ""),
        "SELECT count(*) FROM pg_database WHERE datname LIKE 'aureon_rbk_2%'",
    )
    assert left == "0"
