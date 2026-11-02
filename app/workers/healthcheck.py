"""Docker healthcheck for the worker containers (scheduler, dhan-feed, research, coding,
imaging): healthy while the process's agent heartbeat is fresher than --max-age seconds.

    python -m app.workers.healthcheck --max-age 90

Exit 0 = healthy, 1 = unhealthy. Reads one file; touches no database or network.
"""

from __future__ import annotations

import argparse
import sys
import time

from app.core.heartbeat import heartbeat_file


def check(max_age: float, now: float | None = None) -> tuple[bool, str]:
    path = heartbeat_file()
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False, f"no heartbeat yet ({path})"
    if age > max_age:
        return False, f"heartbeat is {age:.0f}s old (> {max_age:.0f}s)"
    return True, f"heartbeat {age:.0f}s ago"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age", type=float, default=90.0)
    args = ap.parse_args(argv)
    ok, msg = check(args.max_age)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
