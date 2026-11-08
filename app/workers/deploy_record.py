"""Record host deploys as agent runs of the `deployer` agent (docs/deploy-automation.md).

The host poller (scripts/ops/deploy-poll.sh) calls this in a one-shot api container around
every deploy, so Spasta's agent monitor shows deploy status and history:

    python -m app.workers.deploy_record start --target backend --sha <new> --from <old> \
        --log /mnt/spasta-data/logs/deploy.log            # prints the run id
    python -m app.workers.deploy_record finish --run-id 42 --status succeeded
    python -m app.workers.deploy_record finish --run-id 42 --status failed --error "health"
    python -m app.workers.deploy_record finish --run-id 42 --status failed --exit-code 1 \
        --phases-json '{"build_started": "2026-11-08T10:00:00Z", "failure": "readiness"}'

Only ids, SHAs and the log path are stored (runs are visible to every user). Best effort by
design: a recording failure exits non-zero but must never block or fail the deploy itself.

Each deploy is also a devops.deployments row (docs/32-devops-monitoring.md): repo and branch
from DEPLOY_TARGET_REPOS, the phase timestamps deploy.sh wrote (--phases-json), and the
outcome from its exit code (--exit-code; derived from --status / --rolled-back when absent).
It is `succeeded` only when a health verification (`health_ok`) was recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from app.agents.registry import AgentRegistry, AgentSpec
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import build_engine, build_sessionmaker
from app.services import devops

log = get_logger("workers.deploy_record")

DEPLOYER = AgentSpec("deployer", "Host deployer", "deploy")
TARGETS = ("backend", "frontend")
# Deploys are rare: without this the agent would show "stale" between them.
STALE_AFTER_S = 35 * 24 * 3600
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_LOG = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")
MAX_ERROR = 500
MAX_PHASES_JSON = 4096
_ENV = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def _env(value: str) -> str:
    if not _ENV.match(value):
        raise argparse.ArgumentTypeError("expected an environment name ([A-Za-z0-9._-])")
    return value


def _phases(value: str) -> dict[str, Any]:
    if len(value) > MAX_PHASES_JSON:
        raise argparse.ArgumentTypeError("phases JSON is too long")
    try:
        raw = json.loads(value) if value.strip() else {}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("phases must be a JSON object") from exc
    if not isinstance(raw, dict):
        raise argparse.ArgumentTypeError("phases must be a JSON object")
    return devops.parse_phases(raw)


def _sha(value: str) -> str:
    if not _SHA.match(value):
        raise argparse.ArgumentTypeError("expected a hex git SHA (7-40 chars)")
    return value


def _sha_or_none(value: str) -> str | None:
    return None if value in ("", "none") else _sha(value)


def _log_path(value: str) -> str:
    if not _LOG.match(value) or ".." in value:
        raise argparse.ArgumentTypeError("expected an absolute log path")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.workers.deploy_record")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="open a running deploy run; prints its id")
    s.add_argument("--target", choices=TARGETS, required=True)
    s.add_argument("--sha", type=_sha, required=True)
    s.add_argument("--from", dest="from_sha", type=_sha_or_none, default=None)
    s.add_argument("--log", type=_log_path, required=True)
    s.add_argument("--environment", type=_env, default=None)
    f = sub.add_parser("finish", help="close a deploy run")
    f.add_argument("--run-id", type=int, required=True)
    f.add_argument("--status", choices=("succeeded", "failed"), required=True)
    f.add_argument("--error", default=None)
    f.add_argument("--rolled-back", action="store_true")
    # deploy.sh's exit code: 0 promoted, 1 rolled back healthy, 2 rollback unhealthy.
    f.add_argument("--exit-code", type=int, default=None)
    f.add_argument("--phases-json", dest="phases", type=_phases, default={})
    return p


def exit_code_of(args: argparse.Namespace) -> int:
    if args.exit_code is not None:
        return int(args.exit_code)
    if args.status == "succeeded":
        return 0
    return 1 if args.rolled_back else 3


def start_details(args: argparse.Namespace) -> dict[str, Any]:
    return {"target": args.target, "sha": args.sha, "from_sha": args.from_sha, "log": args.log}


def finish_error(args: argparse.Namespace) -> str | None:
    if args.error is None:
        return None
    # One line, bounded: the poller passes a short reason, never log contents.
    return " ".join(str(args.error).split())[:MAX_ERROR] or None


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        registry = AgentRegistry(build_sessionmaker(engine))
        await registry.register(DEPLOYER, {"stale_after_s": STALE_AFTER_S})
        maker = build_sessionmaker(engine)
        if args.cmd == "start":
            await registry.heartbeat(DEPLOYER.id, "busy")
            run_id = await registry.start_run(
                DEPLOYER.id, f"deploy:{args.target}", details=start_details(args)
            )
            await _devops(maker, "start", args, run_id)
            print(run_id)
        else:
            code = exit_code_of(args)
            await registry.finish_run(
                args.run_id,
                args.status,
                error=finish_error(args),
                details={"rolled_back": bool(args.rolled_back) or code in (1, 2)},
            )
            await registry.heartbeat(DEPLOYER.id, "idle" if args.status == "succeeded" else "error")
            await _devops(maker, "finish", args, args.run_id)
        return 0
    finally:
        await engine.dispose()


async def _devops(maker: Any, cmd: str, args: argparse.Namespace, run_id: int) -> None:
    """The devops.deployments row. Best effort: the agent run is already recorded."""
    settings = get_settings()
    try:
        async with maker() as db, db.begin():
            if cmd == "start":
                await devops.server_deploy_started(
                    db, settings, agent_run_id=run_id, target=args.target, sha=args.sha,
                    previous_sha=args.from_sha, log_path=args.log, environment=args.environment,
                )  # fmt: skip
            else:
                await devops.server_deploy_finished(
                    db, settings, agent_run_id=run_id, exit_code=exit_code_of(args),
                    phases=args.phases, error=finish_error(args),
                )  # fmt: skip
    except Exception as exc:
        log.warning("deploy_record_devops_failed", error=type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
