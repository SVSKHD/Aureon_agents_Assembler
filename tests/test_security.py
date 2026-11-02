from __future__ import annotations

from app.auth import security


def test_argon2id_hash_and_verify() -> None:
    h = security.hash_password("a long enough password")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "a long enough password")
    assert not security.verify_password(h, "wrong")
    assert not security.verify_password("not-a-hash", "x")


def test_session_tokens_unique_and_hashed() -> None:
    a, b = security.new_session_token(), security.new_session_token()
    assert a != b and len(a) >= 43
    assert len(security.token_hash(a)) == 32


def test_csrf_token_bound_to_session_and_key() -> None:
    t = security.new_session_token()
    k = "k" * 40
    assert security.csrf_token_for(t, k) == security.csrf_token_for(t, k)
    assert security.csrf_token_for(t, k) != security.csrf_token_for(t + "x", k)
    assert security.csrf_token_for(t, k) != security.csrf_token_for(t, "j" * 40)
