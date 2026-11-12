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
    assert "not-a-number" not in _all_text(d)


def test_env_shadowed_and_unknown_rows(env: Settings) -> None:
    with_env = svc.overlay(env, {"ANTHROPIC_API_KEY": KEY_A})
    rows = [_row(env, "ANTHROPIC_API_KEY", KEY_B), _row(env, "GEMINI_API_KEY", "old-key-value")]
    live = svc.overlay(with_env, {"ANTHROPIC_API_KEY": KEY_B})
    d = svc.diagnose(with_env, live, rows)
    k = _key(d, "ANTHROPIC_API_KEY")
    assert "shadows_env" in k.issues and k.env_masked == "••••AAAA"
    assert d.unknown_rows == ("GEMINI_API_KEY",) and d.rows == 2
    assert "old-key-value" not in _all_text(d)
