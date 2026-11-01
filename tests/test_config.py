from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

BASE = {"DATABASE_URL": "postgresql+asyncpg://u:p@db/spasta", "SECRET_KEY": "s" * 32}


def test_cors_origins_parsed_from_csv() -> None:
    s = Settings(**BASE, CORS_ORIGINS="http://a, https://b ,")
    assert s.CORS_ORIGINS == ["http://a", "https://b"]


def test_short_secret_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "SECRET_KEY": "short"})


def test_secrets_not_in_repr() -> None:
    s = Settings(**BASE)
    assert "u:p@" not in repr(s)
    assert "s" * 32 not in repr(s)
