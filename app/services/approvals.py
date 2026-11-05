"""The approval engine (docs/19-github-issues-prs.md, docs/23-approval-engine.md).

A gated operation never runs directly. The caller files a request:

    req = await gate.request(kind, target, payload, artifact, requested_by=..., client=..., db=db)

which is stored as `pending` in audit.approval_requests. The policy that applies is resolved
from audit.approval_policies (the most specific `kind_pattern` wins, see `resolve_policy`)
and snapshotted onto the request: required approvals, expiry, self-approval, re-auth. The
artifacts (what the approver is shown) are stored in audit.approval_artifacts, one row per
name, and `artifact_sha256` binds them all. An admin then calls

    await gate.approve(id, admin, reauth_password, deps=..., client=...)   # executes
    await gate.deny(id, admin, client=...)

`approve` re-authenticates server-side (the admin's password, or a recent session re-auth
stamp when the policy allows one), refuses a self-approval the policy does not allow and a
second approval by the same admin, and counts the approval. Once the policy's
`required_approvals` distinct admins have approved, the request leaves `pending` (single use:
a later approve is a 409), every artifact is re-computed by the kind's `current_artifacts`
and, on any drift, the request is `invalidated` without running anything. Only then the
executor registered for the kind runs; the request ends `executed` or `failed`.

Every state change is an audit.approval_events row (hash-chained per request, see
`verify_chain`) and an audit.audit_events row. Notifications (Discord) are sent in the
background after the transaction commits; they never block or fail the flow.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.auth.security import verify_password
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.approvals import (
    ApprovalArtifact,
    ApprovalEvent,
    ApprovalPolicy,
    ApprovalRequest,
)
from app.models.auth import ROLE_ADMIN, Role, User
from app.services import audit
from app.services.audit import ClientInfo

log = get_logger("services.approvals")

TERMINAL = frozenset({"denied", "expired", "invalidated", "executed", "failed"})
MAX_RESULT_TEXT = 2000
PRIMARY = "primary"
# Events that notify (Discord). Partial approvals and execution results only show in the app.
NOTIFY_EVENTS = frozenset({"requested", "approved", "denied", "expired"})

ArtifactValue = bytes | str | Mapping[str, Any]


class ApprovalError(Exception):
    """An approval call that cannot proceed. `status` is the HTTP status to answer."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExecContext:
    """What an executor gets: the DB, settings, named dependencies (e.g. "github") and who
    approved."""

    maker: async_sessionmaker[AsyncSession]
    settings: Settings
    deps: Mapping[str, Any]
    actor_user_id: uuid.UUID
    client: ClientInfo


ArtifactFn = Callable[[ExecContext, ApprovalRequest], Awaitable[ArtifactValue]]
ArtifactsFn = Callable[[ExecContext, ApprovalRequest], Awaitable[Mapping[str, ArtifactValue]]]
ExecuteFn = Callable[[ExecContext, ApprovalRequest], Awaitable[dict[str, Any]]]
StatusHook = Callable[[AsyncSession, ApprovalRequest], Awaitable[None]]
SubjectFn = Callable[[str, Mapping[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    kind: str
    execute: ExecuteFn
    # Re-computes the artifact right before execution; a different sha256 invalidates.
    current_artifact: ArtifactFn | None = None
    # Phase 20: several named artifacts (takes precedence over current_artifact).
    current_artifacts: ArtifactsFn | None = None
    # Called inside the transaction of every status change (e.g. to mirror the status into
    # github.tracked_operations).
    on_status: StatusHook | None = None
    # deps keys that must be present; checked before the request is consumed.
    requires: tuple[str, ...] = ()
    # Phase 20: the policy subject for a request (default: the kind). E.g. a merge of a
    # `repair/*` branch resolves the `repair.*` policy.
    policy_subject: SubjectFn | None = None


# Approval kinds that would act on real money. No executor may be registered for them, so no
# approval (human or not) can turn into a LIVE order, a risk-limit or execution-mode change.
# Paper intents ("trading.paper_intent") are simulated and stay allowed. Trading Phase G must
# change this list on purpose, in a reviewed commit (docs/29-trading-safety.md).
FORBIDDEN_KIND_PREFIXES = (
    "trading.live",
    "trading.order",
    "trading.mode",
    "trading.risk",
    "broker.",
    "dhan.",
    "mt5.order",
)


class ForbiddenApprovalKind(ValueError):
    """An executor for a live-trading kind: refused until Trading Phase G."""


def assert_kind_allowed(kind: str) -> None:
    k = kind.strip().lower()
    if any(k.startswith(p) for p in FORBIDDEN_KIND_PREFIXES):
        raise ForbiddenApprovalKind(
            f"approval kind {kind!r} would act on live trading; refused (docs/29)"
        )


@dataclass(slots=True)
class ExecutorRegistry:
    _specs: dict[str, ExecutorSpec] = field(default_factory=dict)

    def register(self, spec: ExecutorSpec) -> None:
        assert_kind_allowed(spec.kind)
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> ExecutorSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._specs)


REGISTRY = ExecutorRegistry()


# --- artifacts --------------------------------------------------------------------------------


def _canonical(artifact: ArtifactValue) -> bytes:
    if isinstance(artifact, Mapping):
        return json.dumps(artifact, sort_keys=True, separators=(",", ":"), default=str).encode()
    if isinstance(artifact, str):
        return artifact.encode()
    return bytes(artifact)


def artifact_sha256(artifact: ArtifactValue | None) -> str | None:
    """sha256 hex of an artifact. Mappings are hashed as canonical JSON (sorted keys)."""
    if artifact is None:
        return None
    return hashlib.sha256(_canonical(artifact)).hexdigest()


def combined_sha256(shas: Mapping[str, str]) -> str | None:
    """The request's `artifact_sha256`: the artifact's own hash when there is only the primary
    one (unchanged from Phase 16), else the hash of the sorted {name: sha256} map."""
    if not shas:
        return None
    if set(shas) == {PRIMARY}:
        return shas[PRIMARY]
    return artifact_sha256(dict(shas))


def _artifact_row(name: str, artifact: ArtifactValue, max_chars: int) -> ApprovalArtifact:
    data = _canonical(artifact)
    sha = hashlib.sha256(data).hexdigest()
    text: str | None
    if isinstance(artifact, bytes | bytearray):
        try:
            text = bytes(artifact).decode()
        except UnicodeDecodeError:
            text = None
    else:
        text = data.decode()
    truncated = text is not None and len(text) > max_chars
    return ApprovalArtifact(
        name=name[:64],
        sha256=sha,
        size=len(data),
        content=text[:max_chars] if text is not None else None,
        content_ref=None if text is not None else f"sha256:{sha}",
        truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class DriftStatus:
    # ok | drifted | unknown (could not recompute) | not_checked | no_artifact
    status: str
    drifted: list[str] = field(default_factory=list)
    detail: str | None = None


# --- policies ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Policy:
    id: int | None
    kind_pattern: str
    required_approvals: int = 1
    expiry_seconds: int | None = None
    allow_self_approval: bool = True
    require_reauth: bool = True
    reauth_max_age_seconds: int = 0
    notify_channels: tuple[str, ...] = ("discord",)
    risk_level: str = "medium"

    @classmethod
    def from_row(cls, row: ApprovalPolicy) -> Policy:
        return cls(
            id=row.id,
            kind_pattern=row.kind_pattern,
            required_approvals=row.required_approvals,
            expiry_seconds=row.expiry_seconds,
            allow_self_approval=row.allow_self_approval,
            require_reauth=row.require_reauth,
            reauth_max_age_seconds=row.reauth_max_age_seconds,
            notify_channels=tuple(str(c) for c in (row.notify_channels or [])),
            risk_level=row.risk_level,
        )

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any] | None) -> Policy:
        if not snap:  # a request filed before Phase 20: the Phase 16 rules
            return BUILTIN_POLICY
        names = cls.__dataclass_fields__
        values = {k: v for k, v in snap.items() if k in names}
        values["notify_channels"] = tuple(values.get("notify_channels") or ())
        return cls(**values)

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["notify_channels"] = list(self.notify_channels)
        return d


# Used when no enabled policy matches (or the table is empty): the Phase 16 behaviour.
BUILTIN_POLICY = Policy(id=None, kind_pattern="*", notify_channels=())


def pattern_specificity(pattern: str) -> tuple[int, int, int]:
    """Higher sorts first: an exact pattern beats any wildcard; then more literal
    characters; then fewer wildcards."""
    wild = sum(pattern.count(c) for c in "*?[")
    literal = len(pattern) - wild
    return (0 if wild else 1, literal, -wild)


class _PolicyLike(Protocol):
    @property
    def id(self) -> int | None: ...

    @property
    def kind_pattern(self) -> str: ...


def resolve_policy[P: _PolicyLike](policies: Iterable[P], subject: str) -> P | None:
    """The most specific enabled policy whose pattern matches `subject` (ties: lowest id)."""
    best: P | None = None
    best_key: tuple[int, int, int, int] | None = None
    for pol in policies:
        if not getattr(pol, "enabled", True) or not fnmatch.fnmatchcase(subject, pol.kind_pattern):
            continue
        key = (*pattern_specificity(pol.kind_pattern), -(pol.id or 0))
        if best_key is None or key > best_key:
            best, best_key = pol, key
    return best


async def load_policy(db: AsyncSession, subject: str) -> Policy:
    rows = (await db.scalars(select(ApprovalPolicy).where(ApprovalPolicy.enabled.is_(True)))).all()
    row = resolve_policy(list(rows), subject)
    return Policy.from_row(row) if row is not None else BUILTIN_POLICY


# --- the event hash chain ---------------------------------------------------------------------


def _jsonable(details: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(dict(details or {}), default=str))
    return out


def event_hash(
    prev_hash: str | None,
    request_id: uuid.UUID,
    event: str,
    actor: uuid.UUID | None,
    details: Mapping[str, Any],
    at: datetime,
) -> str:
    body = json.dumps(
        {
            "request_id": str(request_id),
            "event": event,
            "actor": str(actor) if actor else None,
            "details": details,
            "at": at.astimezone(UTC).isoformat(timespec="microseconds"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(((prev_hash or "") + body).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainStatus:
    # ok | broken | legacy (only pre-chain events) | empty
    status: str
    verified: int
    legacy: int
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(events: Sequence[ApprovalEvent], head_hash: str | None) -> ChainStatus:
    """Recompute every hash in order. Events from before the chain existed (hash NULL) may only
    precede the first hashed one. `head_hash` (the request's event_head_hash) must equal the
    last hash, so a deleted tail is detected too."""
    prev: str | None = None
    verified = legacy = 0
    for ev in events:
        if ev.hash is None:
            if verified:
                return ChainStatus("broken", verified, legacy, ev.id, "unhashed event in chain")
            legacy += 1
            continue
        if ev.prev_hash != prev:
            return ChainStatus("broken", verified, legacy, ev.id, "prev_hash mismatch")
        expected = event_hash(prev, ev.request_id, ev.event, ev.actor, ev.details or {}, ev.at)
        if expected != ev.hash:
            return ChainStatus("broken", verified, legacy, ev.id, "hash mismatch")
        prev = ev.hash
        verified += 1
    if verified and head_hash != prev:
        return ChainStatus("broken", verified, legacy, None, "head hash mismatch")
    if not verified:
        if head_hash is not None:
            return ChainStatus("broken", 0, legacy, None, "head hash without events")
        return ChainStatus("legacy" if legacy else "empty", 0, legacy)
    return ChainStatus("ok", verified, legacy)


# --- notifications (after commit, background) --------------------------------------------------

_OUTBOX = "approval_outbox"
_HOOKED = "approval_outbox_hooked"
Dispatcher = Callable[[Settings, list[dict[str, Any]]], None]


def _default_dispatch(settings: Settings, notices: list[dict[str, Any]]) -> None:
    from app.integrations import discord  # lazy: keeps the engine importable without httpx use

    discord.notify_background(settings, notices)


DISPATCHERS: list[Dispatcher] = [_default_dispatch]


def _after_commit(session: Session) -> None:
    items: list[tuple[Settings, dict[str, Any]]] = session.info.pop(_OUTBOX, [])
    if not items:
        return
    by_settings: dict[int, tuple[Settings, list[dict[str, Any]]]] = {}
    for settings, notice in items:
        by_settings.setdefault(id(settings), (settings, []))[1].append(notice)
    for settings, notices in by_settings.values():
        for dispatch in DISPATCHERS:
            try:
                dispatch(settings, notices)
            except Exception as exc:  # a notifier never breaks the flow
                log.warning("approval_notify_dispatch_failed", error=type(exc).__name__)


def _after_rollback(session: Session) -> None:
    session.info.pop(_OUTBOX, None)


def _queue_notice(db: AsyncSession, settings: Settings, notice: dict[str, Any]) -> None:
    sync = db.sync_session
    if not sync.info.get(_HOOKED):
        sa_event.listen(sync, "after_commit", _after_commit)
        sa_event.listen(sync, "after_rollback", _after_rollback)
        sync.info[_HOOKED] = True
    sync.info.setdefault(_OUTBOX, []).append((settings, notice))


# --- helpers ----------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _error_result(exc: BaseException) -> dict[str, Any]:
    """Never str() an arbitrary exception into the DB: only already-redacted messages."""
    msg = getattr(exc, "message", None)
    code = getattr(exc, "code", None)
    text = f"{code}: {msg}" if code and isinstance(msg, str) else type(exc).__name__
    out: dict[str, Any] = {"error": text[:MAX_RESULT_TEXT]}
    if isinstance(code, str) and isinstance(msg, str):
        out["code"] = code[:64]
        status = getattr(exc, "status", None)
        if isinstance(status, int) and 400 <= status < 600:
            out["http_status"] = status
    return out


def _error_text(exc: BaseException) -> str:  # kept for callers of the Phase 16 helper
    text: str = _error_result(exc)["error"]
    return text


def _default_summary(kind: str, target: str) -> str:
    return f"{kind} on {target}"[:500]


@dataclass(frozen=True, slots=True)
class ApprovalDetailData:
    request: ApprovalRequest
    events: list[ApprovalEvent]
    artifacts: list[ApprovalArtifact]
    policy: Policy
    drift: DriftStatus
    chain: ChainStatus
    approvers: list[uuid.UUID]


class ApprovalGate:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: ExecutorRegistry = REGISTRY,
    ) -> None:
        self.maker = maker
        self.settings = settings
        self.registry = registry

    # --- helpers ----------------------------------------------------------------------------

    async def _event(
        self,
        db: AsyncSession,
        req: ApprovalRequest,
        event: str,
        actor: uuid.UUID | None,
        client: ClientInfo,
        details: dict[str, Any] | None = None,
    ) -> None:
        clean = _jsonable(details)
        at = _now()
        prev = req.event_head_hash
        digest = event_hash(prev, req.id, event, actor, clean, at)
        db.add(
            ApprovalEvent(
                request_id=req.id,
                event=event,
                actor=actor,
                details=clean,
                at=at,
                prev_hash=prev,
                hash=digest,
            )
        )
        req.event_head_hash = digest
        audit.record(
            db,
            f"approval_{event}",
            client,
            actor_user_id=actor,
            details={"approval_id": str(req.id), "kind": req.kind, "target": req.target} | clean,
        )
        if event in NOTIFY_EVENTS:
            policy = Policy.from_snapshot(req.policy)
            if "discord" in policy.notify_channels:
                _queue_notice(
                    db,
                    self.settings,
                    {
                        "event": event,
                        "approval_id": str(req.id),
                        "kind": req.kind,
                        "target": req.target,
                        "summary": req.summary or _default_summary(req.kind, req.target),
                        "risk_level": req.risk_level,
                        "required_approvals": req.required_approvals,
                        "received_approvals": req.received_approvals,
                    },
                )

    async def _set_status(
        self,
        db: AsyncSession,
        req: ApprovalRequest,
        status: str,
        actor: uuid.UUID | None,
        client: ClientInfo,
        details: dict[str, Any] | None = None,
    ) -> None:
        req.status = status
        await self._event(db, req, status, actor, client, details)
        await db.flush()
        spec = self.registry.get(req.kind)
        if spec is not None and spec.on_status is not None:
            await spec.on_status(db, req)

    async def _expire_if_due(self, db: AsyncSession, req: ApprovalRequest) -> bool:
        if req.status == "pending" and req.expires_at <= _now():
            await self._set_status(db, req, "expired", None, ClientInfo())
            return True
        return False

    async def _only_admin(self, db: AsyncSession, user_id: uuid.UUID) -> bool:
        ids = (
            await db.scalars(
                select(User.id)
                .join(Role, User.role_id == Role.id)
                .where(Role.name == ROLE_ADMIN, User.is_active.is_(True))
                .limit(2)
            )
        ).all()
        return list(ids) == [user_id]

    async def _self_approval_ok(
        self, db: AsyncSession, req: ApprovalRequest, admin_id: uuid.UUID, policy: Policy
    ) -> bool:
        if req.requested_by != admin_id:
            return True
        return (
            policy.allow_self_approval
            and self.settings.APPROVALS_ALLOW_SELF_APPROVAL
            and await self._only_admin(db, admin_id)
        )

    @staticmethod
    async def _approvers(db: AsyncSession, request_id: uuid.UUID) -> list[uuid.UUID]:
        rows = (
            await db.scalars(
                select(ApprovalEvent.actor)
                .where(
                    ApprovalEvent.request_id == request_id,
                    ApprovalEvent.event.in_(("approval_recorded", "approved")),
                    ApprovalEvent.actor.is_not(None),
                )
                .order_by(ApprovalEvent.id)
            )
        ).all()
        seen: list[uuid.UUID] = []
        for a in rows:
            if a is not None and a not in seen:
                seen.append(a)
        return seen

    @staticmethod
    async def _stored_shas(db: AsyncSession, request_id: uuid.UUID) -> dict[str, str]:
        rows = (
            await db.execute(
                select(ApprovalArtifact.name, ApprovalArtifact.sha256).where(
                    ApprovalArtifact.request_id == request_id
                )
            )
        ).all()
        return {name: sha for name, sha in rows}

    async def _current_shas(
        self, spec: ExecutorSpec, ctx: ExecContext, req: ApprovalRequest
    ) -> dict[str, str] | None:
        if spec.current_artifacts is not None:
            current = await spec.current_artifacts(ctx, req)
        elif spec.current_artifact is not None:
            current = {PRIMARY: await spec.current_artifact(ctx, req)}
        else:
            return None
        return {k: hashlib.sha256(_canonical(v)).hexdigest() for k, v in current.items()}

    async def _drift(
        self, spec: ExecutorSpec, ctx: ExecContext, req: ApprovalRequest, stored: dict[str, str]
    ) -> tuple[DriftStatus, str | None]:
        """Compare every stored artifact with its recomputed value (raises what the artifact
        functions raise)."""
        if req.artifact_sha256 is None:
            return DriftStatus("no_artifact"), None
        current = await self._current_shas(spec, ctx, req)
        if current is None:
            return DriftStatus("no_artifact"), None
        if not stored:  # filed before Phase 20: only the combined hash exists
            stored_view = {PRIMARY: req.artifact_sha256}
            current_view = {PRIMARY: combined_sha256(current) or ""}
        else:
            stored_view, current_view = stored, current
        drifted = sorted(
            n
            for n in set(stored_view) | set(current_view)
            if stored_view.get(n) != current_view.get(n)
        )
        actual = combined_sha256(current)
        return (DriftStatus("drifted", drifted) if drifted else DriftStatus("ok")), actual

    # --- interface --------------------------------------------------------------------------

    async def request(
        self,
        kind: str,
        target: str,
        payload: dict[str, Any],
        artifact: ArtifactValue | None,
        *,
        requested_by: uuid.UUID | None,
        client: ClientInfo,
        db: AsyncSession,
        artifacts: Mapping[str, ArtifactValue] | None = None,
        summary: str | None = None,
    ) -> ApprovalRequest:
        """File a pending request in the caller's transaction (the caller commits)."""
        spec = self.registry.get(kind)
        if spec is None:
            raise ApprovalError(500, "unknown_approval_kind", f"no executor for {kind!r}")
        named: dict[str, ArtifactValue] = {}
        if artifact is not None:
            named[PRIMARY] = artifact
        named.update(artifacts or {})
        rows = [
            _artifact_row(n, a, self.settings.APPROVALS_ARTIFACT_MAX_CHARS)
            for n, a in sorted(named.items())
        ]
        subject = spec.policy_subject(kind, payload) if spec.policy_subject else kind
        policy = await load_policy(db, subject)
        ttl = (
            timedelta(seconds=policy.expiry_seconds)
            if policy.expiry_seconds
            else timedelta(minutes=self.settings.APPROVALS_TTL_MINUTES)
        )
        req = ApprovalRequest(
            kind=kind,
            target=target[:300],
            payload=payload,
            artifact_sha256=combined_sha256({r.name: r.sha256 for r in rows}),
            status="pending",
            requested_by=requested_by,
            expires_at=_now() + ttl,
            policy_id=policy.id,
            policy=policy.snapshot(),
            policy_subject=subject[:64],
            required_approvals=policy.required_approvals,
            received_approvals=0,
            risk_level=policy.risk_level,
            summary=(summary or _default_summary(kind, target))[:500],
            requester_ip=(client.ip or None) and str(client.ip)[:64],
        )
        db.add(req)
        await db.flush()
        for r in rows:
            r.request_id = req.id
            db.add(r)
        await self._event(
            db,
            req,
            "requested",
            requested_by,
            client,
            {"artifact_sha256": req.artifact_sha256, "expires_at": req.expires_at.isoformat()},
        )
        await db.flush()
        log.info(
            "approval_requested",
            approval_id=str(req.id),
            kind=kind,
            target=target,
            policy=policy.kind_pattern,
        )
        return req

    async def _reauth(
        self,
        db: AsyncSession,
        req: ApprovalRequest,
        admin: User,
        password: str | None,
        stamped_at: datetime | None,
        policy: Policy,
        client: ClientInfo,
    ) -> tuple[str | None, ApprovalError | None]:
        """Returns (method, refusal). A wrong password is counted against the request."""
        if password is not None:
            ok = await asyncio.to_thread(verify_password, admin.password_hash, password)
            if ok:
                return "password", None
            await self._event(db, req, "reauth_failed", admin.id, client)
            await db.flush()
            failures = await db.scalar(
                select(func.count())
                .select_from(ApprovalEvent)
                .where(ApprovalEvent.request_id == req.id, ApprovalEvent.event == "reauth_failed")
            )
            if req.status == "pending" and (failures or 0) >= (
                self.settings.APPROVALS_MAX_REAUTH_FAILURES
            ):
                req.decided_by, req.decided_at = admin.id, _now()
                await self._set_status(
                    db, req, "denied", admin.id, client, {"reason": "too many re-auth failures"}
                )
            return None, ApprovalError(403, "reauth_failed", "password re-authentication failed")
        if not policy.require_reauth:
            return "not_required", None
        if (
            stamped_at is not None
            and policy.reauth_max_age_seconds > 0
            and timedelta(0)
            <= _now() - stamped_at
            <= timedelta(seconds=policy.reauth_max_age_seconds)
        ):
            return "session_stamp", None
        return None, ApprovalError(
            403,
            "reauth_required",
            "this approval needs your password"
            + ("" if policy.reauth_max_age_seconds else " (a session re-auth does not count)"),
        )

    async def approve(
        self,
        request_id: uuid.UUID,
        admin: User,
        reauth_password: str | None,
        *,
        deps: Mapping[str, Any],
        client: ClientInfo,
        reauth_stamped_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Re-auth, count the approval, and once the policy's quorum is reached consume,
        re-check the artifacts and execute. Raises ApprovalError (403/404/409/503); an executor
        failure is not raised but recorded as status `failed`. Below the quorum the request
        is returned still `pending` with `received_approvals` increased."""
        outcome: ApprovalError | None = None
        quorum = False
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            spec = self.registry.get(req.kind)
            policy = Policy.from_snapshot(req.policy)
            method, outcome = await self._reauth(
                db, req, admin, reauth_password, reauth_stamped_at, policy, client
            )
            if outcome is not None:
                pass
            elif await self._expire_if_due(db, req):
                outcome = ApprovalError(409, "approval_expired", "approval request has expired")
            elif req.status != "pending":
                code = (
                    "approval_already_used"
                    if req.status in ("approved", "executed", "failed")
                    else "approval_not_pending"
                )
                outcome = ApprovalError(409, code, f"approval request is {req.status}")
            elif spec is None:
                outcome = ApprovalError(409, "unknown_approval_kind", f"no executor for {req.kind}")
            elif missing := [k for k in spec.requires if deps.get(k) is None]:
                outcome = ApprovalError(
                    503, f"{missing[0]}_not_configured", f"{missing[0]} is not configured"
                )
            elif not await self._self_approval_ok(db, req, admin.id, policy):
                await self._event(db, req, "self_approval_refused", admin.id, client)
                outcome = ApprovalError(
                    403, "self_approval_not_allowed", "another admin must approve this request"
                )
            elif admin.id in await self._approvers(db, req.id):
                outcome = ApprovalError(
                    409, "already_approved_by_you", "a different admin must add the next approval"
                )
            else:
                received = req.received_approvals + 1
                req.received_approvals = received
                info = {"reauth": method, "received": received, "required": req.required_approvals}
                if received < req.required_approvals:
                    await self._event(db, req, "approval_recorded", admin.id, client, info)
                else:
                    quorum = True
                    req.decided_by, req.decided_at = admin.id, _now()
                    await self._set_status(db, req, "approved", admin.id, client, info)
        if outcome is not None:
            raise outcome
        if not quorum:
            return req
        assert spec is not None

        ctx = ExecContext(self.maker, self.settings, deps, admin.id, client)
        status, result, details = "executed", {}, {}
        try:
            async with self.maker() as db:
                stored = await self._stored_shas(db, req.id)
            drift, actual = await self._drift(spec, ctx, req, stored)
            if drift.status == "drifted":
                status = "invalidated"
                result = {"reason": "artifact_drift"}
                details = {
                    "expected": req.artifact_sha256,
                    "actual": actual,
                    "drifted": drift.drifted,
                }
            if status == "executed":
                result = await spec.execute(ctx, req)
        except Exception as exc:  # recorded as `failed`; the raw text never leaves
            status, result = "failed", _error_result(exc)
            log.warning("approval_execution_failed", approval_id=str(req.id), error=result["error"])

        async with self.maker() as db, db.begin():
            row = await db.get(ApprovalRequest, req.id, with_for_update=True)
            assert row is not None
            row.result = result
            if status == "executed":
                row.executed_at = _now()
            await self._set_status(db, row, status, admin.id, client, details)
            req = row
        if status == "invalidated":
            raise ApprovalError(
                409,
                "approval_invalidated",
                "the artifact changed since the request was made; file a new request",
            )
        return req

    async def deny(
        self,
        request_id: uuid.UUID,
        admin: User,
        *,
        client: ClientInfo,
        reason: str | None = None,
    ) -> ApprovalRequest:
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            expired = await self._expire_if_due(db, req)
            if not expired and req.status == "pending":
                req.decided_by, req.decided_at = admin.id, _now()
                await self._set_status(
                    db, req, "denied", admin.id, client, {"reason": reason} if reason else None
                )
                return req
        raise ApprovalError(409, "approval_not_pending", f"approval request is {req.status}")

    async def expire_due(self, db: AsyncSession, limit: int = 500) -> int:
        rows = (
            await db.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "pending", ApprovalRequest.expires_at <= _now())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
        for req in rows:
            await self._set_status(db, req, "expired", None, ClientInfo())
        return len(rows)

    async def get(self, request_id: uuid.UUID) -> tuple[ApprovalRequest, list[ApprovalEvent]]:
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            await self._expire_if_due(db, req)
            events = (
                await db.scalars(
                    select(ApprovalEvent)
                    .where(ApprovalEvent.request_id == req.id)
                    .order_by(ApprovalEvent.id)
                )
            ).all()
            return req, list(events)

    async def detail(
        self,
        request_id: uuid.UUID,
        *,
        deps: Mapping[str, Any] | None = None,
        actor_user_id: uuid.UUID | None = None,
        check_drift: bool = False,
    ) -> ApprovalDetailData:
        req, events = await self.get(request_id)
        async with self.maker() as db:
            arts = list(
                (
                    await db.scalars(
                        select(ApprovalArtifact)
                        .where(ApprovalArtifact.request_id == req.id)
                        .order_by(ApprovalArtifact.name)
                    )
                ).all()
            )
            approvers = await self._approvers(db, req.id)
        drift = DriftStatus("not_checked")
        spec = self.registry.get(req.kind)
        if check_drift and req.status == "pending" and spec is not None:
            if any(deps is None or deps.get(k) is None for k in spec.requires):
                drift = DriftStatus("unknown", detail="dependency not configured")
            else:
                ctx = ExecContext(
                    self.maker,
                    self.settings,
                    deps or {},
                    actor_user_id or uuid.UUID(int=0),
                    ClientInfo(),
                )
                try:
                    drift, _ = await self._drift(spec, ctx, req, {a.name: a.sha256 for a in arts})
                except Exception as exc:
                    drift = DriftStatus("unknown", detail=_error_result(exc)["error"])
        return ApprovalDetailData(
            request=req,
            events=events,
            artifacts=arts,
            policy=Policy.from_snapshot(req.policy),
            drift=drift,
            chain=verify_chain(events, req.event_head_hash),
            approvers=approvers,
        )

    async def list_requests(
        self, *, status: str | None, kind: str | None, limit: int, offset: int
    ) -> tuple[list[ApprovalRequest], int]:
        rows, total, _counts = await self.list_with_counts(
            status=status, kind=kind, limit=limit, offset=offset
        )
        return rows, total

    async def list_with_counts(
        self, *, status: str | None, kind: str | None, limit: int, offset: int
    ) -> tuple[list[ApprovalRequest], int, dict[str, int]]:
        """A page plus counts by status (for the `kind` filter, whatever `status` is)."""
        async with self.maker() as db, db.begin():
            await self.expire_due(db)
        async with self.maker() as db:
            q = select(ApprovalRequest)
            cq = select(ApprovalRequest.status, func.count()).group_by(ApprovalRequest.status)
            if status:
                q = q.where(ApprovalRequest.status == status)
            if kind:
                q = q.where(ApprovalRequest.kind == kind)
                cq = cq.where(ApprovalRequest.kind == kind)
            total = await db.scalar(select(func.count()).select_from(q.subquery()))
            rows = (
                await db.scalars(
                    q.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset)
                )
            ).all()
            counts = {str(s): int(n) for s, n in (await db.execute(cq)).all()}
            return list(rows), total or 0, counts

    async def inbox(self, admin_id: uuid.UUID, limit: int = 200) -> list[ApprovalRequest]:
        """Pending, unexpired requests this admin can act on: not already approved by them,
        and not their own unless the policy lets them self-approve."""
        async with self.maker() as db, db.begin():
            await self.expire_due(db)
        async with self.maker() as db:
            rows = (
                await db.scalars(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.status == "pending")
                    .order_by(ApprovalRequest.created_at.desc())
                    .limit(limit)
                )
            ).all()
            out = []
            for req in rows:
                if admin_id in await self._approvers(db, req.id):
                    continue
                if not await self._self_approval_ok(
                    db, req, admin_id, Policy.from_snapshot(req.policy)
                ):
                    continue
                out.append(req)
            return out


__all__ = [
    "BUILTIN_POLICY",
    "PRIMARY",
    "REGISTRY",
    "TERMINAL",
    "ApprovalDetailData",
    "ApprovalError",
    "ApprovalGate",
    "ChainStatus",
    "DriftStatus",
    "ExecContext",
    "ExecutorRegistry",
    "ExecutorSpec",
    "Policy",
    "artifact_sha256",
    "combined_sha256",
    "event_hash",
    "load_policy",
    "pattern_specificity",
    "resolve_policy",
    "verify_chain",
]
