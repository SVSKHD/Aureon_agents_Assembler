"""Record one host runtime observation (docs/32-devops-monitoring.md, "Runtime collector").

The host-side collector (scripts/ops/runtime-collect.sh, run by aureon-runtime-collect.timer)
reads `docker compose ps` / `docker inspect` and the API's readiness ON THE HOST, then pipes
one aureon.runtime/v1 JSON document into this module inside the API image:

    ... | docker compose exec -T api python -m app.workers.runtime_record

So the Docker socket is only ever touched by the host user that already runs deploys; the API
gains no Docker access and no new endpoint or token. The document is validated (size cap,
schema, field limits) and written to devops.services / devops.service_snapshots; a status
change emits service.unhealthy / service.recovered once. The `runtime-collector` agent is
heartbeated, so a collector that stops reporting shows up as stale in the agent monitor and
the dashboard marks its services UNKNOWN after DEVOPS_RUNTIME_STALE_SECONDS.

Exit codes: 0 recorded, 2 invalid input, 1 storage failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from pydantic import ValidationError

from app.agents.registry import AgentRegistry, AgentSpec
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import build_engine, build_sessionmaker
from app.services import devops

log = get_logger("workers.runtime_record")
COLLECTOR = AgentSpec("runtime-collector", "Runtime collector", "monitor")
MAX_INPUT = 1024 * 1024
STALE_AFTER_S = 300


def parse(raw: bytes) -> devops.RuntimeReport:
    if len(raw) > MAX_INPUT:
        raise ValueError("input over 1 MiB")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError("input is not JSON") from exc
    try:
        return devops.RuntimeReport.model_validate(doc)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})[:10]
        raise ValueError(f"invalid runtime report: {', '.join(fields)}") from None


async def record(report: devops.RuntimeReport) -> dict[str, Any]:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        maker = build_sessionmaker(engine)
        registry = AgentRegistry(maker)
        await registry.register(COLLECTOR, {"stale_after_s": STALE_AFTER_S, "host": report.host})
        async with maker() as db, db.begin():
            out = await devops.record_runtime(db, settings, report)
        await registry.heartbeat(COLLECTOR.id, "idle")
        return out
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        report = parse(raw)
    except ValueError as exc:
        log.error("runtime_record_rejected", reason=str(exc))
        return 2
    try:
        out = asyncio.run(record(report))
    except Exception as exc:
        log.error("runtime_record_failed", error=type(exc).__name__)
        return 1
    log.info("runtime_recorded", host=report.host, **out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
