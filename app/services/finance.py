"""Finance service: every query is scoped to the signed-in user here, not in the router.

Ownership is the added `aureon_user_id` column. A row without it (written by the old
app before its owner was claimed) is invisible to everyone until an admin links the
legacy owner id to an Aureon user (`claim`). Admins get no extra visibility: finance
is personal data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Numeric, Table, and_, cast, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import (
    LEGACY,
    OWNED_TABLES,
    FinanceIdentity,
    astra_documents,
    expenses,
    portfolio_profiles,
    secured,
    trades,
)
from app.schemas import finance as s


class FinanceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True, slots=True)
class Resource:
    name: str
    table: Table
    date_col: str
    money: tuple[str, ...]
    out: type[BaseModel]
    filters: tuple[str, ...]  # exact-match text filters allowed on the list endpoint


TRADES = Resource("trades", trades, "ist_date", ("lot", "entry", "exit", "move", "pl"),
                  s.TradeOut, ("symbol", "session", "side"))  # fmt: skip
EXPENSES = Resource("expenses", expenses, "date", ("amount",), s.ExpenseOut, ("category", "kind"))
SECURED = Resource("secured", secured, "date", ("amt",), s.SecuredOut, ())
RESOURCES = {r.name: r for r in (TRADES, EXPENSES, SECURED)}


def _now() -> datetime:
    return datetime.now(UTC)


def to_decimal(v: float | int | Decimal | None) -> Decimal | None:
    """A double from the legacy column as the shortest exact decimal (0.1 → '0.1')."""
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(repr(float(v)))


def _to_db(res: Resource, values: dict[str, Any]) -> dict[str, Any]:
    # The legacy columns are double precision (D-A3): convert only at this boundary.
    return {k: (float(v) if k in res.money and v is not None else v) for k, v in values.items()}


def _out(res: Resource, row: Any) -> BaseModel:
    data = dict(row._mapping)
    for k in res.money:
        data[k] = to_decimal(data[k])
    return res.out.model_validate(data)


async def owner_key(db: AsyncSession, user_id: uuid.UUID) -> str:
    """The legacy `user_id` text for rows this user creates."""
    linked = await db.scalar(
        select(FinanceIdentity.legacy_user_id).where(FinanceIdentity.user_id == user_id)
    )
    return linked or str(user_id)


def _mine(res: Resource, user_id: uuid.UUID) -> Any:
    return res.table.c.aureon_user_id == user_id


async def list_rows(
    db: AsyncSession,
    res: Resource,
    user_id: uuid.UUID,
    *,
    date_from: date | None,
    date_to: date | None,
    match: dict[str, str],
    limit: int,
    offset: int,
) -> tuple[list[BaseModel], int]:
    t = res.table
    conds = [_mine(res, user_id)]
    if date_from:
        conds.append(t.c[res.date_col] >= date_from)
    if date_to:
        conds.append(t.c[res.date_col] <= date_to)
    for k, v in match.items():
        if k in res.filters:
            conds.append(t.c[k] == v)
    where = and_(*conds)
    total = int(await db.scalar(select(func.count()).select_from(t).where(where)) or 0)
    rows = (
        await db.execute(
            select(t)
            .where(where)
            .order_by(t.c[res.date_col].desc(), t.c.created_at.desc(), t.c.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_out(res, r) for r in rows], total


async def _row(db: AsyncSession, res: Resource, user_id: uuid.UUID, row_id: str) -> Any:
    row = (
        await db.execute(
            select(res.table).where(_mine(res, user_id), res.table.c.id == row_id).limit(1)
        )
    ).first()
    if row is None:
        raise FinanceError(
            404, "not_found", f"{res.name[:-1] if res.name != 'secured' else 'entry'} not found"
        )
    return row


async def get_row(db: AsyncSession, res: Resource, user_id: uuid.UUID, row_id: str) -> BaseModel:
    return _out(res, await _row(db, res, user_id, row_id))


async def create_row(
    db: AsyncSession, res: Resource, user_id: uuid.UUID, body: BaseModel
) -> BaseModel:
    values = body.model_dump()
    row_id = values.pop("id", None) or uuid.uuid4().hex
    key = await owner_key(db, user_id)
    exists = await db.scalar(
        select(func.count())
        .select_from(res.table)
        .where(res.table.c.user_id == key, res.table.c.id == row_id)
    )
    if exists:
        raise FinanceError(409, "exists", f"an entry with id {row_id!r} already exists")
    now = _now()
    await db.execute(
        insert(res.table).values(
            **_to_db(res, values),
            id=row_id,
            user_id=key,
            aureon_user_id=user_id,
            created_at=now,
            updated_at=now,
        )
    )
    return await get_row(db, res, user_id, row_id)


async def patch_row(
    db: AsyncSession, res: Resource, user_id: uuid.UUID, row_id: str, body: BaseModel
) -> BaseModel:
    current = await _row(db, res, user_id, row_id)
    values = body.model_dump(exclude_unset=True)
    if res is EXPENSES:
        kind = values.get("kind", current.kind)
        day = values["recur_day"] if "recur_day" in values else current.recur_day
        if kind == "recurring" and day is None:
            raise FinanceError(422, "recur_day_required", "a recurring expense needs recur_day")
    if values:
        await db.execute(
            update(res.table)
            .where(_mine(res, user_id), res.table.c.id == row_id)
            .values(**_to_db(res, values), updated_at=_now())
        )
    return await get_row(db, res, user_id, row_id)


async def delete_row(db: AsyncSession, res: Resource, user_id: uuid.UUID, row_id: str) -> None:
    await _row(db, res, user_id, row_id)
    await db.execute(delete(res.table).where(_mine(res, user_id), res.table.c.id == row_id))


async def totals(
    db: AsyncSession, user_id: uuid.UUID, date_from: date | None, date_to: date | None
) -> s.FinanceTotals:
    async def agg(res: Resource, col: str) -> tuple[int, Decimal]:
        t = res.table
        conds = [_mine(res, user_id)]
        if date_from:
            conds.append(t.c[res.date_col] >= date_from)
        if date_to:
            conds.append(t.c[res.date_col] <= date_to)
        n, total = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(cast(t.c[col], Numeric)), 0)).where(
                    and_(*conds)
                )
            )
        ).one()
        return int(n), Decimal(format(Decimal(total).normalize(), "f")) if total else Decimal(0)

    n_t, pl = await agg(TRADES, "pl")
    n_e, ex = await agg(EXPENSES, "amount")
    n_s, sec = await agg(SECURED, "amt")
    return s.FinanceTotals(
        date_from=date_from,
        date_to=date_to,
        trades=n_t,
        trade_pl=pl,
        expenses=ex,
        expenses_count=n_e,
        secured=sec,
        secured_count=n_s,
    )


# --- documents --------------------------------------------------------------------------


def _doc_out(row: Any) -> s.DocumentOut:
    return s.DocumentOut(
        namespace=row.namespace, doc_id=row.doc_id, data=row.data, updated_at=row.updated_at
    )


async def list_documents(
    db: AsyncSession, user_id: uuid.UUID, namespace: str, limit: int, offset: int
) -> tuple[list[s.DocumentOut], int]:
    t = astra_documents
    where = and_(t.c.aureon_user_id == user_id, t.c.namespace == namespace)
    total = int(await db.scalar(select(func.count()).select_from(t).where(where)) or 0)
    rows = (
        await db.execute(
            select(t)
            .where(where)
            .order_by(t.c.updated_at.desc(), t.c.doc_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [_doc_out(r) for r in rows], total


async def _doc(db: AsyncSession, namespace: str, doc_id: str) -> Any:
    t = astra_documents
    return (
        await db.execute(select(t).where(t.c.namespace == namespace, t.c.doc_id == doc_id))
    ).first()


async def get_document(
    db: AsyncSession, user_id: uuid.UUID, namespace: str, doc_id: str
) -> s.DocumentOut:
    row = await _doc(db, namespace, doc_id)
    if row is None or row.aureon_user_id != user_id:
        raise FinanceError(404, "not_found", "document not found")
    return _doc_out(row)


async def put_document(
    db: AsyncSession, user_id: uuid.UUID, namespace: str, doc_id: str, body: s.DocumentIn
) -> s.DocumentOut:
    """Firestore setDoc: create, replace, or merge. A document owned by someone else (or
    not yet claimed) is never overwritten: that is 404, as if it did not exist."""
    t = astra_documents
    row = await _doc(db, namespace, doc_id)
    if row is not None and row.aureon_user_id != user_id:
        raise FinanceError(404, "not_found", "document not found")
    if row is None:
        await db.execute(
            pg_insert(t).values(
                namespace=namespace,
                doc_id=doc_id,
                user_id=await owner_key(db, user_id),
                aureon_user_id=user_id,
                data=body.data,
                updated_at=_now(),
            )
        )
    else:
        data = {**row.data, **body.data} if body.merge else body.data
        await db.execute(
            update(t)
            .where(t.c.namespace == namespace, t.c.doc_id == doc_id, t.c.aureon_user_id == user_id)
            .values(data=data, updated_at=_now())
        )
    return await get_document(db, user_id, namespace, doc_id)


async def delete_document(
    db: AsyncSession, user_id: uuid.UUID, namespace: str, doc_id: str
) -> None:
    await get_document(db, user_id, namespace, doc_id)
    t = astra_documents
    await db.execute(
        delete(t).where(
            t.c.namespace == namespace, t.c.doc_id == doc_id, t.c.aureon_user_id == user_id
        )
    )


# --- portfolio (public profile, admin-edited) ------------------------------------------------


async def get_portfolio(db: AsyncSession, profile_id: str) -> s.PortfolioOut:
    t = portfolio_profiles
    row = (await db.execute(select(t).where(t.c.id == profile_id))).first()
    if row is None:
        raise FinanceError(404, "not_found", "profile not found")
    return s.PortfolioOut(id=row.id, content=row.content, updated_at=row.updated_at)


async def put_portfolio(db: AsyncSession, profile_id: str, body: s.PortfolioIn) -> s.PortfolioOut:
    t = portfolio_profiles
    stmt = pg_insert(t).values(id=profile_id, content=body.content, updated_at=_now())
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[t.c.id], set_={"content": body.content, "updated_at": _now()}
        )
    )
    return await get_portfolio(db, profile_id)


# --- claim legacy rows ------------------------------------------------------------------------


async def claim(db: AsyncSession, body: s.ClaimIn, admin_id: uuid.UUID) -> s.ClaimOut:
    """Attach the rows a legacy owner id wrote (and nobody claimed yet) to an Aureon user.
    Dry run unless `apply`. A legacy id can belong to one Aureon user only."""
    linked = await db.scalar(
        select(FinanceIdentity).where(
            (FinanceIdentity.legacy_user_id == body.legacy_user_id)
            | (FinanceIdentity.user_id == body.user_id)
        )
    )
    if linked is not None and (
        linked.user_id != body.user_id or linked.legacy_user_id != body.legacy_user_id
    ):
        raise FinanceError(
            409, "already_linked", "that legacy id or user is already linked to someone else"
        )
    rows: dict[str, int] = {}
    for name in OWNED_TABLES:
        t = LEGACY.tables[f"public.{name}"]
        where = and_(t.c.user_id == body.legacy_user_id, t.c.aureon_user_id.is_(None))
        rows[name] = int(await db.scalar(select(func.count()).select_from(t).where(where)) or 0)
        if body.apply:
            await db.execute(update(t).where(where).values(aureon_user_id=body.user_id))
    if body.apply and linked is None:
        db.add(
            FinanceIdentity(
                user_id=body.user_id, legacy_user_id=body.legacy_user_id, linked_by=admin_id
            )
        )
    return s.ClaimOut(
        legacy_user_id=body.legacy_user_id, user_id=body.user_id, applied=body.apply, rows=rows
    )
