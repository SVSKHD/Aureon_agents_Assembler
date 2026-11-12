"""Dhan monitoring, bounded recovery, alerts and trading-safety invariants (docs/28, docs/29).

Unit level (no Postgres): the monitor's checks, which incidents are recoverable and by what,
the recovery action's outcomes (including cooldown and no listener), alert delivery failure,
and static guarantees that no AI/agent/recovery code can reach LIVE trading or risk limits.
The Postgres flows (incidents opened/resolved, retry exhaustion) are in
tests/test_dhan_operations_db.py.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.repairs import INCIDENT_SOURCES, Incident
from app.services import alerts, approvals, dhan_monitor, repair_recovery
from app.services import dhan_connection as conn
from app.services.alerts import Alert
from app.workers.scheduler import TASKS, default_jobs
from tests.fake_redis import FakeRedis

CLIENT = "1100003626"
TOKEN = "manual-token-" + "a" * 40
OPEN = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "DHAN_CLIENT_ID": CLIENT,
            "DHAN_ACCESS_TOKEN": SecretStr(TOKEN),
            "DHAN_FEED_INSTRUMENTS": ["NSE_EQ:3499"],
        }
    )


def connection(status: str = "connected", **kw: Any) -> dict[str, Any]:
    return {"status": status, "consecutive_failures": 0, "last_check_outcome": "ok"} | kw


def health(*codes: str, status: str = "ok") -> dict[str, Any]:
    return {
        "status": status if not codes else "down",
        "codes": list(codes),
        "problems": [f"problem {c}" for c in codes],
        "market": {"open": True, "session": "open"},
    }


def failing(checks: list[dhan_monitor.Check]) -> dict[str, dhan_monitor.Check]:
    return {c.name: c for c in checks if not c.ok}


# --- the monitor's checks -----------------------------------------------------------------


def test_all_healthy_means_nothing_failing(s: Settings) -> None:
    checks = dhan_monitor.evaluate(s, connection(), health(), True, OPEN)
    assert failing(checks) == {}
    names = {c.name for c in checks}
    assert {"auth", "auth_unverified", "redis", "feed_disconnected", "feed_not_running"} <= names


@pytest.mark.parametrize("status", ["expired", "rejected", "mismatch", "disconnected"])
def test_unusable_credential_is_never_auto_recovered(s: Settings, status: str) -> None:
    out = failing(dhan_monitor.evaluate(s, connection(status), health(), True, OPEN))
    auth = out["auth"]
    assert auth.severity == "error" and not auth.recoverable
    assert auth.details["renewal"] == "manual" and "log in" in auth.details["action"]
    assert "automatic login is off" in auth.details["note"]
    inc = Incident(source="dhan", details={**auth.details, "recoverable": False})
    assert repair_recovery.plan(inc) == ()  # -> escalates to a person straight away


def test_expired_session_without_manual_token_is_caught_locally(s: Settings) -> None:
    gone = s.model_copy(
        update={
            "DHAN_ACCESS_TOKEN": None,
            "DHAN_SESSION_TOKEN": SecretStr("x" * 40),
            "DHAN_SESSION_EXPIRES_AT": OPEN - timedelta(minutes=1),
        }
    )
    # dhan.connections still says connected (the check job has not run yet).
    out = failing(dhan_monitor.evaluate(gone, connection(), health(), True, OPEN))
    assert out["auth"].details["status"] == "expired"


def test_dhan_unreachable_for_checks_warns_after_three(s: Settings) -> None:
    c = connection("connected", last_check_outcome="unavailable", consecutive_failures=2)
    assert "auth_unverified" not in failing(dhan_monitor.evaluate(s, c, health(), True, OPEN))
    c["consecutive_failures"] = 3
    out = failing(dhan_monitor.evaluate(s, c, health(), True, OPEN))
    assert out["auth_unverified"].severity == "warning"


def test_feed_problems_map_to_incidents_and_plans(s: Settings) -> None:
    out = failing(
        dhan_monitor.evaluate(
            s, connection(), health("disconnected", "redis_writes_failing"), True, OPEN
        )
    )
    disc, red = out["feed_disconnected"], out["feed_redis_writes_failing"]
    assert disc.recoverable and not red.recoverable
    assert disc.details["kind"] == "market_data" and disc.details["agent_id"] == "dhan-feed"
    assert repair_recovery.plan(Incident(source="dhan", details=disc.details)) == (
        "reconnect_dhan_feed",
    )
    assert repair_recovery.plan(Incident(source="dhan", details=red.details)) == ()
    nr = failing(dhan_monitor.evaluate(s, connection(), health("not_running"), True, OPEN))
    assert "COMPOSE_PROFILES" in nr["feed_not_running"].details["action"]


def test_redis_down_is_its_own_check(s: Settings) -> None:
    out = failing(dhan_monitor.evaluate(s, connection(), health(), False, OPEN))
    assert out["redis"].severity == "error" and not out["redis"].recoverable


def test_unconfigured_dhan_has_no_checks(s: Settings) -> None:
    assert dhan_monitor.evaluate(s.model_copy(update={"DHAN_CLIENT_ID": None}), connection(),
                                 health(), True, OPEN) == []  # fmt: skip
    no_feed = s.model_copy(update={"DHAN_FEED_INSTRUMENTS": []})
    names = {c.name for c in dhan_monitor.evaluate(no_feed, connection(), health(), True, OPEN)}
    assert not any(n.startswith("feed_") for n in names)


def test_fingerprints_are_stable_and_per_check(s: Settings) -> None:
    a = dhan_monitor.Check("feed_disconnected", False)
    assert a.fingerprint(CLIENT) == dhan_monitor.Check("feed_disconnected", False).fingerprint(
        CLIENT
    )
    assert a.fingerprint(CLIENT) != dhan_monitor.Check("auth", False).fingerprint(CLIENT)
    assert "dhan" in INCIDENT_SOURCES


# --- bounded recovery ----------------------------------------------------------------------


def deps(s: Settings, redis: Any) -> repair_recovery.Deps:
    return repair_recovery.Deps(maker=None, settings=s, redis=redis)  # type: ignore[arg-type]


def feed_incident() -> Incident:
    return Incident(
        id=uuid.uuid4(),
        source="dhan",
        details={"check": "feed_disconnected", "agent_id": "dhan-feed"},
    )


async def test_reconnect_action_outcomes(s: Settings) -> None:
    r = FakeRedis()
    inc = feed_incident()
    # Nobody listening (the dhan profile is not running): failed, not "done".
    res = await repair_recovery.reconnect_dhan_feed(deps(s, r), inc)
    assert res.outcome == "failed" and "no dhan-feed worker" in res.detail
    # Cooldown shared with the admin button: a second attempt is skipped, not repeated.
    res = await repair_recovery.reconnect_dhan_feed(deps(s, r), inc)
    assert res.outcome == "skipped" and "cooldown" in res.detail
    await r.delete("dhan:feed:reconnect-cooldown")
    sub = r.pubsub()
    await sub.subscribe("dhan:feed:control")
    res = await repair_recovery.reconnect_dhan_feed(deps(s, r), inc)
    assert res.outcome == "done"
    msg = await sub.get_message(timeout=0.1)
    assert msg is not None and b'"cmd": "reconnect"' in msg["data"]
    assert str(inc.id).encode() in msg["data"]  # traceable to the incident
    r.fail = True
    res = await repair_recovery.reconnect_dhan_feed(deps(s, r), inc)
    assert res.outcome == "failed"


def test_market_data_recovery_is_separate_from_trading() -> None:
    """Every action a Dhan incident can run is a market-data action, and the market-data set
    is exactly the feed reconnect. Trading agents are still never restarted."""
    assert repair_recovery.MARKET_DATA_ACTIONS == frozenset({"reconnect_dhan_feed"})
    for names in repair_recovery.DHAN_PLANS.values():
        assert set(names) <= repair_recovery.MARKET_DATA_ACTIONS
    assert set(repair_recovery.DHAN_PLANS) == {f"feed_{c}" for c in dhan_monitor.RECOVERABLE}
    assert "dhan" in repair_recovery.NEVER_RESTART_PREFIXES
    for check in ("auth", "auth_unverified", "redis", "feed_auth_failed", "feed_not_running"):
        assert repair_recovery.plan(Incident(source="dhan", details={"check": check})) == ()
    # A crafted heartbeat incident for the feed still cannot restart it.
    hb = Incident(source="heartbeat", details={"agent_id": "dhan-feed", "kind": "trading"})
    assert repair_recovery.plan(hb) == ("restart_agent", "clear_queue_lock")


async def test_restart_agent_refuses_the_feed(s: Settings) -> None:
    hb = Incident(source="heartbeat", details={"agent_id": "dhan-feed", "kind": "trading"})
    res = await repair_recovery.restart_agent(deps(s, None), hb)
    assert res.outcome == "manual"


# --- alerts --------------------------------------------------------------------------------


async def test_alert_delivery_failure_is_retried_next_run(
    s: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooked = s.model_copy(
        update={"DISCORD_WEBHOOK_URL": SecretStr("https://discord.com/api/webhooks/1/x")}
    )
    alert = Alert("incident", "fp-dhan-feed", "error", "Incident (dhan): feed down", "", "/x")

    async def collect(*a: Any, **kw: Any) -> tuple[list[Alert], dict[str, Any]]:
        return [alert], {}

    monkeypatch.setattr(alerts, "collect", collect)
    sent: list[str] = []

    async def down(settings: Settings, a: Any) -> bool:
        raise ConnectionError("discord unreachable")

    async def up(settings: Settings, a: Any) -> bool:
        sent.append(a["title"])
        return True

    dedup = alerts.Dedup(None, 600)
    out = await alerts.dispatch(None, hooked, None, sender=down, dedup=dedup)  # type: ignore[arg-type]
    assert out["failed"] == 1 and out["sent"] == 0
    out = await alerts.dispatch(None, hooked, None, sender=up, dedup=dedup)  # type: ignore[arg-type]
    assert out["sent"] == 1 and sent == ["Incident (dhan): feed down"]
    out = await alerts.dispatch(None, hooked, None, sender=up, dedup=dedup)  # type: ignore[arg-type]
    assert out["deduplicated"] == 1  # no repeat inside the window


def test_expired_credential_alerts_once() -> None:
    expired = Incident(source="dhan", details={"check": "auth", "status": "expired"})
    rejected = Incident(source="dhan", details={"check": "auth", "status": "rejected"})
    assert alerts._covered_by_session_alert(expired)
    assert not alerts._covered_by_session_alert(rejected)


# --- scheduler wiring ------------------------------------------------------------------------


def test_monitoring_jobs_exist_when_a_client_id_is_set(s: Settings) -> None:
    ids = {j[0] for j in default_jobs(s)}
    assert {"dhan-session-check", "dhan-monitor", "dhan-reconcile"} <= ids
    # No usable token: verification and monitoring still run (that is when they matter).
    dead = s.model_copy(update={"DHAN_ACCESS_TOKEN": None})
    ids = {j[0] for j in default_jobs(dead)}
    assert {"dhan-session-check", "dhan-monitor"} <= ids and "dhan-reconcile" not in ids
    assert not {j[0] for j in default_jobs(s.model_copy(update={"DHAN_CLIENT_ID": None}))} & {
        "dhan-session-check",
        "dhan-monitor",
    }
    assert "dhan_session_check" in TASKS and "dhan_monitor" in TASKS


# --- trading safety ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["trading.live_order", "trading.order.place", "trading.mode.live", "trading.risk.update",
     "broker.dhan.order", "dhan.order", "mt5.order.send", "Trading.Live"],
)  # fmt: skip
def test_no_executor_can_be_registered_for_live_actions(kind: str) -> None:
    async def run(*a: Any) -> dict[str, Any]:
        return {}

    reg = approvals.ExecutorRegistry()
    with pytest.raises(approvals.ForbiddenApprovalKind):
        reg.register(approvals.ExecutorSpec(kind=kind, execute=run))  # type: ignore[arg-type]
    assert reg.get(kind) is None


def test_existing_executors_are_unaffected() -> None:
    from app.api.v1 import paper  # noqa: F401 - registers trading.paper_intent
    from app.services import github_actions, github_ops  # noqa: F401

    kinds = approvals.REGISTRY.kinds()
    assert "trading.paper_intent" in kinds
    for k in kinds:
        approvals.assert_kind_allowed(k)


LIVE_NAMES = {"issue_live_token", "check_mode_change", "put_risk_policy", "change_mode"}
ALLOWED_LIVE = {ROOT / "app/services/trading.py", ROOT / "app/api/v1/trading.py"}


def _py(*dirs: str) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        p = ROOT / d
        out += [p] if p.is_file() else sorted(p.rglob("*.py"))
    return out


def test_only_the_admin_trading_api_can_reach_live_mode_or_risk_limits() -> None:
    """No agent, AI service, worker, monitor or recovery module mentions the LIVE-token /
    mode-change / risk-policy functions, constructs a RiskPolicy, or writes `.mode`."""
    offenders: list[str] = []
    for path in _py("app"):
        if path in ALLOWED_LIVE or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.id if isinstance(node, ast.Name)
                else node.attr if isinstance(node, ast.Attribute)
                else node.name if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                else None
            )  # fmt: skip
            if name in LIVE_NAMES:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")  # type: ignore[attr-defined]
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RiskPolicy":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} RiskPolicy()")
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
                "update", "insert"
            ) and any(getattr(a, "id", None) in ("RiskPolicy", "BrokerAccount")
                      for a in node.args):  # fmt: skip
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} write")
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    # `self.mode` is an object's own field (e.g. DhanFeed's subscription mode);
                    # an account's execution mode is written through another name.
                    if (
                        isinstance(t, ast.Attribute)
                        and t.attr in ("mode", "mode_changed_by")
                        and getattr(t.value, "id", None) != "self"
                    ):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} .{t.attr}=")
    assert offenders == [], offenders


def test_ai_and_agent_code_cannot_approve_anything() -> None:
    """Approving needs ApprovalGate.approve(…, admin: User, reauth_password) from an admin
    session; no AI service, agent or worker calls it."""
    for path in _py("app/agents", "app/workers", "app/services"):
        if path.name == "approvals.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert ".approve(" not in text, path


def test_recovery_and_monitor_import_nothing_from_trading() -> None:
    for name in ("repair_recovery.py", "dhan_monitor.py", "dhan_feed_control.py",
                 "dhan_connection.py", "dhan_feed_health.py"):  # fmt: skip
        tree = ast.parse((ROOT / "app/services" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in ("app.services.trading", "app.services.paper"), name
                assert not node.module.startswith("app.api"), name


def test_live_flag_stays_off_by_default(settings: Settings) -> None:
    assert settings.TRADING_LIVE_ENABLED is False
    assert conn.ACTIONS["expired"] and "log in" in conn.ACTIONS["expired"]
