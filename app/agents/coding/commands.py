"""Allowlisted commands only: an exact argv match from CODE_ALLOWED_COMMANDS, run without
a shell, in the task's worktree, with a scrubbed environment and a hard timeout."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOWED: tuple[tuple[str, ...], ...] = (
    ("npm", "test"),
    ("npm", "run", "verify"),
    ("npm", "run", "type-check"),
    ("npm", "run", "lint:css"),
    ("pytest", "-q"),
    ("python", "-m", "pytest", "-q"),
    (".venv/bin/python", "-m", "pytest", "-q"),
    (".venv/bin/python", "-m", "pytest", "-q", "-m", "not db"),
    ("ruff", "check", "."),
    (".venv/bin/ruff", "check", "."),
    (".venv/bin/mypy", "app"),
)
OUTPUT_CAP = 64_000
# Only these reach a test process; DATABASE_URL, tokens and keys never do.
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "TZ")


class CommandRejected(ValueError):
    pass


def check_allowed(argv: Sequence[str], allowed: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Exact match against the allowlist. No prefixes, no extra arguments, no shell."""
    cmd = tuple(argv)
    if not cmd:
        raise CommandRejected("empty command")
    if any("\x00" in a for a in cmd):
        raise CommandRejected("invalid characters")
    if cmd not in {tuple(a) for a in allowed}:
        raise CommandRejected(f"not allowlisted: {' '.join(cmd)}")
    return cmd


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    output: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "ok": self.ok,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "output": self.output,
        }


def scrubbed_env(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    env.update({"HOME": str(home), "CI": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


async def run_allowed(
    argv: Sequence[str],
    *,
    allowed: Sequence[Sequence[str]],
    cwd: Path,
    limit_s: float,
    home: Path,
) -> CommandResult:
    cmd = check_allowed(argv, allowed)
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=scrubbed_env(home),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group: a timeout kills the whole tree
    )
    timed_out = False
    try:
        async with asyncio.timeout(limit_s):
            out, _ = await proc.communicate()
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", "replace")
    if len(text) > OUTPUT_CAP:
        text = text[: OUTPUT_CAP // 4] + "\n…[output truncated]…\n" + text[-OUTPUT_CAP // 2 :]
    return CommandResult(
        cmd,
        None if timed_out else proc.returncode,
        text,
        int((time.monotonic() - started) * 1000),
        timed_out,
    )
