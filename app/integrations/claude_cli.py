"""The local Claude CLI, used only by the PR reviewer (docs/22-claude-review.md).

This is the single non-Ollama AI path in the project, and it stays behind
CLAUDE_REVIEW_ENABLED. `available()` is true only when the setting is on, the binary resolves
on PATH (or is an absolute path to an executable), and `claude --version` exits 0 within
10 seconds; the answer, good or bad, is cached for 5 minutes.

Every run:
- argv only (`create_subprocess_exec`), never a shell;
- a minimal environment: HOME and PATH, plus the env var names in
  CLAUDE_CLI_ENV_PASSTHROUGH (repo secrets are refused even if listed);
- the prompt on stdin, never in argv;
- a fresh, empty temporary working directory (removed afterwards), so there is nothing in
  the working directory for the CLI to read;
- its own process group, killed as a whole on timeout; output capped.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("integrations.claude_cli")

VERSION_TIMEOUT_S = 10.0
CACHE_SECONDS = 300.0
OUTPUT_CAP = 1024 * 1024
STDERR_CAP = 8 * 1024
# Never passed to the CLI, whatever CLAUDE_CLI_ENV_PASSTHROUGH says.
DENIED_ENV_PREFIXES = (
    "GITHUB_",
    "GH_",
    "DATABASE_",
    "POSTGRES",
    "PG",
    "REDIS",
    "SECRET",
    "DHAN_",
    "MT5_",
    "AWS_",
    "SSH_",
    "GIT_",
    "TRADING_",
    "PAPER_",
)
DENIED_ENV = frozenset({"SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LD_PRELOAD", "PYTHONPATH"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class CLIResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class Availability:
    available: bool
    reason: str | None  # disabled | cli_missing | version_failed | version_timeout
    version: str | None
    path: str | None
    checked_at: float


# (argv, stdin, limit seconds, env, cwd) -> result
Runner = Callable[
    [Sequence[str], bytes | None, float, Mapping[str, str], Path], Awaitable[CLIResult]
]


def allowed_env_name(name: str) -> bool:
    return (
        bool(_ENV_NAME.match(name))
        and name not in DENIED_ENV
        and not name.upper().startswith(DENIED_ENV_PREFIXES)
        and name not in ("HOME", "PATH")
    )


def cli_env(settings: Settings, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """HOME, PATH and the allowed passthrough names, nothing else."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in ("HOME", "PATH") if k in src}
    for name in settings.CLAUDE_CLI_ENV_PASSTHROUGH:
        if not allowed_env_name(name):
            log.warning("claude_cli_env_refused", name=name)
            continue
        if name in src:
            env[name] = src[name]
    return env


def _cap(raw: bytes, cap: int) -> str:
    text = raw[:cap].decode("utf-8", "replace")
    return text + ("\n…[truncated]" if len(raw) > cap else "")


async def run_subprocess(
    argv: Sequence[str],
    stdin: bytes | None,
    limit_s: float,
    env: Mapping[str, str],
    cwd: Path,
) -> CLIResult:
    """Run argv (no shell) in its own process group; kill the group on timeout."""
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=dict(env),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        async with asyncio.timeout(limit_s):
            out, err = await proc.communicate(stdin)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = await proc.communicate()
    return CLIResult(
        None if timed_out else proc.returncode,
        _cap(out or b"", OUTPUT_CAP),
        _cap(err or b"", STDERR_CAP),
        int((time.monotonic() - started) * 1000),
        timed_out,
    )


def resolve_binary(path: str, env_path: str | None) -> str | None:
    """The absolute path of the CLI, or None when it does not resolve to an executable."""
    if not path or "\x00" in path:
        return None
    return shutil.which(path, path=env_path)


class ClaudeCLI:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.runner: Runner = runner or run_subprocess
        self.clock = clock
        self._cached: Availability | None = None
        self._lock = asyncio.Lock()
        self.invocations = 0  # every process started (version checks included)

    def invalidate(self) -> None:
        self._cached = None

    async def _invoke(self, argv: Sequence[str], stdin: bytes | None, limit_s: float) -> CLIResult:
        env = cli_env(self.settings)
        workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="claude-review-"))
        self.invocations += 1
        try:
            return await self.runner(argv, stdin, limit_s, env, workdir)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    async def availability(self, *, force: bool = False) -> Availability:
        now = self.clock()
        if not self.settings.CLAUDE_REVIEW_ENABLED:
            return Availability(False, "disabled", None, None, now)
        async with self._lock:
            c = self._cached
            if c is not None and not force and now - c.checked_at < CACHE_SECONDS:
                return c
            self._cached = await self._check(now)
            return self._cached

    async def _check(self, now: float) -> Availability:
        env_path = cli_env(self.settings).get("PATH")
        binary = resolve_binary(self.settings.CLAUDE_CLI_PATH, env_path)
        if binary is None:
            log.warning("claude_cli_missing", path=self.settings.CLAUDE_CLI_PATH)
            return Availability(False, "cli_missing", None, None, now)
        try:
            res = await self._invoke([binary, "--version"], None, VERSION_TIMEOUT_S)
        except OSError as exc:
            log.warning("claude_cli_unrunnable", error=type(exc).__name__)
            return Availability(False, "version_failed", None, binary, now)
        if res.timed_out:
            return Availability(False, "version_timeout", None, binary, now)
        if res.exit_code != 0:
            log.warning("claude_cli_version_failed", exit_code=res.exit_code)
            return Availability(False, "version_failed", None, binary, now)
        version = (res.stdout.strip().splitlines() or [""])[0][:100] or None
        return Availability(True, None, version, binary, now)

    async def available(self) -> bool:
        return (await self.availability()).available

    async def run(self, prompt: str, binary: str, *, limit_s: float | None = None) -> CLIResult:
        """Send `prompt` on stdin to `binary` with CLAUDE_CLI_ARGS. The caller checks
        availability (and gets `binary` from it) first."""
        argv = [binary, *self.settings.CLAUDE_CLI_ARGS]
        default = self.settings.CLAUDE_REVIEW_TIMEOUT_SECONDS
        limit = float(limit_s if limit_s is not None else default)
        return await self._invoke(argv, prompt.encode("utf-8"), limit)


__all__ = [
    "Availability",
    "CLIResult",
    "ClaudeCLI",
    "allowed_env_name",
    "cli_env",
    "resolve_binary",
    "run_subprocess",
]
