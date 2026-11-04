"""Runtime settings and secrets: /api/v1/settings (admin only; docs/settings.md).

No endpoint returns a value. GET lists masks and where each value comes from; PUT sets a DB
value over .env; DELETE drops it so .env applies again; POST …/test tries the credential.
Writing a secret (or a URL a credential is sent to) needs the admin's password in the body.
Every write and test is audited by key name, rate limited per admin, and fails closed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.auth.deps import AdminRequired, DbSession
from app.auth.security import verify_password
from app.core.config import Settings
from app.models.app_settings import AppSetting
from app.services import app_settings as svc
from app.services import app_settings_runtime, audit
from app.services.audit import ClientInfo

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/settings", tags=["settings"])

WRITES_PER_10_MIN = 20
TESTS_PER_10_MIN = 20
MAX_REAUTH_FAILURES = 5
REAUTH_FAIL_WINDOW = 900
TEST_RESULT_TTL = 7 * 24 * 3600

Source = Literal["db", "env", "default", "unset"]


class SettingOut(BaseModel):
    key: str
    category: str
    description: str
    kind: str
    is_secret: bool
    requires_reauth: bool
    is_set: bool
    masked: str | None
    source: Source
    updated_at: datetime | None = None
    version: int | None = None
    # DHAN_ACCESS_TOKEN only: the token's own expiry, and whether it is inside the warning.
    expires_at: datetime | None = None
    expires_soon: bool | None = None


class TestResultOut(BaseModel):
    category: str
    ok: bool
    detail: str
    tested_at: datetime


class SettingsOut(BaseModel):
    categories: list[str]
    settings: list[SettingOut]
    tests: dict[str, TestResultOut]
    encryption_ready: bool


class SettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=8192)
    password: str | None = Field(default=None, max_length=1024)


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(default=None, max_length=1024)


def _error(status: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status, {"code": code, "detail": detail})


def _spec(key: str) -> svc.Spec:
    spec = svc.REGISTRY.get(key)
    if spec is not None:
        return spec
    if key in svc.BOOTSTRAP_KEYS:
        raise _error(403, "bootstrap_key", f"{key} is a bootstrap key: set it in .env only")
    raise _error(404, "unknown_key", "not an editable setting")


def _limiter(request: Request, admin: AdminRequired, bucket: str) -> BrokerRateLimiter:
    limit = WRITES_PER_10_MIN if bucket == "write" else TESTS_PER_10_MIN
    return BrokerRateLimiter(
        request.app.state.redis,
        f"rl:settings:{admin.user.id}",
        {bucket: [Window(600, limit)]},
    )


async def _rate_limit(limiter: BrokerRateLimiter, bucket: str) -> None:
    try:
        await limiter.acquire(bucket, max_wait=0)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            {"code": "rate_limited", "detail": "too many settings changes; try again later"},
            headers={"Retry-After": str(max(1, int(exc.wait_s + 0.999)))},
        ) from exc
    except Exception as exc:  # Redis down: fail CLOSED for secrets
        raise _error(503, "rate_limit_unavailable", "settings are unavailable") from exc


async def write_limited(request: Request, admin: AdminRequired) -> None:
    await _rate_limit(_limiter(request, admin, "write"), "write")


async def test_limited(request: Request, admin: AdminRequired) -> None:
    await _rate_limit(_limiter(request, admin, "test"), "test")


async def _reauth(
    request: Request, db: DbSession, admin: AdminRequired, spec: svc.Spec, password: str | None
) -> None:
    if not spec.reauth:
        return
    redis = request.app.state.redis
    fail_key = f"settings:reauth_fail:{admin.session.id}"
    try:
        failures = int(await redis.get(fail_key) or 0)
    except Exception as exc:
        raise _error(503, "reauth_unavailable", "settings are unavailable") from exc
    if failures >= MAX_REAUTH_FAILURES:
        raise _error(429, "too_many_reauth_failures", "try again later")
    user = admin.user
    if password:
        ok: bool = await asyncio.to_thread(verify_password, user.password_hash, password)
        if ok:
            return
    pipe = redis.pipeline()
    pipe.incr(fail_key)
    pipe.expire(fail_key, REAUTH_FAIL_WINDOW)
    await pipe.execute()
    audit.record(
        db,
        "settings_reauth_failed",
        ClientInfo.from_request(request),
        actor_user_id=user.id,
        details={"key": spec.key},
    )
    await db.commit()
    raise _error(403, "reauth_failed", "password re-authentication failed")


def _cipher(settings: Settings) -> svc.Cipher:
    try:
        return svc.Cipher(settings.SETTINGS_MASTER_KEY)
    except svc.SettingsError as exc:
        raise _error(503, exc.code, exc.detail) from exc


def _source(key: str, row: AppSetting | None, env: Settings) -> Source:
    if row is not None:
        return "db"
    if svc.current_value(env, key) is None:
        return "unset"
    return "env" if key in env.model_fields_set else "default"


async def _test_results(request: Request) -> dict[str, TestResultOut]:
    out: dict[str, TestResultOut] = {}
    try:
        raw = await request.app.state.redis.mget([f"settings:test:{c}" for c in svc.CATEGORIES])
    except Exception:
        return out
    for category, item in zip(svc.CATEGORIES, raw, strict=True):
        if item:
            out[category] = TestResultOut.model_validate_json(item)
    return out


def _describe(spec: svc.Spec, row: AppSetting | None, live: Settings, env: Settings) -> SettingOut:
    value = svc.current_value(live, spec.key)
    item = SettingOut(
        key=spec.key,
        category=spec.category,
        description=spec.description,
        kind=spec.kind,
        is_secret=spec.secret,
        requires_reauth=spec.reauth,
        is_set=value is not None,
        masked=svc.mask(value, spec.secret),
        source=_source(spec.key, row, env),
        updated_at=row.updated_at if row is not None else None,
        version=row.version if row is not None else None,
    )
    if spec.key == "DHAN_ACCESS_TOKEN":
        exp = svc.dhan_token_expiry(value)
        item.expires_at = exp
        if exp is not None:
            warn = timedelta(hours=live.DHAN_TOKEN_WARN_HOURS)
            item.expires_soon = exp - datetime.now(UTC) <= warn
    return item


@router.get("", response_model=SettingsOut)
async def list_settings(request: Request, _: AdminRequired, db: DbSession) -> SettingsOut:
    """Every editable key: is it set, where from, a mask, when it changed. Never a value."""
    state = request.app.state
    rows = {r.key: r for r in (await db.scalars(select(AppSetting))).all()}
    live: Settings = state.settings
    env: Settings = state.env_settings
    return SettingsOut(
        categories=list(svc.CATEGORIES),
        settings=[_describe(s, rows.get(k), live, env) for k, s in svc.REGISTRY.items()],
        tests=await _test_results(request),
        encryption_ready=svc.cipher_for(env) is not None,
    )


async def _apply_and_publish(request: Request, key: str) -> SettingOut:
    app = request.app
    await app_settings_runtime.reload(app)
    await svc.publish(app.state.redis, [key])
    async with app.state.sessionmaker() as db:
        row = await db.get(AppSetting, key)
    return _describe(svc.REGISTRY[key], row, app.state.settings, app.state.env_settings)


@router.put("/{key}", response_model=SettingOut, dependencies=[Depends(write_limited)])
async def put_setting(
    key: str, body: SettingIn, request: Request, admin: AdminRequired, db: DbSession
) -> SettingOut:
    """Set or replace the DB value (it overrides .env). Secrets need `password`."""
    spec = _spec(key)
    env: Settings = request.app.state.env_settings
    cipher = _cipher(env)
    await _reauth(request, db, admin, spec, body.password)
    try:
        value = svc.normalize(spec, body.value)
        overrides = await svc.load_overrides(db, cipher)
        overrides[key] = value
        svc.checked_overlay(env, overrides, key)
    except svc.SettingsError as exc:
        raise _error(422, exc.code, exc.detail) from None
    row = await db.get(AppSetting, key, with_for_update=True)
    if row is None:
        row = AppSetting(key=key, version=0)
        db.add(row)
    row.value_encrypted = cipher.encrypt(value)
    row.is_secret = spec.secret
    row.category = spec.category
    row.updated_by = admin.user.id
    row.updated_at = datetime.now(UTC)
    row.version = (row.version or 0) + 1
    audit.record(
        db,
        "settings_updated",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"key": key, "category": spec.category, "is_secret": spec.secret,
                 "version": row.version},
    )  # fmt: skip
    await db.commit()
    return await _apply_and_publish(request, key)


@router.delete("/{key}", response_model=SettingOut, dependencies=[Depends(write_limited)])
async def revert_setting(
    key: str,
    request: Request,
    admin: AdminRequired,
    db: DbSession,
    body: RevertIn | None = None,
) -> SettingOut:
    """Drop the DB value so .env (or the default) applies again. Secrets need `password`."""
    spec = _spec(key)
    await _reauth(request, db, admin, spec, body.password if body else None)
    row = await db.get(AppSetting, key, with_for_update=True)
    if row is None:
        raise _error(404, "not_in_db", f"{key} already comes from .env")
    await db.delete(row)
    audit.record(
        db,
        "settings_reverted",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"key": key, "category": spec.category, "is_secret": spec.secret},
    )
    await db.commit()
    return await _apply_and_publish(request, key)


@router.post("/{key}/test", response_model=TestResultOut, dependencies=[Depends(test_limited)])
async def test_setting(
    key: str, request: Request, admin: AdminRequired, db: DbSession
) -> TestResultOut:
    """Try the key's group against its service with the values in effect now. The result
    is an outcome line, never a value or a response body."""
    spec = _spec(key)
    state = request.app.state
    # Its own client: the shared one has the health-check timeout. Tests inject a transport.
    transport = getattr(state, "settings_test_transport", None)
    async with httpx.AsyncClient(timeout=15.0, transport=transport) as http:
        outcome = await svc.run_test(spec.category, state.settings, http)
    result = TestResultOut(
        category=spec.category, ok=outcome.ok, detail=outcome.detail, tested_at=datetime.now(UTC)
    )
    try:
        await state.redis.set(
            f"settings:test:{spec.category}", result.model_dump_json(), ex=TEST_RESULT_TTL
        )
    except Exception as exc:  # the result is still returned, just not remembered
        log.warning("settings_test_result_not_stored", error=type(exc).__name__)
    audit.record(
        db,
        "settings_tested",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"key": key, "category": spec.category, "ok": outcome.ok},
    )
    await db.commit()
    return result
