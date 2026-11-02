"""Flatten an app's routes, including those behind included routers.

FastAPI >= 0.16x keeps included routers as lazy `_IncludedRouter` entries in `app.routes`,
so iterating `app.routes` alone no longer reaches the endpoints.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


@dataclass(frozen=True)
class RouteInfo:
    methods: frozenset[str]
    path: str
    deps: frozenset[str]
    route: APIRoute


def _dep_names(dependant: Any, acc: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        acc.add(getattr(sub.call, "__name__", repr(sub.call)))
        _dep_names(sub, acc)
    return acc


def _walk(routes: Iterable[Any], prefix: str) -> Iterator[tuple[str, Any]]:
    for r in routes:
        if hasattr(r, "original_router"):  # an included router
            yield from _walk(r.original_router.routes, prefix + r.include_context.prefix)
        else:
            yield prefix, r


def api_routes(app: FastAPI) -> list[RouteInfo]:
    out = []
    for prefix, r in _walk(app.router.routes, ""):
        if isinstance(r, APIRoute):
            out.append(
                RouteInfo(
                    frozenset(r.methods),
                    prefix + r.path,
                    frozenset(_dep_names(r.dependant, set())),
                    r,
                )
            )
    return out


def other_paths(app: FastAPI) -> list[str]:
    """Paths of non-APIRoute entries (Swagger, openapi.json, mounts)."""
    return [
        prefix + getattr(r, "path", "")
        for prefix, r in _walk(app.router.routes, "")
        if not isinstance(r, APIRoute)
    ]
