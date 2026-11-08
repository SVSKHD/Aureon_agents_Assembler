"""Record a running deploy's progress (docs/33-deploy-progress.md).

scripts/ops/deploy-poll.sh pipes one aureon.deploy.progress/v1 JSON document into this module
while a deploy runs (`docker compose exec -T api ...`, every few seconds, best effort) and once
more when it ends (`docker compose run --rm ...`):

    {"schema": "aureon.deploy.progress/v1", "run_id": 42, "target": "backend",
     "stages": [{"name": "building", "status": "running", "at": "...", "detail": null}],
     "sha": "<commit the script checked out>", "pr": 46, "log": "<this deploy's lines>",
     "log_truncated": false}

Every report carries the whole stage list, so one that is missed (the API restarting in the
middle of a backend deploy) is healed by the next. The log is scrubbed of known secrets,
credentials and one-time ids before it is stored. Exit codes: 0 recorded (or nothing to do),
2 invalid input, 1 storage failure. Never blocks or fails the deploy.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger, register_secret_values
from app.db.session import build_engine, build_sessionmaker
from app.services import app_settings as svc
from app.services import deploy_progress

log = get_logger("workers.deploy_progress")
MAX_INPUT = 1024 * 1024


def parse(raw: bytes) -> deploy_progress.ProgressReport:
    if len(raw) > MAX_INPUT:
        raise ValueError("input over 1 MiB")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError("input is not JSON") from exc
    try:
        return deploy_progress.ProgressReport.model_validate(doc)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})[:10]
        raise ValueError(f"invalid progress report: {', '.join(fields)}") from None


async def record(report: deploy_progress.ProgressReport) -> dict[str, Any]:
    settings = get_settings()
    # The log is scrubbed of every credential this process knows (as the API's own logs are).
    register_secret_values(svc.secret_values(settings))
    engine = build_engine(settings)
    try:
        async with build_sessionmaker(engine)() as db, db.begin():
            return await deploy_progress.apply_report(db, settings, report)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        report = parse(raw)
    except ValueError as exc:
        log.error("deploy_progress_rejected", reason=str(exc))
        return 2
    try:
        out = asyncio.run(record(report))
    except Exception as exc:
        log.error("deploy_progress_failed", error=type(exc).__name__)
        return 1
    log.info("deploy_progress_recorded", run_id=report.run_id, target=report.target, **out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
