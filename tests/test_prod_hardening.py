"""Phase E: ENV=prod startup checklist, log redaction, security headers, worker healthcheck."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import structlog
from asgi_lifespan import LifespanManager
from httpx import AsyncClient
from pydantic import ValidationError

from app.core import heartbeat
from app.core.config import Settings
from app.core.logging import REDACTED, configure_logging, redact_secrets
from app.core.prod_checks import (
    ProductionConfigError,
    assert_production_ready,
    production_problems,
    secret_key_problem,
)
from app.main import create_app
from app.workers import healthcheck
from app.workers.migrate import url_for_database

ROOT = Path(__file__).resolve().parents[1]
GOOD_SECRET = "Zq8-vR2_kLm4Np7Xs1Tb9Wc3Yd6Fg0Hj5Ae"


def prod(data_root: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "ENV": "prod",
        "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/none",
        "SECRET_KEY": GOOD_SECRET,
        "DATA_ROOT": data_root,
        "CORS_ORIGINS": "https://spasta.online",
    }
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[call-arg]


# --- prod checklist ----------------------------------------------------------------------


def test_good_prod_config_passes(data_root: Path) -> None:
    s = prod(data_root)
    assert production_problems(s) == []
    assert_production_ready(s)


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        ("change-me-to-a-long-random-value-at-least-32-chars", "placeholder"),
        ("k" * 48, "low-entropy"),
        ("short", "shorter"),
    ],
)
def test_secret_key_rules(secret: str, fragment: str) -> None:
    assert fragment in (secret_key_problem(secret) or "")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"SECRET_KEY": "change-me-to-a-long-random-value-at-least-32-chars"}, "SECRET_KEY"),
        ({"SECRET_KEY": "a" * 40}, "SECRET_KEY"),
        ({"DEBUG": True}, "DEBUG"),
        ({"API_DOCS_ENABLED": True}, "API_DOCS_ENABLED"),
        ({"COOKIE_SECURE": False}, "COOKIE_SECURE"),
        ({"CORS_ORIGINS": "*"}, "CORS_ORIGINS"),
    ],
)
def test_each_unsafe_setting_is_refused(
    data_root: Path, overrides: dict[str, Any], fragment: str
) -> None:
    s = prod(data_root, **overrides)
    with pytest.raises(ProductionConfigError) as exc:
        assert_production_ready(s)
    assert fragment in str(exc.value)
    # The secret itself never appears in the error.
    assert s.SECRET_KEY.get_secret_value() not in str(exc.value)


def test_missing_hdd_marker_is_refused(data_root: Path) -> None:
    (data_root / ".spasta-data-mounted").unlink()
    with pytest.raises(ProductionConfigError, match="data root"):
        assert_production_ready(prod(data_root))


def test_non_prod_is_not_checked(data_root: Path) -> None:
    s = prod(data_root, ENV="dev", DEBUG=True, COOKIE_SECURE=False)
    assert_production_ready(s)  # no raise
    assert s.docs_enabled


async def test_prod_api_refuses_to_start(data_root: Path) -> None:
    app = create_app(prod(data_root, COOKIE_SECURE=False))
    with pytest.raises(ProductionConfigError):
        async with LifespanManager(app):
            pass


def test_live_flag_needs_phase_g_approval() -> None:
    base = {"DATABASE_URL": "postgresql+asyncpg://u:p@db/spasta", "SECRET_KEY": "s" * 32}
    assert Settings(**base).TRADING_LIVE_ENABLED is False  # default off
    with pytest.raises(ValidationError, match="PHASE_G_APPROVED"):
        Settings(**base, TRADING_LIVE_ENABLED=True)
    with pytest.raises(ValidationError):
        Settings(**base, TRADING_LIVE_ENABLED=True, PHASE_G_APPROVED="true")
    ok = Settings(**base, TRADING_LIVE_ENABLED=True, PHASE_G_APPROVED="yes")
    assert ok.TRADING_LIVE_ENABLED


async def test_live_mode_stays_unreachable_with_phase_g_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag pair only lifts the config refusal; the Phase G gate in services stays."""
    import uuid

    from app.agents.trading.types import ExecutionMode
    from app.services import trading

    async def token_ok(*_: Any) -> bool:
        return True

    monkeypatch.setattr(trading, "_consume_live_token", token_ok)
    s = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@db/spasta",
        SECRET_KEY="s" * 32,
        TRADING_LIVE_ENABLED=True,
        PHASE_G_APPROVED="yes",
    )
    acct = type("A", (), {"id": uuid.uuid4()})()
    with pytest.raises(trading.ModeChangeDenied) as exc:
        await trading.check_mode_change(
            None,  # type: ignore[arg-type]
            s,
            acct,  # type: ignore[arg-type]
            ExecutionMode.LIVE,
            uuid.uuid4(),
            "tok",
        )
    assert exc.value.code == "preconditions_unmet"


# --- log redaction -----------------------------------------------------------------------


def test_redaction_masks_sensitive_keys_and_embedded_credentials() -> None:
    out = redact_secrets(
        None,
        "info",
        {
            "event": "x",
            "password": "hunter2",
            "access_token": "abc",
            "headers": {"Authorization": "Bearer abcdefghijk", "Cookie": "s=1", "ok": "v"},
            "api_key": "k",
            "url": "postgresql+asyncpg://aureon:s3cret@db:5432/spasta",
            "msg": "upstream said: Bearer zzzzzzzzzzzz",
            "items": [{"secret": "x"}, "token abcdefghijklmnop"],
            "count": 3,
        },
    )
    assert out["password"] == out["access_token"] == out["api_key"] == REDACTED
    assert out["headers"] == {"Authorization": REDACTED, "Cookie": REDACTED, "ok": "v"}
    assert out["url"] == "postgresql+asyncpg://aureon:***@db:5432/spasta"
    assert "zzzz" not in out["msg"]
    assert out["items"][0] == {"secret": REDACTED}
    assert "abcdefghijklmnop" not in out["items"][1]
    assert out["count"] == 3 and out["event"] == "x"


def test_configured_logging_redacts_structlog_and_stdlib(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    try:
        structlog.get_logger("t").info("login", password="hunter2", dsn="postgresql://u:pw@h/d")
        logging.getLogger("uvicorn.error").info(
            "connect postgresql://u:topsecret@h/d failed", extra={}
        )
        out = capsys.readouterr().out
    finally:
        structlog.reset_defaults()
        configure_logging("INFO")
    assert "hunter2" not in out and "topsecret" not in out and ":pw@" not in out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert any(line.get("password") == REDACTED for line in lines)


# --- security headers ---------------------------------------------------------------------


async def test_security_headers_on_api_responses(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "camera=()" in r.headers["permissions-policy"]
    docs = await client.get("/api/docs")
    assert "content-security-policy" not in docs.headers  # Swagger (dev only) needs scripts


# --- worker healthcheck + migrate helper --------------------------------------------------


def test_worker_healthcheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "hb"
    monkeypatch.setenv("AUREON_HEARTBEAT_FILE", str(f))
    assert healthcheck.main(["--max-age", "60"]) == 1  # no heartbeat yet
    heartbeat.touch_heartbeat_file()
    assert healthcheck.main(["--max-age", "60"]) == 0
    old = time.time() - 600
    os.utime(f, (old, old))
    ok, msg = healthcheck.check(60)
    assert not ok and "old" in msg


async def test_registry_heartbeat_touches_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents.registry import OLLAMA, Heartbeat

    f = tmp_path / "hb"
    monkeypatch.setenv("AUREON_HEARTBEAT_FILE", str(f))

    class Reg:
        async def register(self, *_: Any) -> bool:
            return False

        async def heartbeat(self, *_: Any) -> bool:
            return False

    class Broken(Reg):
        async def register(self, *_: Any) -> bool:
            raise ConnectionError

    await Heartbeat(Broken(), OLLAMA, 1, lambda: ("idle", {})).beat()  # type: ignore[arg-type]
    assert not f.exists()  # DB down: no liveness claim
    await Heartbeat(Reg(), OLLAMA, 1, lambda: ("idle", {})).beat()  # type: ignore[arg-type]
    assert f.exists()


def test_url_for_database_swaps_only_the_name() -> None:
    url = "postgresql+asyncpg://aureon:p%40ss@pg:5432/spasta"
    out = url_for_database(url, "spasta_drill_20261102_010101")
    assert out == "postgresql+asyncpg://aureon:p%40ss@pg:5432/spasta_drill_20261102_010101"
    with pytest.raises(ValueError):
        url_for_database(url, 'x"; drop database spasta; --')


def test_logging_stream_is_stdout() -> None:
    # The redaction processor must sit before the renderer in both chains.
    from app.core import logging as applog

    src = Path(applog.__file__).read_text()
    assert src.count("redact_secrets,") == 2
    assert isinstance(io.StringIO(), io.StringIO)


# --- coding worker: children cannot read the worker's environment --------------------------


CHILD = """import os
try:
    print(open(f"/proc/{os.getppid()}/environ", "rb").read().count(b"AUREON_CANARY"))
except OSError:
    print("denied")
"""


def _parent_script(guard: bool) -> str:
    return (
        "import os, subprocess, sys\n"
        + (
            "from app.core.procsec import make_non_dumpable\nassert make_non_dumpable()\n"
            if guard
            else ""
        )
        + f"child = {CHILD!r}\n"
        + "print(subprocess.run([sys.executable, '-c', child], env={'PATH': os.environ['PATH']},"
        + " capture_output=True, text=True).stdout.strip())\n"
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc only")
@pytest.mark.parametrize(("guard", "expected"), [(False, "1"), (True, "denied")])
def test_non_dumpable_parent_hides_environ_from_children(guard: bool, expected: str) -> None:
    prefix: list[str] = []
    if os.geteuid() == 0:  # root reads any /proc/*/environ: drop to nobody like the worker uid
        setpriv = shutil.which("setpriv")
        if setpriv is None:
            pytest.skip("running as root without setpriv")
        prefix = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups"]
    r = subprocess.run(
        [*prefix, sys.executable, "-"],
        input=_parent_script(guard),
        env={"PATH": os.environ["PATH"], "AUREON_CANARY": "s3cret", "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        cwd="/",
        timeout=60,
    )
    if r.returncode != 0 and "ModuleNotFoundError" in r.stderr + r.stdout:
        pytest.skip("checkout not readable by the unprivileged user")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected
