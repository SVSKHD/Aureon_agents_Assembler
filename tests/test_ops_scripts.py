"""scripts/ops/*.sh with a fake `docker` (and `curl`) on PATH: the generated commands, the
refusals and the retention path guard. The `db` test runs a real backup -> restore drill
against the isolated test database, with the shim executing the commands locally."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "ops"
SECRET = "pw-should-never-be-printed-123"

SHIM = r'''#!PYTHON
"""Fake docker CLI. Logs argv; 'fake' mode answers canned output, 'local' mode runs the
in-container command on this machine (local Postgres, local python)."""
import json, os, subprocess, sys

argv = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
raw = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
stdin = raw.decode("utf-8", "replace")

def inner(args):
    if args[0] == "exec":                      # docker exec -i CONTAINER cmd...
        return args[3:]
    if args[0] == "compose":
        if "exec" in args:
            i = args.index("exec") + 1
            while args[i].startswith("-"):
                i += 1
            return args[i + 1:]
        if "run" in args:
            i = args.index("run") + 1
            while args[i].startswith("-"):
                i += 1
            return args[i + 1:]
        if "ps" in args:
            return ["__ps__"]
    return args

cmd = inner(argv)
mode = os.environ.get("SHIM_MODE", "fake")
if cmd == ["__ps__"]:
    print(os.environ.get("SHIM_PS", "[]"))
    sys.exit(0)
if mode == "local":
    env = dict(os.environ)
    if cmd[0] == "python":
        cmd = [sys.executable, *cmd[1:]]
        env["DATABASE_URL"] = os.environ["SHIM_DATABASE_URL"]
    r = subprocess.run(cmd, input=raw, env=env, cwd=os.environ["SHIM_CWD"])
    sys.exit(r.returncode)

prog = cmd[0]
if prog == "pg_dump":
    sys.stdout.write("PGDMP-fake " + " ".join(cmd) + "\n")
elif prog == "pg_restore":
    pass
elif prog == "psql":
    sql = stdin + " ".join(cmd)
    if "pg_get_functiondef" in sql:
        print("CREATE FUNCTION public.astra_uid() RETURNS text LANGUAGE sql AS $$ select 'x' $$;")
    elif "gexec" in sql:
        print(os.environ.get("SHIM_COUNTS", "core.users|3\npublic.trades|5"))
    elif "aureon_alembic_version" in sql:
        print("0010")
    elif "pg_database" in sql:
        print(os.environ.get("SHIM_DB_EXISTS", ""))
elif prog == "python":
    print("0010 (head)")
sys.exit(int(os.environ.get("SHIM_EXIT_" + prog.replace("-", "_"), "0")))
'''

CURL = r"""#!/usr/bin/env bash
for a in "$@"; do last="$a"; done
case "$last" in
  */api/health) echo '{"status":"ok"}' ;;
  */api/health/ready)
    if [[ " $* " == *" -w "* ]]; then printf '{"status":"%s"}\n%s' "$CURL_READY" "$CURL_CODE"; fi ;;
esac
"""


class Ops:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.data = tmp / "hdd"
        self.data.mkdir()
        (self.data / ".spasta-data-mounted").touch()
        (self.data / "images" / "u1").mkdir(parents=True)
        (self.data / "images" / "u1" / "a.png").write_bytes(b"\x89PNG")
        self.backups = self.data / "backups" / "aureon"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "docker.log"
        shim = self.bin / "docker"
        shim.write_text(SHIM.replace("#!PYTHON", f"#!{sys.executable}"))
        shim.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text(CURL)
        curl.chmod(0o755)
        self.env: dict[str, str] = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(tmp),
            "SHIM_LOG": str(self.log),
            "SHIM_CWD": str(ROOT),
            "OPS_ENV_FILE": str(tmp / "none.env"),
            "DATA_ROOT": str(self.data),
            "BACKUP_DIR": str(self.backups),
            "PG_COMPOSE_DIR": "/opt/private-ai",
            "PG_SERVICE": "postgres",
            "AUREON_DIR": str(ROOT),
            "PGPASSWORD": SECRET,
        }

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(OPS / script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def backup(self) -> Path:
        r = self.run("backup.sh")
        assert r.returncode == 0, r.stderr
        (dest,) = [p for p in self.backups.iterdir() if p.name.startswith("aureon-")]
        return dest


@pytest.fixture
def ops(tmp_path: Path) -> Ops:
    return Ops(tmp_path)


def _inner(call: list[str]) -> list[str]:
    i = call.index("-T") + 2 if "-T" in call else 0
    return call[i:]


# --- backup -------------------------------------------------------------------------------


def test_backup_writes_dumps_manifest_and_checksums(ops: Ops) -> None:
    dest = ops.backup()
    names = {p.name for p in dest.iterdir()}
    assert names == {
        "db-schemas.dump",
        "db-public.dump",
        "public-functions.sql",
        "row-counts.txt",
        "data.tar",
        "MANIFEST.json",
        "SHA256SUMS",
    }
    assert (dest / "SHA256SUMS").stat().st_mode & 0o077 == 0  # umask 077
    check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=dest, capture_output=True)
    assert check.returncode == 0
    m = json.loads((dest / "MANIFEST.json").read_text())
    assert m["row_counts"] == {"core.users": 3, "public.trades": 5}
    assert m["alembic_revision"] == "0010"
    assert m["data_dirs"] == ["images"]  # finance-attachments missing -> skipped
    assert set(m["files"]) >= {"db-schemas.dump", "db-public.dump", "data.tar"}
    with tarfile.open(dest / "data.tar") as t:
        assert "images/u1/a.png" in t.getnames()

    dumps = [c for c in ops.calls() if "pg_dump" in c]
    assert len(dumps) == 2
    for c in dumps:
        # docker compose exec -T on the Postgres project: no host port, no -h/-p.
        assert c[:5] == ["compose", "--project-directory", "/opt/private-ai", "exec", "-T"]
        inner = _inner(c)
        assert inner[:6] == ["pg_dump", "-Fc", "-U", "postgres", "-d", "spasta"]
        assert "-p" not in inner and "-h" not in inner
    schemas = _inner(dumps[0])
    assert [schemas[i + 1] for i, a in enumerate(schemas) if a == "-n"] == [
        "core",
        "audit",
        "ai",
        "agents",
        "trading",
        "dhan",
        "mt5",
        "github",
        "spasta",
    ]
    tables = _inner(dumps[1])
    assert [tables[i + 1] for i, a in enumerate(tables) if a == "-t"] == [
        "public.trades",
        "public.expenses",
        "public.secured",
        "public.astra_documents",
        "public.portfolio_profiles",
        "public.aureon_alembic_version",
    ]


def test_backup_writes_a_readable_status_file(ops: Ops) -> None:
    dest = ops.backup()
    status = ops.data / "ops-status" / "backup.json"
    assert status.stat().st_mode & 0o777 == 0o644  # the scheduler (another uid) reads it
    assert (status.parent.stat().st_mode & 0o777) == 0o755
    doc = json.loads(status.read_text())
    assert doc["status"] == "ok" and doc["last_success_backup"] == dest.name
    assert doc["last_success_at"] == doc["finished_at"]
    # A failed run records the failure and keeps the last success.
    r = ops.run("backup.sh", SHIM_EXIT_pg_dump="1")
    assert r.returncode != 0
    failed = json.loads(status.read_text())
    assert failed["status"] == "failed"
    assert failed["last_success_backup"] == dest.name
    assert failed["last_success_at"] == doc["last_success_at"]
    assert SECRET not in status.read_text()


def test_backup_uses_docker_exec_when_a_container_is_named(ops: Ops) -> None:
    r = ops.run("backup.sh", PG_CONTAINER="pg16")
    assert r.returncode == 0, r.stderr
    assert all(c[:3] == ["exec", "-i", "pg16"] for c in ops.calls())


def test_backup_never_prints_secrets(ops: Ops) -> None:
    r = ops.run("backup.sh")
    assert SECRET not in r.stdout + r.stderr
    assert all(SECRET not in " ".join(c) for c in ops.calls())


def test_backup_refuses_without_hdd_marker(ops: Ops) -> None:
    (ops.data / ".spasta-data-mounted").unlink()
    r = ops.run("backup.sh")
    assert r.returncode == 4 and "not mounted" in r.stderr
    assert ops.calls() == [] and not ops.backups.exists()


def test_backup_dir_must_be_on_the_hdd(ops: Ops, tmp_path: Path) -> None:
    r = ops.run("backup.sh", BACKUP_DIR=str(tmp_path / "ssd"))
    assert r.returncode == 4 and "not under DATA_ROOT" in r.stderr
    r = ops.run("backup.sh", BACKUP_DIR=str(ops.data / "x" / ".." / ".."))
    assert r.returncode == 4
    assert ops.calls() == []


def test_failed_dump_leaves_no_partial_backup(ops: Ops) -> None:
    r = ops.run("backup.sh", SHIM_EXIT_pg_dump="1")
    assert r.returncode != 0
    assert list(ops.backups.iterdir()) == []


def test_retention_deletes_only_old_complete_backups_inside_backup_dir(
    ops: Ops, tmp_path: Path
) -> None:
    ops.backups.mkdir(parents=True)

    def mk(name: str, manifest: bool = True) -> Path:
        d = ops.backups / name
        d.mkdir()
        if manifest:
            (d / "MANIFEST.json").write_text("{}")
        return d

    old = mk("aureon-20200101T000000Z")
    no_manifest = mk("aureon-20200102T000000Z", manifest=False)
    odd_name = mk("aureon-keep-me")
    recent = mk("aureon-29990101T000000Z")
    other = ops.backups / "notes.txt"
    other.write_text("keep")
    outside = tmp_path / "outside" / "aureon-20200103T000000Z"
    outside.mkdir(parents=True)
    (outside / "MANIFEST.json").write_text("{}")
    link = ops.backups / "aureon-20200104T000000Z"
    link.symlink_to(outside, target_is_directory=True)

    r = ops.run("backup.sh", BACKUP_RETENTION_DAYS="7")
    assert r.returncode == 0, r.stderr
    assert not old.exists()
    for kept in (no_manifest, odd_name, recent, other, outside, link):
        assert kept.exists() or kept.is_symlink(), kept
    assert (outside / "MANIFEST.json").exists()
    assert "removing aureon-20200101T000000Z" in r.stderr


@pytest.mark.parametrize("days", ["0", "-1", "abc"])
def test_retention_must_be_positive(ops: Ops, days: str) -> None:
    r = ops.run("backup.sh", BACKUP_RETENTION_DAYS=days)
    assert r.returncode == 2 and ops.calls() == []


def test_dry_run_writes_nothing(ops: Ops) -> None:
    r = ops.run("backup.sh", "--dry-run")
    assert r.returncode == 0 and "pg_dump -Fc" in r.stderr
    assert ops.calls() == [] and not ops.backups.exists()


# --- restore ------------------------------------------------------------------------------


def _restores(ops: Ops) -> list[list[str]]:
    return [_inner(c) for c in ops.calls() if "pg_restore" in c]


def test_restore_defaults_to_a_new_database(ops: Ops) -> None:
    dest = ops.backup()
    ops.log.unlink()
    r = ops.run("restore.sh", dest.name)
    assert r.returncode == 0, r.stderr
    target = r.stdout.strip()
    assert target.startswith("spasta_restore_")
    sql = [" ".join(c) for c in ops.calls()]
    assert any(f'CREATE DATABASE "{target}" TEMPLATE template0' in s for s in sql)
    restores = _restores(ops)
    assert len(restores) == 2
    for c in restores:
        assert c[c.index("-d") + 1] == target
        assert "--clean" not in c
    assert not any("-d spasta " in s + " " for s in sql if "pg_restore" in s)


def test_restore_latest(ops: Ops) -> None:
    ops.backup()
    r = ops.run("restore.sh", "latest")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--into-live"],
        ["--target-db", "spasta"],
        ["--yes-i-am-sure", "--target-db", "spasta"],
    ],
)
def test_restore_refuses_the_live_db_without_both_flags(ops: Ops, args: list[str]) -> None:
    dest = ops.backup()
    ops.log.unlink()
    r = ops.run("restore.sh", dest.name, *args)
    assert r.returncode == 3, r.stderr
    assert "live" in r.stderr.lower()
    assert _restores(ops) == []
    assert not any("CREATE DATABASE" in " ".join(c) for c in ops.calls())


def test_restore_into_live_with_both_flags(ops: Ops) -> None:
    dest = ops.backup()
    ops.log.unlink()
    r = ops.run("restore.sh", dest.name, "--into-live", "--yes-i-am-sure")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "spasta"
    for c in _restores(ops):
        assert c[c.index("-d") + 1] == "spasta"
        assert "--single-transaction" in c and "--clean" in c and "--exit-on-error" in c
    assert not any("CREATE DATABASE" in " ".join(c) for c in ops.calls())


def test_restore_verifies_checksums_before_touching_anything(ops: Ops) -> None:
    dest = ops.backup()
    with (dest / "db-public.dump").open("a") as f:
        f.write("tampered")
    ops.log.unlink()
    r = ops.run("restore.sh", dest.name)
    assert r.returncode == 3 and "checksum" in r.stderr
    assert ops.calls() == []


def test_restore_refuses_an_existing_target(ops: Ops) -> None:
    dest = ops.backup()
    r = ops.run("restore.sh", dest.name, "--target-db", "taken", SHIM_DB_EXISTS="1")
    assert r.returncode == 3 and "already exists" in r.stderr


@pytest.mark.parametrize("name", ['x"; drop database spasta; --', "Upper", "a-b"])
def test_restore_rejects_bad_db_names(ops: Ops, name: str) -> None:
    dest = ops.backup()
    r = ops.run("restore.sh", dest.name, "--target-db", name)
    assert r.returncode == 2


def test_restore_with_data_never_writes_over_live_folders(ops: Ops) -> None:
    dest = ops.backup()
    (ops.data / "images" / "u1" / "a.png").write_bytes(b"live")
    r = ops.run("restore.sh", dest.name, "--with-data")
    assert r.returncode == 0, r.stderr
    assert (ops.data / "images" / "u1" / "a.png").read_bytes() == b"live"
    (restored,) = ops.data.glob("restore-*")
    assert (restored / "images" / "u1" / "a.png").read_bytes() == b"\x89PNG"


# --- drill --------------------------------------------------------------------------------


def test_drill_restores_checks_and_drops_only_its_scratch_db(ops: Ops) -> None:
    ops.backup()
    ops.log.unlink()
    r = ops.run("restore-drill.sh")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout[r.stdout.index("{") :])
    assert report["passed"] and report["alembic_current"] == "0010"
    db = report["scratch_database"]
    assert db.startswith("spasta_drill_")
    sql = [" ".join(c) for c in ops.calls()]
    drops = [s for s in sql if "DROP DATABASE" in s]
    assert drops == [s for s in drops if f'"{db}"' in s] and len(drops) == 1
    run = [c for c in ops.calls() if "run" in c]
    assert run and run[0][-2:] == ["--current-for", db]
    assert (ops.backups / "drills").is_dir()


def test_drill_lets_the_migrator_read_only_the_scratch_version_table(ops: Ops) -> None:
    """restore.sh restores --no-owner --no-privileges; the migrate image connects as the
    migrator role, so `alembic current` needs SELECT on the scratch copy's version table
    (found on the first production drill)."""
    ops.backup()
    ops.log.unlink()
    r = ops.run("restore-drill.sh", AUREON_MIGRATOR_ROLE="aureon_migrator")
    assert r.returncode == 0, r.stderr
    db = json.loads(r.stdout[r.stdout.index("{") :])["scratch_database"]
    grants = [c for c in ops.calls() if any("GRANT" in a for a in c)]
    assert len(grants) == 1
    g = " ".join(grants[0])
    assert 'GRANT SELECT ON public.aureon_alembic_version TO "aureon_migrator"' in g
    assert f"-d {db}" in g
    r = ops.run("restore-drill.sh", AUREON_MIGRATOR_ROLE="bad;role")
    assert r.returncode == 2


def test_drill_fails_on_row_count_mismatch_and_still_drops(ops: Ops) -> None:
    ops.backup()
    ops.log.unlink()
    r = ops.run("restore-drill.sh", SHIM_COUNTS="core.users|3\npublic.trades|4")
    assert r.returncode == 1
    assert '"public.trades"' in r.stdout
    assert any("DROP DATABASE" in " ".join(c) for c in ops.calls())


def test_drill_does_not_drop_a_db_it_did_not_create(ops: Ops) -> None:
    ops.backup()
    ops.log.unlink()
    r = ops.run("restore-drill.sh", SHIM_DB_EXISTS="1")
    assert r.returncode == 3
    assert not any("DROP DATABASE" in " ".join(c) for c in ops.calls())


# --- healthcheck --------------------------------------------------------------------------

PS_OK = json.dumps(
    [
        {"Service": "api", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"Service": "redis", "State": "running", "Health": "healthy", "ExitCode": 0},
        {"Service": "migrate", "State": "exited", "Health": "", "ExitCode": 0},
        {"Service": "mount-check", "State": "exited", "Health": "", "ExitCode": 0},
    ]
)


def test_healthcheck_ok(ops: Ops) -> None:
    r = ops.run("healthcheck.sh", SHIM_PS=PS_OK, CURL_READY="ready", CURL_CODE="200")
    assert r.returncode == 0, r.stdout + r.stderr


def test_healthcheck_flags_not_ready_and_unhealthy(ops: Ops) -> None:
    rows: list[dict[str, Any]] = json.loads(PS_OK)
    rows[1]["Health"] = "unhealthy"
    r = ops.run("healthcheck.sh", SHIM_PS=json.dumps(rows), CURL_READY="no", CURL_CODE="503")
    assert r.returncode == 1
    assert "ready answered 503" in r.stderr and "BAD: redis" in r.stdout


# --- lint ---------------------------------------------------------------------------------


def test_scripts_are_strict_and_shellcheck_clean() -> None:
    scripts = sorted(OPS.glob("*.sh"))
    for s in scripts:
        if s.name != "lib.sh":
            assert "set -euo pipefail" in s.read_text(), s
            assert os.access(s, os.X_OK), s
    sc = shutil.which("shellcheck") or str(Path(sys.executable).parent / "shellcheck")
    if not Path(sc).exists():
        pytest.skip("shellcheck not installed")
    r = subprocess.run([sc, "-x", *map(str, scripts)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# --- real round trip (db) -----------------------------------------------------------------


@pytest.mark.db
def test_real_backup_and_restore_drill(ops: Ops, migrated: str) -> None:
    if not shutil.which("pg_dump"):
        pytest.skip("pg_dump not installed")
    u = urlsplit(migrated.replace("+asyncpg", ""))
    env = {
        "SHIM_MODE": "local",
        "SHIM_DATABASE_URL": migrated,
        "PGHOST": u.hostname or "127.0.0.1",
        "PGPORT": str(u.port or 5432),
        "PGPASSWORD": u.password or "",
        "PG_USER": u.username or "postgres",
        "PG_DB": u.path.lstrip("/"),
        "BACKUP_PUBLIC_FUNCTIONS": "astra_uid",
    }
    r = ops.run("backup.sh", **env)
    assert r.returncode == 0, r.stderr
    (dest,) = ops.backups.glob("aureon-*")
    m = json.loads((dest / "MANIFEST.json").read_text())
    assert m["alembic_revision"] and "core.users" in m["row_counts"]
    assert any(k.startswith("trading.") for k in m["row_counts"])
    r = ops.run("restore-drill.sh", **env)
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads(r.stdout[r.stdout.index("{") :])
    assert report["passed"] and report["tables_checked"] > 20
    assert "dumps_with_errors=0" in r.stderr, r.stderr
    left = subprocess.run(
        ["psql", "-At", "-U", env["PG_USER"], "-d", env["PG_DB"], "-c",
         "SELECT count(*) FROM pg_database WHERE datname LIKE 'spasta_drill_%'"],
        env={**os.environ, **env}, capture_output=True, text=True,
    )  # fmt: skip
    assert left.stdout.strip() == "0"


def test_healthcheck_wait_retries_then_gives_up(ops: Ops) -> None:
    r = ops.run(
        "healthcheck.sh",
        "--wait",
        "2",
        SHIM_PS=PS_OK,
        CURL_READY="no",
        CURL_CODE="503",
        HEALTHCHECK_RETRY_SECONDS="1",
    )
    assert r.returncode == 1
    assert "retrying in 1s" in r.stderr


def test_systemd_units_order_after_docker_and_the_hdd() -> None:
    unit = (ROOT / "deploy" / "systemd" / "aureon.service").read_text()
    assert "RequiresMountsFor=/mnt/spasta-data" in unit
    assert "After=docker.service" in unit and "Requires=docker.service" in unit
    assert "ExecStart=/usr/bin/docker compose up -d" in unit
    for name in ("aureon-backup", "aureon-restore-drill"):
        svc = (ROOT / "deploy" / "systemd" / f"{name}.service").read_text()
        timer = (ROOT / "deploy" / "systemd" / f"{name}.timer").read_text()
        assert "RequiresMountsFor=/mnt/spasta-data" in svc and "Type=oneshot" in svc
        assert "OnCalendar=" in timer and "Persistent=true" in timer
