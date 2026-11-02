"""Finance API (Prompt 3): the Spasta finance tables, scoped to the signed-in user.

    /api/finance/{trades,expenses,secured}           GET (paged, date range, filters), POST
    /api/finance/{trades,expenses,secured}/{id}      GET, PATCH, DELETE
    /api/finance/totals?date_from=&date_to=           exact sums for a range
    /api/finance/documents/{namespace}                GET (paged)
    /api/finance/documents/{namespace}/{doc_id}       GET, PUT (set / merge), DELETE
    /api/finance/portfolio-profiles/{id}              GET (any signed-in user), PUT (admin)
    /api/finance/claim                                admin: link a legacy owner id (dry run first)
    /api/finance/changes?since=                       change sync: rows, documents, deletes
    /api/finance/attachments[?kind=&ref=]             GET list, POST (multipart upload)
    /api/finance/attachments/{id}[/file]              GET metadata / file, DELETE

List endpoints take `updated_since` (rows with updated_at > it).

Another user's row answers 404, exactly like a missing one, so ids cannot be probed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.deps import AdminRequired, CsrfProtected, Current, DbSession, SettingsDep
from app.models.finance import FinanceAttachment
from app.schemas import finance as s
from app.schemas.common import Page
from app.services import audit
from app.services import finance as svc
from app.services import finance_attachments as att
from app.services.audit import ClientInfo
from app.services.health import check_data_root

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
        updated_since: datetime | None = None,
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
            updated_since=updated_since,
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

    async def remove(
        row_id: RowIdPath, db: DbSession, current: CsrfProtected, settings: SettingsDep
    ) -> Response:
        try:
            files = await svc.delete_row(db, res, current.user.id, row_id)
        except svc.FinanceError as exc:
            raise _fail(exc) from exc
        await db.commit()
        if files:
            await asyncio.to_thread(att.unlink_files, settings, files)
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
    namespace: NsPath,
    db: DbSession,
    current: Current,
    updated_since: datetime | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[s.DocumentOut]:
    items, total = await svc.list_documents(
        db, current.user.id, namespace, limit, offset, updated_since
    )
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


# --- change sync ----------------------------------------------------------------------------


@router.get("/changes", response_model=s.ChangesOut)
async def changes(
    since: datetime, db: DbSession, current: Current, settings: SettingsDep
) -> s.ChangesOut:
    """Rows, documents and deletes changed after `since`. Store `cursor`, send it next time."""
    if since.tzinfo is None:
        raise HTTPException(422, {"code": "naive_since", "detail": "since needs a UTC offset"})
    return await svc.changes(db, current.user.id, since, settings.FINANCE_CHANGES_MAX_ROWS)


# --- receipt attachments ----------------------------------------------------------------------


def _att_out(row: Any) -> s.AttachmentOut:
    return s.AttachmentOut.model_validate(row)


@router.post("/attachments", response_model=s.AttachmentOut, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
    kind: Annotated[s.AttachmentKind, Form()],
    ref: Annotated[str, Form(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")],
    file: Annotated[UploadFile, File()],
) -> s.AttachmentOut:
    user_id: uuid.UUID = current.user.id
    try:
        await asyncio.to_thread(check_data_root, settings)
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "attachment storage is not mounted"
        ) from exc
    try:
        await att.prepare(db, settings, user_id, kind, ref)
        chunks, size, content_type, sha = await att.read_upload(
            file, settings.FINANCE_ATTACHMENT_MAX_BYTES
        )
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    attachment_id = uuid.uuid4()
    rel = await asyncio.to_thread(att.store, settings, user_id, attachment_id, chunks)
    row = FinanceAttachment(
        id=attachment_id,
        aureon_user_id=user_id,
        kind=kind,
        ref=ref,
        filename=att.clean_filename(file.filename),
        content_type=content_type,
        size_bytes=size,
        sha256=sha,
        storage_path=rel,
    )
    db.add(row)
    audit.record(
        db,
        "finance_attachment_uploaded",
        ClientInfo.from_request(request),
        actor_user_id=user_id,
        details={"id": str(attachment_id), "kind": kind, "ref": ref, "size": size},
    )
    try:
        await db.commit()
    except Exception:
        await asyncio.to_thread(att.unlink_files, settings, [rel])
        raise
    await db.refresh(row)
    return _att_out(row)


@router.get("/attachments", response_model=list[s.AttachmentOut])
async def list_attachments(
    db: DbSession,
    current: Current,
    kind: s.AttachmentKind | None = None,
    ref: Annotated[str | None, Query(max_length=128)] = None,
) -> list[s.AttachmentOut]:
    return [_att_out(r) for r in await att.list_for(db, current.user.id, kind, ref)]


@router.get("/attachments/{attachment_id}", response_model=s.AttachmentOut)
async def get_attachment(
    attachment_id: uuid.UUID, db: DbSession, current: Current
) -> s.AttachmentOut:
    try:
        return _att_out(await att.get(db, current.user.id, attachment_id))
    except svc.FinanceError as exc:
        raise _fail(exc) from exc


@router.get("/attachments/{attachment_id}/file", response_class=FileResponse)
async def attachment_file(
    attachment_id: uuid.UUID, db: DbSession, current: Current, settings: SettingsDep
) -> FileResponse:
    try:
        row = await att.get(db, current.user.id, attachment_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    path = await asyncio.to_thread(att.safe_file, settings, row.storage_path)
    if path is None:
        raise HTTPException(404, {"code": "file_missing", "detail": "attachment file not found"})
    return FileResponse(
        path,
        media_type=row.content_type,
        filename=row.filename,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: uuid.UUID,
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> Response:
    try:
        row = await att.get(db, current.user.id, attachment_id)
    except svc.FinanceError as exc:
        raise _fail(exc) from exc
    rel = row.storage_path
    await db.delete(row)
    audit.record(
        db,
        "finance_attachment_deleted",
        ClientInfo.from_request(request),
        actor_user_id=current.user.id,
        details={"id": str(attachment_id), "kind": row.kind, "ref": row.ref},
    )
    await db.commit()
    await asyncio.to_thread(att.unlink_files, settings, [rel])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
