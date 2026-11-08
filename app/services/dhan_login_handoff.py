"""One-time Dhan login handoff that survives a Spasta re-login (docs/11-dhan.md).

Dhan's API-key login has no `state` parameter, and its redirect back to us is a cross-site
navigation, so the Spasta session cookies (SameSite=Strict) are not sent with it; the admin may
also have to sign in to Spasta again before the login can be finished, which gives them a NEW
session id. The pending login is therefore bound to the admin's USER id, not the session, and
carried by its own browser cookie:

    POST /start      new random state (256 bit). Redis `dhan:auth:handoff:<sha256(state)>`
                     = {user, exp} for CONSENT_TTL_S; the browser gets the state as an HttpOnly,
                     Secure, SameSite=Lax cookie scoped to /api/v1/dhan/auth (Lax: it rides the
                     top-level redirect back from Dhan; Strict would not).
    GET  /callback   public. Needs that cookie and a live record; stores Dhan's tokenId in the
                     record (Fernet-encrypted), once. The browser only learns "ready".
    POST /consume    admin + CSRF, unchanged. Atomically (one Lua script): the record must
                     exist, belong to THIS user and hold a tokenId; it is then deleted and a
                     "used" marker left. Another user is refused and the record stays for its
                     owner. Expired, replayed and unsolicited attempts each get their own code.

Nothing here logs or returns the state, the tokenId or the consent id. The state is stored only
as a hash; the tokenId only encrypted.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from app.core.config import Settings
from app.services import app_settings as svc

PREFIX = "dhan:auth:handoff:"
USED_PREFIX = "dhan:auth:handoff-used:"
STATE_BYTES = 32
# The cookie is scoped to the Dhan auth routes only.
COOKIE_PATH = "/api/v1/dhan/auth"

# KEYS[1] record, KEYS[2] used marker. ARGV[1] user id, ARGV[2] used-marker TTL.
# Returns the record JSON on success, or one of: "missing", "used", "other_user", "not_returned".
_CONSUME = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  if redis.call('EXISTS', KEYS[2]) == 1 then return 'used' end
  return 'missing'
end
local rec = cjson.decode(raw)
if rec['user'] ~= ARGV[1] then return 'other_user' end
if not rec['token'] then return 'not_returned' end
redis.call('DEL', KEYS[1])
redis.call('SET', KEYS[2], ARGV[1], 'EX', tonumber(ARGV[2]))
return raw
"""

# KEYS[1] record. ARGV[1] encrypted tokenId, ARGV[2] token fingerprint.
# Returns "ok", "same" (the same callback again), "missing" or "duplicate" (another tokenId).
_RETURN = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'missing' end
local rec = cjson.decode(raw)
if rec['token'] then
  if rec['token_fp'] == ARGV[2] then return 'same' end
  return 'duplicate'
end
rec['token'] = ARGV[1]
rec['token_fp'] = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(rec), 'KEEPTTL')
return 'ok'
"""


def cookie_name(settings: Settings) -> str:
    # Not __Host-: that prefix forbids a Path other than "/".
    return "__Secure-aureon_dhan_login" if settings.COOKIE_SECURE else "aureon_dhan_login"


def new_state() -> str:
    return secrets.token_urlsafe(STATE_BYTES)


def valid_state(state: str | None) -> bool:
    return (
        bool(state)
        and 32 <= len(state or "") <= 64
        and all(c.isalnum() or c in "-_" for c in state or "")
    )


def _key(state: str) -> str:
    return PREFIX + hashlib.sha256(state.encode()).hexdigest()


def _used_key(state: str) -> str:
    return USED_PREFIX + hashlib.sha256(state.encode()).hexdigest()


def _fp(token_id: str) -> str:
    return hashlib.sha256(token_id.encode()).hexdigest()[:32]


async def begin(redis: Redis, user_id: str, ttl_s: int) -> str:
    """A new pending login for this user; returns the state for the browser cookie."""
    state = new_state()
    rec = {"user": user_id, "exp": int(time.time()) + ttl_s}
    await redis.set(_key(state), json.dumps(rec), ex=ttl_s)
    return state


ReturnResult = Literal["ok", "same", "missing", "duplicate"]


async def record_return(
    redis: Redis, cipher: svc.Cipher, state: str, token_id: str
) -> ReturnResult:
    """Attach Dhan's tokenId to the pending login (first callback wins)."""
    out = await redis.eval(_RETURN, 1, _key(state), cipher.encrypt(token_id), _fp(token_id))
    return out.decode() if isinstance(out, bytes) else str(out)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Claimed:
    token_id: str
    exp: int
    raw: str


ClaimRefusal = Literal["missing", "used", "other_user", "not_returned"]


async def claim(
    redis: Redis, cipher: svc.Cipher, state: str, user_id: str, used_ttl_s: int
) -> Claimed | ClaimRefusal:
    """Single use: hand this user the tokenId and forget the pending login, atomically."""
    out: Any = await redis.eval(
        _CONSUME, 2, _key(state), _used_key(state), user_id, str(used_ttl_s)
    )
    text = out.decode() if isinstance(out, bytes) else str(out)
    if text in ("missing", "used", "other_user", "not_returned"):
        return text  # type: ignore[return-value]
    rec = json.loads(text)
    return Claimed(token_id=cipher.decrypt(rec["token"]), exp=int(rec["exp"]), raw=text)


async def restore(redis: Redis, state: str, claimed: Claimed) -> bool:
    """Put a claimed login back (Dhan was unreachable, so the tokenId was not spent) for the
    rest of its lifetime, so the admin can retry. False when it has expired meanwhile."""
    left = claimed.exp - int(time.time())
    if left <= 0:
        return False
    await redis.set(_key(state), claimed.raw, ex=left)
    await redis.delete(_used_key(state))
    return True


async def discard(redis: Redis, state: str) -> None:
    await redis.delete(_key(state))
