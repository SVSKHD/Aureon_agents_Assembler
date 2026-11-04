"""Image generators. The worker runs exactly one at a time.

- SubprocessGenerator wraps the EXISTING /opt/image-agent entrypoint. The argv template comes
  from IMAGE_AGENT_COMMAND and is run without a shell. Prompts are passed as single argv
  items, never interpolated into a command line, and the child process gets a minimal
  environment with no app secrets.
- StubGenerator writes a placeholder PNG; it is for tests and dev only.

No model is ever downloaded here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import struct
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import Settings

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PLACEHOLDERS = (
    "prompt", "negative_prompt", "seed", "steps", "width", "height", "output",
    # From the job's IMAGE_MODELS entry (docs/image-agent.md); empty without one.
    "model_path", "cfg", "sampler",
)  # fmt: skip

ProgressCallback = Callable[[int], None]


class GenerationFailed(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    prompt: str
    negative_prompt: str
    seed: int
    steps: int
    width: int
    height: int
    model_path: str = ""
    cfg: float = 7.0
    sampler: str = "euler_a"


class ImageGenerator(Protocol):
    supports_cancel: bool

    async def generate(
        self, req: GenerationRequest, output: Path, on_progress: ProgressCallback
    ) -> None:
        """Write a PNG to `output`. Cancelling the awaiting task must stop the work."""


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise GenerationFailed("output is not a valid PNG")
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


def make_png(width: int, height: int, seed: int) -> bytes:
    """Small grayscale gradient PNG (stub output)."""
    rows = b"".join(
        b"\x00" + bytes(((x + y + seed) % 256) for x in range(width)) for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


class StubGenerator:
    supports_cancel = True

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    async def generate(
        self, req: GenerationRequest, output: Path, on_progress: ProgressCallback
    ) -> None:
        for step in range(1, req.steps + 1):
            if self.delay:
                await asyncio.sleep(self.delay / req.steps)
            on_progress(int(step * 100 / req.steps))
        await asyncio.to_thread(output.write_bytes, make_png(req.width, req.height, req.seed))


_FRACTION = re.compile(r"(\d+)\s*/\s*(\d+)")
_PERCENT = re.compile(r"(\d{1,3})\s*%")


def parse_progress(line: str, steps: int) -> int | None:
    """Accept `12/25`-style step counters (diffusers/tqdm) or `48%`."""
    if m := _PERCENT.search(line):
        return min(100, int(m.group(1)))
    if m := _FRACTION.search(line):
        done, total = int(m.group(1)), int(m.group(2))
        if 0 < total <= max(steps * 4, 1000) and done <= total:
            return int(done * 100 / total)
    return None


def _not_an_option(template_arg: str, value: str) -> str:
    """A user's text in its own argv slot ("{prompt}") must not start with '-': the image
    agent's argument parser would read "--output=/elsewhere" as an option. A leading space
    changes nothing for the model."""
    if template_arg.startswith("{") and value.startswith("-"):
        return " " + value
    return value


class SubprocessGenerator:
    supports_cancel = True  # by terminating the process group

    def __init__(self, template: list[str], cwd: Path, env: dict[str, str]) -> None:
        if not template:
            raise ValueError("IMAGE_AGENT_COMMAND is empty; set it from docs/00-inspection.md §5")
        if not any("{output}" in a for a in template):
            raise ValueError("IMAGE_AGENT_COMMAND must contain an {output} placeholder")
        dummy = dict.fromkeys(PLACEHOLDERS, "x")
        for arg in template:
            arg.format_map(dummy)  # raises KeyError/ValueError on unknown or broken placeholders
        self.template = template
        self.cwd = cwd
        self.env = env
        self._proc: asyncio.subprocess.Process | None = None

    def suspend(self) -> bool:
        """Freeze the running generation (SIGSTOP to its process group): it keeps its memory
        and resumes exactly where it was. Used while chat/voice requests are active."""
        return self._signal(signal.SIGSTOP)

    def resume(self) -> bool:
        return self._signal(signal.SIGCONT)

    def _signal(self, sig: signal.Signals) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
            return True
        return False

    def argv(self, req: GenerationRequest, output: Path) -> list[str]:
        values = {
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt,
            "seed": req.seed,
            "steps": req.steps,
            "width": req.width,
            "height": req.height,
            "output": str(output),
            "model_path": req.model_path,
            "cfg": f"{req.cfg:g}",
            "sampler": req.sampler,
        }
        return [_not_an_option(arg, arg.format_map(values)) for arg in self.template]

    async def generate(
        self, req: GenerationRequest, output: Path, on_progress: ProgressCallback
    ) -> None:
        self._proc = proc = await asyncio.create_subprocess_exec(
            *self.argv(req, output),
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group, so cancel kills children too
        )
        tail: deque[str] = deque(maxlen=15)
        try:
            assert proc.stdout is not None
            buf = ""
            while chunk := await proc.stdout.read(4096):
                buf += chunk.decode(errors="replace")
                *lines, buf = re.split(r"[\r\n]", buf)  # tqdm redraws with \r
                for line in filter(None, (ln.strip() for ln in lines)):
                    tail.append(line[:300])
                    if (pct := parse_progress(line, req.steps)) is not None:
                        on_progress(pct)
            rc = await proc.wait()
        except BaseException:
            await asyncio.shield(_terminate(proc))
            raise
        finally:
            self._proc = None
        if rc != 0:
            raise GenerationFailed(f"image-agent exited with {rc}: " + " | ".join(tail))
        if not await asyncio.to_thread(output.is_file):
            raise GenerationFailed("image-agent exited 0 but wrote no image")


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 5.0) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
        os.killpg(proc.pid, signal.SIGCONT)  # a suspended job must run to handle SIGTERM
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


def child_env(settings: Settings) -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ") if k in os.environ}
    for name in settings.IMAGE_AGENT_ENV_PASSTHROUGH:
        if name in os.environ:
            env[name] = os.environ[name]
    threads = str(settings.IMAGE_THREADS)
    env.update(OMP_NUM_THREADS=threads, MKL_NUM_THREADS=threads, PYTHONUNBUFFERED="1")
    return env


def build_generator(settings: Settings) -> ImageGenerator:
    if settings.IMAGE_GENERATOR == "stub":
        return StubGenerator(settings.IMAGE_STUB_DELAY)
    return SubprocessGenerator(
        settings.IMAGE_AGENT_COMMAND, settings.IMAGE_AGENT_PATH, child_env(settings)
    )
