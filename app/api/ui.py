"""Self-describing API: `x-ui` metadata on every route (docs/ui-manifest.md).

Each router module declares its UI module once:

    UI_MODULE = ui_module("news", label="News", group="markets", icon="newspaper", order=40)

and a route can override any field:

    @router.post("/refresh", openapi_extra=ui_meta(label="Refresh now", kind="action"))

Everything not set is derived from the route: kind from the method/path/response model,
list columns and the id/title fields from the response model, roles from the auth
dependencies, the label from the summary or function name.

Safety floor (cannot be lowered by explicit metadata): routes that touch secrets, settings,
LIVE mode, approvals, merges/pushes/deploys or order execution are `approval` or `hidden`;
machine endpoints (webhooks, the MT5 push) and auth flows are `hidden`; any other unsafe
method is at least `confirm`. `hidden` routes never appear in the manifest, and admin-only
routes appear only for admins.

`describe_routes(app)` is the single source for the OpenAPI extension, the manifest and the
schema hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import typing
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

Kind = Literal["list", "detail", "form", "action", "stream"]
Danger = Literal["none", "confirm", "approval", "hidden"]
Role = Literal["public", "user", "admin"]
DANGER_RANK: dict[str, int] = {"none": 0, "confirm": 1, "approval": 2, "hidden": 3}
KINDS = ("list", "detail", "form", "action", "stream")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
FIELDS = (
    "module", "label", "kind", "columns", "title_field", "id_field", "group", "icon",
    "roles", "danger", "order",
)  # fmt: skip
MAX_COLUMNS = 6

# (method, path regex, floor, reason). First match wins; "*" = any method, "UNSAFE" = any
# writing method. These floors are hard: explicit metadata can raise them, never lower them.
SAFETY_RULES: tuple[tuple[str, str, Danger, str], ...] = (
    # Machine-to-machine and auth flows: never a UI button.
    ("*", r"^/api/v1/github/webhooks$", "hidden", "machine endpoint (HMAC)"),
    ("*", r"^/api/trading/mt5/ingest$", "hidden", "machine endpoint (bearer)"),
    ("*", r"^/api/auth/", "hidden", "authentication flow"),
    ("*", r"^/api/v1/dhan/auth/callback", "hidden", "OAuth-style callback"),
    # Secrets and settings: handcrafted pages only.
    ("*", r"^/api/v1/settings", "hidden", "secrets and settings"),
    ("*", r"^/api/v1/dhan/auth/(start|consume|session)", "hidden", "broker credentials"),
    # LIVE mode and anything that could reach execution.
    ("*", r"/mode(/|$)", "hidden", "execution mode (LIVE)"),
    ("*", r"live", "hidden", "LIVE trading"),
    ("POST", r"^/api/trading/order-intents/[^/]+/approve$", "approval", "order execution"),
    ("POST", r"^/api/trading/order-intents$", "approval", "order intent"),
    ("UNSAFE", r"/paper/", "approval", "paper execution"),
    # Approvals and gated operations.
    ("POST", r"^/api/v1/approvals/[^/]+/(approve|deny)$", "approval", "approval decision"),
    ("*", r"/merge$", "approval", "merge"),
    ("*", r"/git/push$", "hidden", "git push"),
    ("*", r"/force-push$", "hidden", "force push"),
    ("UNSAFE", r"/branch-protection/", "approval", "branch protection"),
    ("UNSAFE", r"/repositories/[^/]+/[^/]+/settings$", "approval", "repository settings"),
    ("UNSAFE", r"/actions-permissions$", "approval", "CI permissions"),
    ("DELETE", r"/branches/", "approval", "branch deletion"),
    ("*", r"^/api/v1/approvals/(policies|reauth)", "hidden", "approval policy / re-auth"),
    # Read-only monitoring of deploys (docs/32): reading history is not deploying.
    ("GET", r"^/api/v1/devops/", "none", "read-only monitoring"),
    ("GET", r"^/api/v1/github/actions/monitor(/|$)", "none", "read-only monitoring"),
    ("*", r"deploy", "hidden", "deploy"),
    ("POST", r"^/api/code/tasks/[^/]+/(approve|reject)$", "approval", "code change approval"),
    ("POST", r"^/api/v1/repairs/[^/]+/(approve|reject|apply)", "approval", "repair approval"),
    ("POST", r"/actions/.+/(dispatch|rerun|cancel|enable|disable)", "approval", "CI control"),
    ("*", r"^/api/agents/[^/]+/(pause|resume)$", "confirm", "agent control"),
    ("*", r"^/api/scheduler/jobs/[^/]+/(pause|resume|run)$", "confirm", "scheduler control"),
)


@dataclass(frozen=True, slots=True)
class ModuleMeta:
    module: str
    label: str
    group: str
    icon: str
    order: int


def ui_module(module: str, *, label: str, group: str, icon: str, order: int) -> ModuleMeta:
    """Declare the UI module of every route in this router module (one per file)."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,40}", module):
        raise ValueError(f"bad UI module name {module!r}")
    return ModuleMeta(module, label, group, icon, order)


def ui_meta(
    *,
    module: str | None = None,
    label: str | None = None,
    kind: Kind | None = None,
    columns: list[str] | None = None,
    title_field: str | None = None,
    id_field: str | None = None,
    group: str | None = None,
    icon: str | None = None,
    roles: list[Role] | None = None,
    danger: Danger | None = None,
    order: int | None = None,
    hidden: bool = False,
) -> dict[str, Any]:
    """`openapi_extra=ui_meta(...)` on a route: explicit `x-ui` fields (others are derived).
    `hidden=True` keeps the route out of every manifest (same as danger="hidden")."""
    meta: dict[str, Any] = {
        k: v
        for k, v in {
            "module": module,
            "label": label,
            "kind": kind,
            "columns": columns,
            "title_field": title_field,
            "id_field": id_field,
            "group": group,
            "icon": icon,
            "roles": roles,
            "danger": "hidden" if hidden else danger,
            "order": order,
        }.items()
        if v is not None
    }
    return {"x-ui": meta}


# --- deriving ------------------------------------------------------------------------------


def _walk(routes: Iterable[Any], prefix: str) -> Iterator[tuple[str, Any]]:
    for r in routes:
        if hasattr(r, "original_router"):  # an included router (lazy in newer FastAPI)
            yield from _walk(r.original_router.routes, prefix + r.include_context.prefix)
        else:
            yield prefix, r


_CONVERTER = re.compile(r"\{(\w+):[^}]+\}")


def iter_routes(app: FastAPI) -> Iterator[tuple[str, APIRoute]]:
    """(OpenAPI path, route): `{path:path}` is written `{path}`, as in the OpenAPI document."""
    for prefix, r in _walk(app.router.routes, ""):
        if isinstance(r, APIRoute) and r.include_in_schema:
            yield _CONVERTER.sub(r"{\1}", prefix + r.path), r


def _dep_names(dependant: Any, acc: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        acc.add(getattr(sub.call, "__name__", ""))
        _dep_names(sub, acc)
    return acc


def _roles(route: APIRoute) -> list[Role]:
    deps = _dep_names(route.dependant, set())
    if "require_admin" in deps:
        return ["admin"]
    if deps & {"get_current", "require_csrf"}:
        return ["user", "admin"]
    return ["public", "user", "admin"]


def module_of(route: APIRoute) -> ModuleMeta | None:
    mod = sys.modules.get(getattr(route.endpoint, "__module__", ""))
    meta = getattr(mod, "UI_MODULE", None)
    return meta if isinstance(meta, ModuleMeta) else None


def _model(annotation: Any) -> tuple[type[BaseModel] | None, bool]:
    """(item model, is_list) of a response model: list[X], Page[X]/{items: list[X]}, or X."""
    if annotation is None:
        return None, False
    origin = typing.get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(annotation)
        inner = args[0] if args else None
        return (inner if isinstance(inner, type) and issubclass(inner, BaseModel) else None), True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        items = annotation.model_fields.get("items")
        if items is not None and typing.get_origin(items.annotation) is list:
            (inner,) = typing.get_args(items.annotation) or (None,)
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return inner, True
        return annotation, False
    return None, False


def _scalar(field: Any) -> bool:
    ann = field.annotation
    args = typing.get_args(ann) or (ann,)
    return all(
        a is type(None)
        or (isinstance(a, type) and not issubclass(a, (BaseModel, list, dict, tuple, set)))
        or typing.get_origin(a) is Literal
        for a in args
    )


def _first(names: Iterable[str], candidates: tuple[str, ...]) -> str | None:
    have = list(names)
    return next((c for c in candidates if c in have), None)


def _kind(method: str, path: str, route: APIRoute, is_list: bool) -> Kind:
    if path.endswith("/stream") or path.endswith("/events"):
        return "stream"
    if method == "GET":
        if is_list:
            return "list"
        return "detail" if path.endswith("}") else ("list" if is_list else "detail")
    if method in ("POST", "PUT", "PATCH") and route.body_field is not None:
        return "form"
    return "action"


def _label(route: APIRoute) -> str:
    if route.summary:
        return route.summary
    return route.name.replace("_", " ").strip().capitalize()


def _floor(method: str, path: str) -> tuple[Danger, str | None]:
    """The hard floor from SAFETY_RULES ("none" when no rule matches)."""
    for m, pattern, danger, reason in SAFETY_RULES:
        hit = m == "*" or m == method or (m == "UNSAFE" and method in UNSAFE)
        if hit and re.search(pattern, path):
            return danger, reason
    return "none", None


def describe(path: str, method: str, route: APIRoute) -> dict[str, Any] | None:
    """The full x-ui for one operation, or None when the route declares nothing (CI fails)."""
    explicit: dict[str, Any] = dict((route.openapi_extra or {}).get("x-ui") or {})
    mod = module_of(route)
    if mod is None and "module" not in explicit and explicit.get("danger") != "hidden":
        return None
    item, is_list = _model(route.response_model)
    names = list(item.model_fields) if item is not None else []
    derived: dict[str, Any] = {
        "module": mod.module if mod else explicit.get("module", "hidden"),
        "label": _label(route),
        "kind": _kind(method, path, route, is_list),
        "group": mod.group if mod else None,
        "icon": mod.icon if mod else None,
        "roles": _roles(route),
        "order": mod.order if mod else 999,
        "id_field": _first(names, ("id", "key", "number", "full_name", "name", "symbol")),
        "title_field": _first(
            names, ("title", "name", "label", "full_name", "key", "summary", "symbol", "email")
        ),
        "columns": (
            [n for n, f in item.model_fields.items() if _scalar(f)][:MAX_COLUMNS]
            if item is not None and is_list
            else None
        ),
    }
    meta = {**derived, **{k: v for k, v in explicit.items() if v is not None}}
    floor, reason = _floor(method, path)
    # Default: a writing method asks for confirmation unless the route says otherwise.
    asked = explicit.get("danger") or ("confirm" if method in UNSAFE else "none")
    meta["danger"] = asked if DANGER_RANK[asked] >= DANGER_RANK[floor] else floor
    if reason and DANGER_RANK[meta["danger"]] > 0 and DANGER_RANK[floor] > 0:
        meta["danger_reason"] = reason
    if meta["kind"] not in KINDS:
        raise ValueError(f"{method} {path}: bad x-ui kind {meta['kind']!r}")
    return meta


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    operation_id: str
    ui: dict[str, Any] | None
    module: ModuleMeta | None


def describe_routes(app: FastAPI) -> list[Operation]:
    out: list[Operation] = []
    for path, route in iter_routes(app):
        mod = module_of(route)
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append(Operation(method, path, route.unique_id, describe(path, method, route), mod))
    return out


def undeclared(app: FastAPI) -> list[str]:
    """Routes with no x-ui: no UI_MODULE in their router module, no per-route metadata and
    not marked hidden. The CI test requires this to be empty."""
    return [f"{op.method} {op.path}" for op in describe_routes(app) if op.ui is None]


def install_openapi(app: FastAPI) -> None:
    """Put each operation's x-ui into the OpenAPI document (cached like FastAPI's own)."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        for op in describe_routes(app):
            node = schema.get("paths", {}).get(op.path, {}).get(op.method.lower())
            if node is not None and op.ui is not None:
                node["x-ui"] = op.ui
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def schema_hash(app: FastAPI) -> str:
    """sha256 of the canonical OpenAPI document (routes, models and x-ui): changes whenever
    anything the frontend could wire changes."""
    text = json.dumps(app.openapi(), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def manifest(app: FastAPI, *, is_admin: bool, schema: str) -> dict[str, Any]:
    """Modules and their operations for this viewer: hidden ones never, admin-only ones only
    for admins."""
    modules: dict[str, dict[str, Any]] = {}
    for op in describe_routes(app):
        ui = op.ui
        if ui is None or ui["danger"] == "hidden":
            continue
        if not is_admin and "user" not in ui["roles"] and "public" not in ui["roles"]:
            continue
        mod = modules.setdefault(
            ui["module"],
            {
                "module": ui["module"],
                "label": op.module.label if op.module else ui["module"].title(),
                "group": ui.get("group"),
                "icon": ui.get("icon"),
                "order": ui.get("order", 999),
                "operations": [],
            },
        )
        mod["operations"].append(
            {"method": op.method, "path": op.path, "operation_id": op.operation_id, "x-ui": ui}
        )
    ordered = sorted(modules.values(), key=lambda m: (m["order"], m["module"]))
    return {"schema_hash": schema, "modules": ordered}


# --- schema change events ------------------------------------------------------------------

SCHEMA_KEY = "aureon:api:schema"
SCHEMA_CHANGED_WINDOW_S = 600


async def announce_schema(redis: Any, current: str) -> bool:
    """At API start: record this build's schema hash. When it differs from the last one, also
    record when it changed, so streams can send `schema_changed` (app/services/trading_stream).
    Best effort: Redis down only means no event. Returns True when the schema changed."""
    import time

    try:
        previous = await redis.hget(SCHEMA_KEY, "hash")
        if isinstance(previous, bytes):
            previous = previous.decode()
        if previous == current:
            return False
        await redis.hset(
            SCHEMA_KEY,
            mapping={"hash": current, "previous": previous or "", "changed_at": str(time.time())},
        )
        return previous is not None
    except Exception:
        return False


async def schema_state(redis: Any) -> dict[str, str]:
    """{"hash", "previous", "changed_at"} as last announced (empty when unknown)."""
    try:
        raw = await redis.hgetall(SCHEMA_KEY)
    except Exception:
        return {}
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in (raw or {}).items()
    }
