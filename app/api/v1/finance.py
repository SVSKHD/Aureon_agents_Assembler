"""Finance API (Prompt 3): the Spasta finance tables, scoped to the signed-in user.

    /api/finance/{trades,expenses,secured}           GET (paged, date range, filters), POST
    /api/finance/{trades,expenses,secured}/{id}      GET, PATCH, DELETE
    /api/finance/totals?date_from=&date_to=           exact sums for a range
    /api/finance/documents/{namespace}                GET (paged)
    /api/finance/documents/{namespace}/{doc_id}       GET, PUT (set / merge), DELETE
    /api/finance/portfolio-profiles/{id}              GET (any signed-in user), PUT (admin)
    /api/finance/claim                                admin: link a legacy owner id (dry run first)

Another user's row answers 404, exactly like a missing one, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel

from app.auth.deps import AdminRequired, CsrfProtected, Current, DbSession
from app.schemas import finance as s
from app.schemas.common import Page
from app.services import audit
from app.services import finance as svc
from app.services.audit import ClientInfo

router = APIRouter(prefix="/finance", tags=["finance"])

Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
RowIdPath = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
NsPath = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
DocPath = Annotated[str, Path(min_length=1, max_length=256)]


def _fail(exc: svc.FinanceError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message})


def _register(res: svc.Resource, in_model: type[BaseModel], patch_model: type[BaseModel]) -> None:
    out = res.out
    name = res.name

    async def list_(
        db: DbSession,
        current: Current,
        date_from: date | None = None,
        date_to: date | None = None,
        symbol: str | None = None,
        session: str | None = None,
        side: str | None = None,
        category: str | None = None,
        kind: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> Any:
        match = {
            k: v
            for k, v in {
                "symbol": symbol,
                "session": session,
                "side": side,
                "category": category,
                "kind": kind,
            }.items()
            if v is not None
        }
        items, total = await svc.list_rows(
            db,
            res,
            current.user.id,
            date_from=date_from,
            date_to=date_to,
            match=match,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def create(body: in_model, db: DbSession, current: CsrfProtected) -> Any:  # type: ignore[valid-type]
        try:
            row = await svc.create_row(db, res, current.user.id, body)
        except svc.FinanceError as exc:
            raise _fail(exc) from exc
        await db.commit()
        return row

    async def get(row_id: RowIdPath, db: DbSession, current: Current) -> Any:
        try:
            return await svc.get_row(db, res, current.user.id, row_id)
        except svc.FinanceError as exc:
            raise _fail(exc) from exc

    async def patch(
        row_id: RowIdPath,
        body: patch_model,  # type: ignore[valid-type]
        db: DbSession,
        current: CsrfProtected,
    ) -> Any:
        try:
            row = await svc.patch_row(db, res, current.user.id, row_id, body)
        except svc.FinanceError as exc:
            raise _fail(exc) from exc
        await db.commit()
        return row

    async def remove(row_id: RowIdPath, db: DbSession, current: CsrfProtected) -> Response:
        try:
            await svc.delete_row(db, res, current.user.id, row_id)
        except svc.FinanceError as exc:
            raise _fail(exc) from exc
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Bind the per-resource body models (string annotations cannot name closure types).
    create.__annotations__["body"] = in_model
    patch.__annotations__["body"] = patch_model

    router.add_api_route(
        f"/{name}",
        list_,
        methods=["GET"],
        response_model=Page[out],  # type: ignore[valid-type]
        name=f"list_{name}",
    )
    router.add_api_route(
        f"/{name}",
        create,
        methods=["POST"],
        response_model=out,
        status_code=status.HTTP_201_CREATED,
        name=f"create_{name}",
    )
    router.add_api_route(
        f"/{name}/{{row_id}}", get, methods=["GET"], response_model=out, name=f"get_{name}"
    )
    router.add_api_route(
        f"/{name}/{{row_id}}", patch, methods=["PATCH"], response_model=out, name=f"patch_{name}"
    )
    router.add_api_route(
        f"/{name}/{{row_id}}",
        remove,
        methods=["DELETE"],
        status_code=status.HTTP_204_NO_CONTENT,
        name=f"delete_{name}",
    )


_register(svc.TRADES, s.TradeIn, s.TradePatch)
_register(svc.EXPENSES, s.ExpenseIn, s.ExpensePatch)
_register(svc.SECURED, s.SecuredIn, s.SecuredPatch)


@router.get("/totals", response_model=s.FinanceTotals)
async def totals(
    db: DbSession, current: Current, date_from: date | None = None, date_to: date | None = None
) -> s.FinanceTotals:
    return await svc.totals(db, current.user.id, date_from, date_to)


@router.get("/documents/{namespace}", response_model=Page[s.DocumentOut])
async def list_documents(
    namespace: NsPath, db: DbSession, current: Current, limit: Limit = 50, offset: Offset = 0
) -> Page[s.DocumentOut]:
    items, total = await svc.list_documents(db, current.user.id, namespace, limit, offset)
    return Page[s.DocumentOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/documents/{namespace}/{doc_id}", response_model=s.DocumentOut)
async def get_document(
    namespace: NsPath, doc_id: DocPath, db: DbSession, current: Current
) -> s.DocumentOut:
    try:
        return await svc.get_document(db, current.user.id, namespace, doc_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc


@router.put("/documents/{namespace}/{doc_id}", response_model=s.DocumentOut)
async def put_document(
    namespace: NsPath, doc_id: DocPath, body: s.DocumentIn, db: DbSession, current: CsrfProtected
) -> s.DocumentOut:
    try:
        out = await svc.put_document(db, current.user.id, namespace, doc_id, body)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    await db.commit()
    return out


@router.delete("/documents/{namespace}/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    namespace: NsPath, doc_id: DocPath, db: DbSession, current: CsrfProtected
) -> Response:
    try:
        await svc.delete_document(db, current.user.id, namespace, doc_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/portfolio-profiles/{profile_id}", response_model=s.PortfolioOut)
async def get_portfolio(profile_id: RowIdPath, db: DbSession, _: Current) -> s.PortfolioOut:
    try:
        return await svc.get_portfolio(db, profile_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc


@router.put("/portfolio-profiles/{profile_id}", response_model=s.PortfolioOut)
async def put_portfolio(
    profile_id: RowIdPath,
    body: s.PortfolioIn,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
) -> s.PortfolioOut:
    out = await svc.put_portfolio(db, profile_id, body)
    audit.record(
        db,
        "portfolio_profile_updated",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"profile": profile_id},
    )
    await db.commit()
    return out


@router.post("/claim", response_model=s.ClaimOut)
async def claim(
    body: s.ClaimIn, request: Request, db: DbSession, admin: AdminRequired
) -> s.ClaimOut:
    admin_id: uuid.UUID = admin.user.id
    try:
        out = await svc.claim(db, body, admin_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    if body.apply:
        audit.record(
            db,
            "finance_rows_claimed",
            ClientInfo.from_request(request),
            actor_user_id=admin_id,
            details={
                "legacy_user_id": body.legacy_user_id,
                "user_id": str(body.user_id),
                "rows": out.rows,
            },
        )
    await db.commit()
    return out
