"""Record host deploys as agent runs of the `deployer` agent (docs/deploy-automation.md).

The host poller (scripts/ops/deploy-poll.sh) calls this in a one-shot api container around
every deploy, so Spasta's agent monitor shows deploy status and history:

    python -m app.workers.deploy_record start --target backend --sha <new> --from <old> \
        --log /mnt/spasta-data/logs/deploy.log            # prints the run id
    python -m app.workers.deploy_record finish --run-id 42 --status succeeded
    python -m app.workers.deploy_record finish --run-id 42 --status failed --error "health"

Only ids, SHAs and the log path are stored (runs are visible to every user). Best effort by
design: a recording failure exits non-zero but must never block or fail the deploy itself.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any

from app.agents.registry import AgentRegistry, AgentSpec
from app.core.config import get_settings
from app.db.session import build_engine, build_sessionmaker

DEPLOYER = AgentSpec("deployer", "Host deployer", "deploy")
TARGETS = ("backend", "frontend")
# Deploys are rare: without this the agent would show "stale" between them.
STALE_AFTER_S = 35 * 24 * 3600
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_LOG = re.compile(r"^/[A-Za-z0-9._/-]{1,255}$")
MAX_ERROR = 500


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
    f = sub.add_parser("finish", help="close a deploy run")
    f.add_argument("--run-id", type=int, required=True)
    f.add_argument("--status", choices=("succeeded", "failed"), required=True)
    f.add_argument("--error", default=None)
    f.add_argument("--rolled-back", action="store_true")
    return p


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
        if args.cmd == "start":
            await registry.heartbeat(DEPLOYER.id, "busy")
            run_id = await registry.start_run(
                DEPLOYER.id, f"deploy:{args.target}", details=start_details(args)
            )
            print(run_id)
        else:
            await registry.finish_run(
                args.run_id,
                args.status,
                error=finish_error(args),
                details={"rolled_back": bool(args.rolled_back)},
            )
            await registry.heartbeat(DEPLOYER.id, "idle" if args.status == "succeeded" else "error")
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
