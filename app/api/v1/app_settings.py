"""Runtime settings and secrets: /api/v1/settings (admin only; docs/settings.md).

GET lists every key with where its value comes from: the full value for a non-secret, a mask
for a secret (POST …/reveal shows one secret after the admin's password, audited, never
cached). PUT sets a DB value over .env; DELETE drops it so .env applies again; POST …/test
tries the credential; GET /diagnostics compares every layer and every process.
Writing a secret (or a URL a credential is sent to) needs the admin's password in the body.
Every write and test is audited by key name, rate limited per admin, and fails closed.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import structlog
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.api.ui import ui_module
from app.auth.deps import AdminRequired, DbSession
from app.auth.security import verify_password
from app.core.config import Settings
from app.models.app_settings import AppSetting
from app.services import app_settings as svc
from app.services import app_settings_runtime, audit
from app.services.audit import ClientInfo

log = structlog.get_logger(__name__)
UI_MODULE = ui_module("settings", label="Settings", group="admin", icon="settings", order=90)
router = APIRouter(prefix="/v1/settings", tags=["settings"])

WRITES_PER_10_MIN = 20
TESTS_PER_10_MIN = 20
REVEALS_PER_10_MIN = 10
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
    managed: bool = False
    is_set: bool
    masked: str | None
    value: str | None = None  # the full value; None for a secret (see /{key}/reveal)
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


def _spec(key: str, write: bool = False) -> svc.Spec:
    spec = svc.REGISTRY.get(key)
    if spec is not None:
        if write and spec.managed:
            raise _error(403, "managed_key", f"{key} is set by its own flow (Log in to Dhan)")
        return spec
    if key in svc.BOOTSTRAP_KEYS:
        raise _error(403, "bootstrap_key", f"{key} is a bootstrap key: set it in .env only")
    raise _error(404, "unknown_key", "not an editable setting")


def _limiter(request: Request, admin: AdminRequired, bucket: str) -> BrokerRateLimiter:
    limit = {"write": WRITES_PER_10_MIN, "reveal": REVEALS_PER_10_MIN}.get(bucket, TESTS_PER_10_MIN)
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


async def reveal_limited(request: Request, admin: AdminRequired) -> None:
    await _rate_limit(_limiter(request, admin, "reveal"), "reveal")


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


def _decryptable(rows: dict[str, AppSetting], env: Settings) -> set[str]:
    """Keys whose DB row this server can read; the others are ignored by the overlay."""
    cipher = svc.cipher_for(env)
    if cipher is None:
        return set()
    out = set()
    for key, row in rows.items():
        try:
            cipher.decrypt(row.value_encrypted)
        except InvalidToken:
            continue
        out.add(key)
    return out


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
        managed=spec.managed,
        is_set=value is not None,
        masked=svc.mask(value, spec.secret),
        value=svc.plain(spec, value),
        source=_source(spec.key, row, env),
        updated_at=row.updated_at if row is not None else None,
        version=row.version if row is not None else None,
    )
    if spec.key in ("DHAN_ACCESS_TOKEN", "DHAN_SESSION_TOKEN"):
        exp = (
            svc.dhan_token_expiry(value)
            if spec.key == "DHAN_ACCESS_TOKEN"
            else svc.as_utc(live.DHAN_SESSION_EXPIRES_AT)
        )
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
    # A row the server cannot decrypt is not in effect: don't call it "db".
    readable = _decryptable(rows, env)
    return SettingsOut(
        categories=list(svc.CATEGORIES),
        settings=[
            _describe(s, rows.get(k) if k in readable else None, live, env)
            for k, s in svc.REGISTRY.items()
        ],
        tests=await _test_results(request),
        encryption_ready=svc.cipher_for(env) is not None,
    )


# --- diagnostics --------------------------------------------------------------------------


IssueCode = Literal["db_undecryptable", "db_rejected", "db_ignored", "stale", "shadows_env"]


class KeyDiagnosisOut(BaseModel):
    key: str
    category: str
    is_secret: bool
    kind: str
    requires_reauth: bool
    # Each layer: a mask, and the full value when the key is not a secret.
    env_masked: str | None
    env_value: str | None
    env_source: Literal["env", "default", "unset"]
    db_present: bool
    db_masked: str | None
    db_value: str | None
    db_version: int | None
    db_updated_at: datetime | None
    expected_masked: str | None
    expected_value: str | None
    expected_source: Source
    live_masked: str | None
    live_value: str | None
    issues: list[IssueCode]


class AiRuntimeOut(BaseModel):
    configured: bool
    key_masked: str | None
    matches_settings: bool  # the gateway holds the key the live settings name
    model_main: str
    model_router: str


class ServerOut(BaseModel):
    host: str
    pid: int
    environment: str
    started_at: datetime
    settings_applied_at: datetime | None


class ProcessOut(BaseModel):
    process: str
    host: str
    pid: int
    started_at: datetime | None
    applied_at: datetime | None
    watches: list[str]
    stale: list[str]
    unwatched_stale: list[str]
    this: bool  # the API process that answered


class RedisOut(BaseModel):
    ok: bool
    error: str | None
    listeners: int | None  # processes subscribed to settings changes


class DiagnosticsOut(BaseModel):
    checked_at: datetime
    server: ServerOut
    encryption_ready: bool
    encryption_problem: str | None
    master_keys: int
    database_ok: bool
    database_error: str | None
    rows: int
    unknown_rows: list[str]
    undecryptable: list[str]
    rejected: list[str]
    overlay_ok: bool
    overlay_fields: list[str]
    stale: list[str]
    redis: RedisOut
    processes: list[ProcessOut]
    ai: AiRuntimeOut
    tests: dict[str, TestResultOut]
    keys: list[KeyDiagnosisOut]


def _ai_runtime(state: object, live: Settings) -> AiRuntimeOut:
    gateway = getattr(state, "ai", None)
    held = gateway.settings.anthropic_key if gateway is not None else ""
    return AiRuntimeOut(
        configured=bool(gateway is not None and gateway.configured),
        key_masked=svc.mask(held or None, True),
        matches_settings=held == live.anthropic_key,
        model_main=live.AI_MODEL_MAIN,
        model_router=live.AI_MODEL_ROUTER,
    )


async def _redis_state(redis: Redis) -> tuple[RedisOut, list[dict[str, Any]]]:
    try:
        async with asyncio.timeout(5):
            applied = await svc.read_applied(redis)
            subs = await redis.pubsub_numsub(svc.CHANNEL)
        listeners = int(subs[0][1]) if subs else 0
        return RedisOut(ok=True, error=None, listeners=listeners), applied
    except Exception as exc:
        return RedisOut(ok=False, error=type(exc).__name__, listeners=None), []


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


@router.get("/diagnostics", response_model=DiagnosticsOut)
async def diagnostics(request: Request, response: Response, _: AdminRequired) -> DiagnosticsOut:
    """Every setting at each layer (.env, the encrypted DB row, what a reload would give,
    what this process runs with), what every process loaded, and where they disagree.
    Non-secret values in full; secrets as masks (use /{key}/reveal for one)."""
    response.headers["Cache-Control"] = "no-store"
    state = request.app.state
    live: Settings = state.settings
    env: Settings = state.env_settings
    rows: list[AppSetting] = []
    db_error = None
    try:
        async with state.sessionmaker() as db:
            rows = list((await db.scalars(select(AppSetting))).all())
    except Exception as exc:  # the report still shows .env and the live process
        db_error = type(exc).__name__
    redis_out, applied = await _redis_state(state.redis)
    d = svc.diagnose(env, live, rows, applied)
    host, pid = socket.gethostname(), os.getpid()
    return DiagnosticsOut(
        checked_at=datetime.now(UTC),
        server=ServerOut(
            host=host,
            pid=pid,
            environment=live.DEVOPS_ENVIRONMENT,
            started_at=svc.PROCESS_STARTED_AT,
            settings_applied_at=getattr(state, "settings_applied_at", None),
        ),
        encryption_ready=d.encryption_ready,
        encryption_problem=d.encryption_problem,
        master_keys=d.master_keys,
        database_ok=db_error is None,
        database_error=db_error,
        rows=d.rows,
        unknown_rows=list(d.unknown_rows),
        undecryptable=list(d.undecryptable),
        rejected=list(d.rejected),
        overlay_ok=d.overlay_ok,
        overlay_fields=list(d.overlay_fields),
        stale=list(d.stale),
        redis=redis_out,
        processes=[
            ProcessOut(
                process=p.process,
                host=p.host,
                pid=p.pid,
                started_at=_when(p.started_at),
                applied_at=_when(p.applied_at),
                watches=list(p.watches),
                stale=list(p.stale),
                unwatched_stale=list(p.unwatched_stale),
                this=p.process == "api" and p.host == host and p.pid == pid,
            )
            for p in d.processes
        ],
        ai=_ai_runtime(state, live),
        tests=await _test_results(request),
        keys=[
            KeyDiagnosisOut(
                key=k.key,
                category=k.category,
                is_secret=k.is_secret,
                kind=svc.REGISTRY[k.key].kind,
                requires_reauth=svc.REGISTRY[k.key].reauth,
                env_masked=k.env_masked,
                env_value=k.env_value,
                env_source=k.env_source,
                db_present=k.db_present,
                db_masked=k.db_masked,
                db_value=k.db_value,
                db_version=k.db_version,
                db_updated_at=k.db_updated_at,
                expected_masked=k.expected_masked,
                expected_value=k.expected_value,
                expected_source=k.expected_source,
                live_masked=k.live_masked,
                live_value=k.live_value,
                issues=list(k.issues),
            )
            for k in d.keys
        ],
    )


class RevealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(default=None, max_length=1024)


class RevealOut(BaseModel):
    key: str
    env: str | None
    db: str | None
    db_present: bool  # a row exists; db is None when it cannot be decrypted
    expected: str | None
    live: str | None


@router.post("/{key}/reveal", response_model=RevealOut, dependencies=[Depends(reveal_limited)])
async def reveal_setting(
    key: str,
    body: RevealIn,
    request: Request,
    response: Response,
    admin: AdminRequired,
    db: DbSession,
) -> RevealOut:
    """The full value at every layer. A secret needs the admin's password (failures count
    towards the lock-out); every reveal is audited by key name and never cached."""
    response.headers["Cache-Control"] = "no-store"
    spec = _spec(key)
    await _reauth(request, db, admin, spec, body.password)
    state = request.app.state
    rows = list((await db.scalars(select(AppSetting))).all())
    v = svc.layer_values(state.env_settings, state.settings, rows, key)
    audit.record(
        db,
        "settings_revealed",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"key": key, "category": spec.category, "is_secret": spec.secret},
    )
    await db.commit()
    return RevealOut(
        key=key, env=v.env, db=v.db, db_present=v.db_present, expected=v.expected, live=v.live
    )


class ReloadOut(BaseModel):
    changed: list[str]


@router.post("/reload", response_model=ReloadOut, dependencies=[Depends(write_limited)])
async def reload_settings(request: Request, admin: AdminRequired, db: DbSession) -> ReloadOut:
    """Re-read .env + the DB now and rebuild the clients whose keys changed (the Claude
    gateway included), here and in the workers. For when diagnostics shows stale keys."""
    app = request.app
    changed = await app_settings_runtime.reload(app)
    await svc.publish(app.state.redis, changed or list(svc.REGISTRY))
    audit.record(
        db,
        "settings_reloaded",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"changed": changed},
    )
    await db.commit()
    return ReloadOut(changed=changed)


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
    spec = _spec(key, write=True)
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
    spec = _spec(key, write=True)
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
