"""SubprocessGenerator against a fake entrypoint (no DB needed)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.agents.imaging.generator import (
    GenerationFailed,
    GenerationRequest,
    StubGenerator,
    SubprocessGenerator,
    child_env,
    parse_progress,
    png_dimensions,
)
from app.core.config import Settings

AGENT = str(Path(__file__).with_name("fake_image_agent.py"))
REQ = GenerationRequest(
    prompt="a cat; rm -rf / $(whoami) `id`",
    negative_prompt="",
    seed=7,
    steps=5,
    width=64,
    height=48,
)


def gen(tmp_path: Path, mode: str = "ok", dump: str = "") -> SubprocessGenerator:
    template = [
        sys.executable,
        AGENT,
        "--prompt",
        "{prompt}",
        "--steps",
        "{steps}",
        "--size",
        "{width}x{height}",
        "--out",
        "{output}",
        "--mode",
        mode,
    ]
    if dump:
        template += ["--dump", dump]
    return SubprocessGenerator(template, tmp_path, {"PATH": os.environ["PATH"]})


async def test_runs_entrypoint_reports_progress_and_writes_png(tmp_path: Path) -> None:
    out, dump = tmp_path / "o.png", tmp_path / "d.json"
    seen: list[int] = []
    await gen(tmp_path, dump=str(dump)).generate(REQ, out, seen.append)
    assert png_dimensions(out) == (64, 48)
    assert seen and seen[-1] == 100 and seen == sorted(seen)
    # Prompt arrives as ONE literal argv item: no shell, no expansion.
    assert json.loads(dump.read_text())["prompt"] == REQ.prompt


async def test_failure_includes_output_tail(tmp_path: Path) -> None:
    with pytest.raises(GenerationFailed, match=r"exited with 3.*out of memory"):
        await gen(tmp_path, "fail").generate(REQ, tmp_path / "o.png", lambda _: None)


async def test_exit_zero_without_file_is_failure(tmp_path: Path) -> None:
    with pytest.raises(GenerationFailed, match="wrote no image"):
        await gen(tmp_path, "nofile").generate(REQ, tmp_path / "o.png", lambda _: None)


async def test_cancel_kills_the_process(tmp_path: Path) -> None:
    dump = tmp_path / "d.json"
    task = asyncio.create_task(
        gen(tmp_path, "hang", str(dump)).generate(REQ, tmp_path / "o.png", lambda _: None)
    )
    for _ in range(200):
        if dump.exists() and dump.stat().st_size:
            break
        await asyncio.sleep(0.02)
    pid = json.loads(dump.read_text())["pid"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_template_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        SubprocessGenerator([], tmp_path, {})
    with pytest.raises(ValueError, match="output"):
        SubprocessGenerator(["agent", "{prompt}"], tmp_path, {})
    with pytest.raises(KeyError):
        SubprocessGenerator(["agent", "{output}", "{password}"], tmp_path, {})


def test_child_env_has_no_app_secrets(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SECRET_KEY", "s" * 40)
    monkeypatch.setenv("HF_HOME", "/opt/image-agent/hf")
    env = child_env(settings.model_copy(update={"IMAGE_AGENT_ENV_PASSTHROUGH": ["HF_HOME"]}))
    assert "DATABASE_URL" not in env and "SECRET_KEY" not in env
    assert env["HF_HOME"] == "/opt/image-agent/hf"
    assert env["OMP_NUM_THREADS"] == str(settings.IMAGE_THREADS)


def test_parse_progress() -> None:
    assert parse_progress(" 40%|████      | 10/25 [00:40<01:00]", 25) == 40
    assert parse_progress("step 5/20", 20) == 25
    assert parse_progress("loading model", 20) is None


async def test_stub_generator(tmp_path: Path) -> None:
    seen: list[int] = []
    await StubGenerator().generate(REQ, tmp_path / "s.png", seen.append)
    assert png_dimensions(tmp_path / "s.png") == (64, 48)
    assert seen[-1] == 100
