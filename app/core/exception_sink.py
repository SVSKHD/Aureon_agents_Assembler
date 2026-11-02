"""In-process capture of unhandled exceptions for incident detection (docs/21-self-repair.md).

A structlog processor (`capture_exceptions`, in both the structlog and the stdlib chain, so
uvicorn's "Exception in ASGI application" and asyncio's "Task exception was never retrieved"
are seen too) and `sys.excepthook` hand every logged exception to `SINK`. The sink only
aggregates in memory: no I/O happens inside a log call. A background flusher
(app/services/incidents.py) writes due entries to agents.incidents.

- Fingerprint: the exception type plus the innermost frame inside the `app` package
  (module:function), so the same bug in the same place is one incident however often it
  fires.
- Rate limit: one write per fingerprint per INCIDENT_EXCEPTION_RATE_SECONDS; repeats in
  between only bump the count. At most MAX_ENTRIES distinct fingerprints are held; beyond
  that new ones are dropped and counted.
- Redaction: the message goes through the log scrubber; locals are never read.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
import traceback
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from app.core.logging import scrub

MAX_ENTRIES = 200
MAX_MESSAGE = 500
MAX_FRAMES = 8
# Exceptions raised inside these modules come from the repair system itself: they are
# recorded, but never turned into repair tasks (the recursion guard).
SELF_MODULES = ("app.services.repair", "app.services.incidents")


@dataclass
class Captured:
    fingerprint: str
    title: str
    details: dict[str, Any]
    count: int = 1
    pending: int = 1
    last_flush: float = 0.0
    first_seen: float = field(default_factory=time.time)


def _frames(tb: TracebackType | None) -> list[dict[str, Any]]:
    out = []
    for frame, line in traceback.walk_tb(tb):
        module = str(frame.f_globals.get("__name__") or "")
        if module == "app" or module.startswith("app."):
            out.append({"module": module, "function": frame.f_code.co_name, "line": line})
    return out


def _file(module: str) -> str:
    return module.replace(".", "/") + ".py"


def describe(exc: BaseException, logger_event: str | None = None) -> Captured:
    """Fingerprint + redacted details of one exception (no locals, no I/O)."""
    frames = _frames(exc.__traceback__)
    top = frames[-1] if frames else None
    etype = f"{type(exc).__module__}.{type(exc).__qualname__}".removeprefix("builtins.")
    where = f"{top['module']}:{top['function']}" if top else "<no app frame>"
    fp = hashlib.sha256(f"exception|{etype}|{where}".encode()).hexdigest()[:40]
    message = scrub(str(exc))[:MAX_MESSAGE]
    details: dict[str, Any] = {
        "type": etype,
        "message": message,
        "event": scrub(logger_event)[:200] if logger_event else None,
        "module": top["module"] if top else None,
        "self_origin": any(f["module"].startswith(SELF_MODULES) for f in frames),
        "frames": [f"{_file(f['module'])}:{f['line']} {f['function']}" for f in frames][
            -MAX_FRAMES:
        ],
    }
    if top:
        details["location"] = {
            "file": _file(top["module"]),
            "line": top["line"],
            "function": top["function"],
        }
    title = f"{etype.rsplit('.', 1)[-1]} in {where}"[:300]
    return Captured(fp, title, details)


class ExceptionSink:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self.entries: dict[str, Captured] = {}
        self.dropped = 0
        self._lock = threading.Lock()

    def add(self, exc: BaseException, logger_event: str | None = None) -> None:
        try:
            cap = describe(exc, logger_event)
        except Exception:  # never let capture break logging
            return
        with self._lock:
            cur = self.entries.get(cap.fingerprint)
            if cur is not None:
                cur.count += 1
                cur.pending += 1
                cur.details = cap.details  # latest line numbers / message
                return
            if len(self.entries) >= self.max_entries:
                self.dropped += 1
                return
            self.entries[cap.fingerprint] = cap

    def due(self, rate_seconds: float, now: float | None = None) -> list[Captured]:
        """Entries with unflushed occurrences whose last write is older than the rate limit.
        Marks them flushed (pending=0) before returning copies."""
        now = time.time() if now is None else now
        out: list[Captured] = []
        with self._lock:
            for cap in self.entries.values():
                if cap.pending and now - cap.last_flush >= rate_seconds:
                    out.append(
                        Captured(
                            cap.fingerprint, cap.title, dict(cap.details), cap.count, cap.pending
                        )
                    )
                    cap.pending = 0
                    cap.last_flush = now
        return out

    def requeue(self, items: list[Captured]) -> None:
        """A failed flush: put the occurrences back so they are written next time."""
        with self._lock:
            for item in items:
                cur = self.entries.get(item.fingerprint)
                if cur is not None:
                    cur.pending += item.pending
                    cur.last_flush = 0.0

    def clear(self) -> None:
        with self._lock:
            self.entries.clear()
            self.dropped = 0


SINK = ExceptionSink()


def _exc_from(value: Any) -> BaseException | None:
    if isinstance(value, BaseException):
        return value
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], BaseException):
        return value[1]
    if value is True:
        return sys.exc_info()[1]
    return None


def capture_exceptions(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: hand the event's exception (if any) to the sink. Must run before
    format_exc_info, which replaces exc_info with text."""
    exc = _exc_from(event_dict.get("exc_info"))
    if exc is not None and not isinstance(exc, KeyboardInterrupt | SystemExit):
        SINK.add(exc, str(event_dict.get("event") or "") or None)
    return event_dict


_installed = False


def install_excepthook() -> None:
    """Chain sys.excepthook so a crash that kills a worker is captured too (idempotent)."""
    global _installed
    if _installed:
        return
    previous = sys.excepthook

    def hook(etype: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        if not isinstance(value, KeyboardInterrupt | SystemExit):
            SINK.add(value, "uncaught")
        previous(etype, value, tb)

    sys.excepthook = hook
    _installed = True
