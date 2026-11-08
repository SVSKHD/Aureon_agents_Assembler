"""V1.6.1 regression tests that need no database: A5 trade-state transitions, A4 migration
state classification, A6 alert delivery leases (in-process backend)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect

from app.core.config import Settings
from app.models.trading import OrderIntent
from app.services import alerts, paper
from app.workers import migrate

# --- A5: the intent state machine -----------------------------------------------------------

ALL = ("PENDING_RISK", "REJECTED", "APPROVED", "SUBMITTED", "FILLED", "CANCELLED", "EXPIRED")
LEGAL = {
    ("PENDING_RISK", "REJECTED"),
    ("PENDING_RISK", "APPROVED"),
    ("PENDING_RISK", "CANCELLED"),
    ("PENDING_RISK", "EXPIRED"),
    ("APPROVED", "SUBMITTED"),
    ("APPROVED", "FILLED"),
    ("APPROVED", "CANCELLED"),
    ("APPROVED", "EXPIRED"),
    ("SUBMITTED", "FILLED"),
    ("SUBMITTED", "CANCELLED"),
    ("SUBMITTED", "EXPIRED"),
}


def _intent(status: str) -> OrderIntent:
    return OrderIntent(id=uuid.uuid4(), status=status, correlation_id="c", mode="PAPER")


@pytest.mark.parametrize("old", ALL)
@pytest.mark.parametrize("new", ALL)
def test_transition_allows_exactly_the_documented_moves(old: str, new: str) -> None:
    intent = _intent(old)
    if (old, new) in LEGAL:
        paper.transition(intent, new)
        assert intent.status == new
    else:
        with pytest.raises(paper.IntentRefused) as exc:
            paper.transition(intent, new)
        assert exc.value.code == "invalid_transition" and exc.value.status == 409
        assert intent.status == old  # unchanged


def test_terminal_states_have_no_way_out() -> None:
    for s in paper.TERMINAL_STATUSES:
        assert paper.TRANSITIONS[s] == frozenset()
    assert set(paper.TRANSITIONS) == set(ALL)
    assert set(paper.OPEN_STATUSES) | set(paper.TERMINAL_STATUSES) == set(ALL)


def test_status_is_the_compare_and_set_column() -> None:
    """Every ORM UPDATE of an intent is `... WHERE status = <read status>` (no new column)."""
    mapper = sa_inspect(OrderIntent)
    assert mapper.version_id_col is OrderIntent.__table__.c.status
    assert mapper.version_id_generator is False


def test_no_direct_status_writes_left_in_paper_service() -> None:
    import inspect
    import re

    src = inspect.getsource(paper)
    writes = re.findall(r"intent\.status = ", src)
    assert len(writes) == 1, "every status change must go through paper.transition()"


# --- A4: migration state --------------------------------------------------------------------


def _head() -> str:
    (head,) = migrate.classify(None)["heads"]
    return str(head)


def test_classify_at_head() -> None:
    r = migrate.classify([_head()])
    assert r["state"] == "at_head" and r["pending"] == [] and r["unknown"] == []


def test_classify_never_migrated_lists_every_revision_oldest_first() -> None:
    r = migrate.classify(None)
    assert r["state"] == "pending"
    assert r["pending"][0] == "0001" and r["pending"][-1] == _head()
    assert r["pending"] == sorted(r["pending"])


def test_classify_behind_lists_only_the_missing_revisions() -> None:
    head = _head()
    prev = f"{int(head) - 2:04d}"
    r = migrate.classify([prev])
    assert r["state"] == "pending"
    assert r["pending"] == [f"{int(head) - 1:04d}", head]


def test_classify_unknown_revision_is_diverged_not_pending() -> None:
    r = migrate.classify(["ffff"])
    assert r["state"] == "diverged" and r["unknown"] == ["ffff"] and r["pending"] == []


def test_check_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    async def at(rev: list[str] | None) -> Any:
        return rev

    cases = [([_head()], 0, "at_head"), (["0001"], 3, "pending"), (None, 3, "pending"),
             (["zzzz"], 4, "diverged")]  # fmt: skip
    for applied, code, state in cases:
        monkeypatch.setattr(migrate, "read_applied", lambda url, a=applied: at(a))
        assert migrate.check("postgresql+asyncpg://x/y") == code
        out = capsys.readouterr().out
        assert migrate.CHECK_MARKER in out and f'"state": "{state}"' in out

    async def down(url: str) -> Any:
        raise OSError("connection refused to secret-host")

    monkeypatch.setattr(migrate, "read_applied", down)
    assert migrate.check("postgresql+asyncpg://x/y") == 1
    out = capsys.readouterr().out
    assert '"state": "unreachable"' in out and "secret-host" not in out


def test_migrate_fails_when_upgrade_leaves_the_schema_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`alembic upgrade head` exiting 0 is not enough: the recorded state must be head."""

    async def ready(url: str, wait: float, interval: float = 3.0) -> bool:
        return True

    async def behind(url: str) -> list[str]:
        return ["0001"]

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@127.0.0.1:1/db")
    monkeypatch.setattr(migrate, "wait_for_postgres", ready)
    monkeypatch.setattr(migrate, "alembic", lambda url, *a: 0)
    monkeypatch.setattr(migrate, "read_applied", behind)
    assert migrate.main([]) == migrate.CHECK_PENDING

    async def at_head(url: str) -> list[str]:
        return [_head()]

    monkeypatch.setattr(migrate, "read_applied", at_head)
    assert migrate.main([]) == 0
    monkeypatch.setattr(migrate, "alembic", lambda url, *a: 1)
    assert migrate.main([]) == 1  # a failed upgrade is reported as such


# --- A6: alert delivery leases (in-process backend; the Redis backend is in the db tests) --


def test_lease_covers_every_discord_attempt(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "DISCORD_MAX_ATTEMPTS": 3,
            "DISCORD_REQUEST_SECONDS": 5.0,
            "DISCORD_RETRY_BASE_SECONDS": 1.0,
        }
    )
    assert alerts.lease_seconds(s) == 3 * 5 + (1 + 2) + 30
    assert alerts.lease_seconds(s) < s.ALERTS_DEDUP_MINUTES * 60


async def test_local_lease_fencing() -> None:
    d = alerts.Dedup(None, window_s=600, lease_s=1)
    a = await d.claim("fp")
    assert a is not None and await d.claim("fp") is None  # leased
    await asyncio.sleep(1.05)  # worker A hung: its lease expires
    b = await d.claim("fp")
    assert b is not None and b != a
    await d.release("fp", a)  # stale A cannot drop B's lease...
    assert await d.claim("fp") is None
    assert await d.commit("fp", a) is False  # ...nor commit over it
    assert await d.commit("fp", b) is True
    assert await d.claim("fp") is None  # sent: deduplicated for the window


class Hang:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, settings: Settings, alert: Any) -> bool:
        self.calls += 1
        await asyncio.sleep(3600)
        return True


async def test_cancelled_send_gives_the_lease_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler's timeout cancels a hung Discord call: the alert must be retried on the
    next run, not silenced for ALERTS_DEDUP_MINUTES (the pre-1.6.1 behaviour)."""
    alert = alerts.Alert("incident", "x-1", "error", "DB slow", "detail", "/dev")

    async def collect(maker: Any, s: Any, now: Any = None) -> Any:
        return [alert], {}

    monkeypatch.setattr(alerts, "collect", collect)
    s = Settings.model_validate(
        {**settings.model_dump(), "DISCORD_WEBHOOK_URL": "https://discord.test/api/webhooks/1/x"}
    )
    d = alerts.Dedup(None, window_s=3600, lease_s=600)
    hang = Hang()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await alerts.dispatch(None, s, None, sender=hang, dedup=d)  # type: ignore[arg-type]
    assert hang.calls == 1

    sent: list[Any] = []

    async def ok(settings: Settings, a: Any) -> bool:
        sent.append(a)
        return True

    out = await alerts.dispatch(None, s, None, sender=ok, dedup=d)  # type: ignore[arg-type]
    assert out["sent"] == 1 and len(sent) == 1
    out = await alerts.dispatch(None, s, None, sender=ok, dedup=d)  # type: ignore[arg-type]
    assert out["deduplicated"] == 1 and len(sent) == 1  # idempotent once delivered
