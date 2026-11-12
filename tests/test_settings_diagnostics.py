"""Settings diagnostics (app_settings.diagnose): every layer, masks only, no database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import Settings
from app.models.app_settings import AppSetting
from app.services import app_settings as svc

KEY_A = "sk-ant-api03-" + "a" * 40 + "AAAA"
KEY_B = "sk-ant-api03-" + "b" * 40 + "BBBB"


@pytest.fixture
def env(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"SETTINGS_MASTER_KEY": SecretStr(Fernet.generate_key().decode())}
    )


def _row(env: Settings, key: str, value: str, master: str | None = None) -> AppSetting:
    cipher = svc.Cipher(master or env.SETTINGS_MASTER_KEY)
    return AppSetting(
        key=key,
        value_encrypted=cipher.encrypt(value),
        is_secret=True,
        category="ai",
        version=2,
        updated_at=datetime(2026, 11, 12, tzinfo=UTC),
    )


def _key(d: svc.Diagnosis, key: str) -> svc.KeyReport:
    return next(k for k in d.keys if k.key == key)


def _all_text(d: svc.Diagnosis) -> str:
    return repr(d)


def test_db_value_in_effect_and_live(env: Settings) -> None:
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_A)]
    live = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})
    d = svc.diagnose(env, live, rows)
    k = _key(d, "ANTHROPIC_API_KEY")
    assert d.encryption_ready and d.overlay_ok and not d.stale
    assert (k.db_present, k.db_masked, k.expected_source) == (True, "••••AAAA", "db")
    assert k.live_masked == "••••AAAA" and k.issues == ()
    assert KEY_A not in _all_text(d)


def test_stale_when_the_process_runs_an_older_value(env: Settings) -> None:
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_B)]
    live = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})  # saved B, still running A
    d = svc.diagnose(env, live, rows)
    k = _key(d, "ANTHROPIC_API_KEY")
    assert "stale" in k.issues and "ANTHROPIC_API_KEY" in d.stale
    assert (k.expected_masked, k.live_masked) == ("••••BBBB", "••••AAAA")


def test_row_from_another_master_key_is_undecryptable(env: Settings) -> None:
    other = Fernet.generate_key().decode()
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_A, master=other)]
    d = svc.diagnose(env, env, rows)
    k = _key(d, "ANTHROPIC_API_KEY")
    assert d.undecryptable == ("ANTHROPIC_API_KEY",)
    assert k.db_present and k.db_masked is None and "db_undecryptable" in k.issues
    assert k.expected_source != "db"


def test_missing_master_key_reports_the_problem(env: Settings) -> None:
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_A)]
    no_key = env.model_copy(update={"SETTINGS_MASTER_KEY": None})
    d = svc.diagnose(no_key, no_key, rows)
    assert not d.encryption_ready and d.encryption_problem and d.master_keys == 0
    assert d.undecryptable == ("ANTHROPIC_API_KEY",)


def test_invalid_db_value_is_named_and_ignored(env: Settings) -> None:
    rows = [_row(env, "AI_MAX_STEPS", "not-a-number"), _row(env, "ANTHROPIC_API_KEY", KEY_A)]
    d = svc.diagnose(env, env, rows)
    assert d.rejected == ("AI_MAX_STEPS",) and not d.overlay_ok
    assert "AI_MAX_STEPS" in d.overlay_fields
    assert "db_rejected" in _key(d, "AI_MAX_STEPS").issues
    # One bad row drops every DB value: the key row is ignored too.
    assert "db_ignored" in _key(d, "ANTHROPIC_API_KEY").issues
    # Not a secret: shown in full, so the bad value can be read and fixed.
    assert _key(d, "AI_MAX_STEPS").db_value == "not-a-number"


def test_env_shadowed_and_unknown_rows(env: Settings) -> None:
    with_env = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_B), _row(env, "GEMINI_API_KEY", "old-key-value")]
    live = svc.overlay(with_env, {"ANTHROPIC_API_KEY": KEY_B})
    d = svc.diagnose(with_env, live, rows)
    k = _key(d, "ANTHROPIC_API_KEY")
    assert "shadows_env" in k.issues and k.env_masked == "••••AAAA"
    assert d.unknown_rows == ("GEMINI_API_KEY",) and d.rows == 2
    assert "old-key-value" not in _all_text(d)


def test_non_secret_values_in_full_secrets_never(env: Settings) -> None:
    rows = [_row(env, "AI_MODEL_MAIN", "claude-sonnet-5-5"), _row(env, "ANTHROPIC_API_KEY", KEY_A)]
    live = svc.overlay(env, {"AI_MODEL_MAIN": "claude-sonnet-5-5", "ANTHROPIC_API_KEY": KEY_A})
    d = svc.diagnose(env, live, rows)
    model = _key(d, "AI_MODEL_MAIN")
    assert (model.env_value, model.db_value) == ("claude-opus-5-5", "claude-sonnet-5-5")
    assert model.expected_value == model.live_value == "claude-sonnet-5-5"
    key = _key(d, "ANTHROPIC_API_KEY")
    assert (key.env_value, key.db_value, key.expected_value, key.live_value) == (None,) * 4
    assert KEY_A not in _all_text(d)


def test_layer_values_are_full_for_the_reveal(env: Settings) -> None:
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_B)]
    live = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})
    v = svc.layer_values(env, live, rows, "ANTHROPIC_API_KEY")
    assert (v.env, v.db, v.db_present, v.expected, v.live) == (None, KEY_B, True, KEY_B, KEY_A)


def test_processes_report_what_they_loaded(env: Settings) -> None:
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_B)]
    now = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_B})
    old = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})
    research = svc.applied_snapshot(old, "research", ("ANTHROPIC_", "AI_"))
    dhan = svc.applied_snapshot(old, "dhan_feed", ("DHAN_",))
    api = svc.applied_snapshot(now, "api", ("",))
    d = svc.diagnose(env, now, rows, [research, dhan, api])
    by = {p.process: p for p in d.processes}
    assert by["research"].stale == ("ANTHROPIC_API_KEY",)
    # The feed holds the old key too, but never uses it live: noted, not a problem.
    assert by["dhan_feed"].stale == () and by["dhan_feed"].unwatched_stale == ("ANTHROPIC_API_KEY",)
    assert by["api"].stale == () and by["api"].unwatched_stale == ()
    # Reports carry keyed digests, never values.
    assert KEY_A not in repr([research, dhan, api]) and KEY_B not in repr([research, dhan, api])


def test_digest_is_keyed_and_stable(env: Settings) -> None:
    a = svc.digest(env, "ANTHROPIC_API_KEY", KEY_A)
    assert a == svc.digest(env, "ANTHROPIC_API_KEY", KEY_A) and a != svc.digest(env, "X", KEY_A)
    other = env.model_copy(update={"SECRET_KEY": SecretStr("another-secret-key-" * 3)})
    assert a != svc.digest(other, "ANTHROPIC_API_KEY", KEY_A)
    assert svc.digest(env, "ANTHROPIC_API_KEY", None) is None


def test_a_report_keeps_its_load_time(env: Settings) -> None:
    loaded = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
    snap = svc.applied_snapshot(env, "scheduler", ("DHAN_",), loaded)
    assert snap["applied_at"] == loaded.isoformat()
    # A replaced container's report must expire soon after it stops re-reporting.
    assert svc.REPORT_EVERY_SECONDS * 2 < svc.APPLIED_TTL <= 3600


async def test_keep_reporting_repeats_until_stopped() -> None:
    import asyncio

    stop = asyncio.Event()
    sent: list[int] = []

    async def send() -> None:
        sent.append(1)
        if len(sent) == 3:
            stop.set()
        if len(sent) == 2:
            raise RuntimeError("redis down")  # one failed report never ends the loop

    await asyncio.wait_for(svc.keep_reporting(send, stop, every=0.01), timeout=2)
    assert len(sent) == 3
