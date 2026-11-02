"""Phase 18 unit tests (no DB): hard blocks, patch checks, exception capture, config."""

from __future__ import annotations

import pytest

from app.agents.coding import patch as patches
from app.agents.coding.git import REPAIR_BRANCH, TASK_BRANCH
from app.agents.coding.paths import PathRejected, normalize
from app.core.config import Settings
from app.core.exception_sink import ExceptionSink, capture_exceptions, describe
from app.services import repairs
from app.workers.scheduler import TASKS, default_jobs


@pytest.mark.parametrize(
    "path",
    [
        "app/agents/trading/dhan/adapter.py",
        "app/services/trading.py",
        "app/services/trading_stream.py",
        "app/services/paper.py",
        "app/services/research.py",
        "app/services/risk_policy.py",
        "app/agents/trading/risk/limits.py",
        "app/auth/security.py",
        "app/auth/deps.py",
        "app/services/auth_extra.py",
        "app/core/security.py",
        ".env",
        ".env.prod",
        "deploy/.env.local",
        "certs/server.pem",
        "app/core/api_keys.py",
        "config/private.key",
        "docker-compose.yml",
        "compose.yaml",
        "Dockerfile",
        "docker/worker.Dockerfile",
        "deploy/install.sh",
        "scripts/ops/backup.sh",
        "alembic/versions/20261102_0015_x.py",
        "alembic.ini",
        ".github/workflows/ci.yml",
        "app/services/repairs.py",
        "app/services/repair_recovery.py",
        "app/services/incidents.py",
        "app/agents/coding/paths.py",
        "APP/SERVICES/TRADING.PY",
    ],
)
def test_hard_block_paths(path: str) -> None:
    assert repairs.hard_block(path) is not None, path


@pytest.mark.parametrize(
    "path",
    ["app/services/finance.py", "app/api/v1/health.py", "tests/test_x.py", ".env.example"],
)
def test_allowed_paths(path: str) -> None:
    assert repairs.hard_block(path) is None


def _patch(file: str, removed: str = "a = 1", added: str = "a = 2") -> patches.Patch:
    text = (
        f"diff --git a/{file} b/{file}\n--- a/{file}\n+++ b/{file}\n"
        f"@@ -1 +1 @@\n-{removed}\n+{added}\n"
    )
    return patches.validate(text, 100_000)


def test_patch_block_after_model() -> None:
    assert repairs.patch_block(_patch("app/services/finance.py")) is None
    assert "trading" in (repairs.patch_block(_patch("app/agents/trading/x.py")) or "")
    # settings: a secret field may never be changed, other settings may
    assert repairs.patch_block(_patch("app/core/config.py", "X: int = 1", "X: int = 2")) is None
    why = repairs.patch_block(
        _patch("app/core/config.py", "GITHUB_TOKEN: SecretStr | None = None", "GITHUB_TOKEN = 1")
    )
    assert why and "secret fields" in why
    why = repairs.patch_block(_patch("app/x.py", "t = None", "t = 'ghp_" + "a" * 36 + "'"))
    assert why and "secret-looking" in why
    # the coding validator already refuses .env before we ever see it
    with pytest.raises(patches.PatchRejected):
        _patch(".env")


def test_repair_branch_names() -> None:
    import uuid

    b = repairs.repair_branch(uuid.uuid4())
    assert REPAIR_BRANCH.match(b) and not TASK_BRANCH.match(b)
    assert not REPAIR_BRANCH.match("repair/../../main")
    with pytest.raises(PathRejected):
        normalize("../etc/passwd")


def _raise_in_app() -> BaseException:
    try:
        normalize("/abs/path")  # raises inside app.agents.coding.paths
    except PathRejected as exc:
        return exc
    raise AssertionError("expected PathRejected")


def test_exception_fingerprint_and_location() -> None:
    a, b = describe(_raise_in_app()), describe(_raise_in_app())
    assert a.fingerprint == b.fingerprint
    assert a.details["location"]["file"] == "app/agents/coding/paths.py"
    assert a.details["location"]["function"] == "normalize"
    assert a.details["self_origin"] is False
    assert a.title.startswith("PathRejected in app.agents.coding.paths:normalize")
    other = describe(ValueError("x"))
    assert other.fingerprint != a.fingerprint and "location" not in other.details


def test_exception_message_is_redacted() -> None:
    cap = describe(RuntimeError("failed with token ghp_" + "Z" * 30 + " at postgres://u:pw@h/db"))
    assert "ghp_" not in cap.details["message"] and "pw@" not in cap.details["message"]


def test_sink_rate_limit_and_cap() -> None:
    sink = ExceptionSink(max_entries=2)
    for _ in range(3):
        sink.add(_raise_in_app())
    due = sink.due(60, now=1000.0)
    assert len(due) == 1 and due[0].count == 3 and due[0].pending == 3
    sink.add(_raise_in_app())
    assert sink.due(60, now=1030.0) == []  # rate-limited: counted, not written
    later = sink.due(60, now=1061.0)
    assert len(later) == 1 and later[0].pending == 1 and later[0].count == 4
    sink.requeue(later)
    assert sink.due(60, now=1062.0)[0].pending == 1  # a failed flush is retried
    sink.add(ValueError("a"))
    sink.add(KeyError("b"))  # over max_entries: dropped, counted
    assert sink.dropped == 1


def test_processor_captures_exc_info() -> None:
    from app.core.exception_sink import SINK

    SINK.clear()
    try:
        normalize("/x")
    except PathRejected:
        capture_exceptions(None, "error", {"event": "boom", "exc_info": True})
    assert len(SINK.entries) == 1
    capture_exceptions(None, "info", {"event": "fine"})
    assert len(SINK.entries) == 1
    SINK.clear()


def test_disabled_reason(settings: Settings) -> None:
    assert "REPAIR_MODEL" in (repairs.disabled_reason(settings) or "")
    s = settings.model_copy(update={"REPAIR_MODEL": "m", "REPAIR_REPO": "x"})
    assert "REPAIR_REPO" in (repairs.disabled_reason(s) or "")


def test_incident_scan_is_scheduled(settings: Settings) -> None:
    assert "incident_scan" in TASKS
    assert ("incident-scan", "incident_scan", "* * * * *") in default_jobs(settings)
    off = settings.model_copy(update={"INCIDENT_SCAN_CRON": ""})
    assert all(j[1] != "incident_scan" for j in default_jobs(off))
