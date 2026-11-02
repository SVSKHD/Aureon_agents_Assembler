"""Per-container liveness file for Docker healthchecks of the workers.

Every `Heartbeat` (app/agents/registry.py) touches this file after a successful registry
heartbeat; `python -m app.workers.healthcheck` fails when it is missing or too old. It lives
on the container's own /tmp tmpfs, so it never outlives the container. Stdlib only, so the
healthcheck starts fast.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

HB_NAME = "aureon-heartbeat"


def heartbeat_file() -> Path:
    override = os.environ.get("AUREON_HEARTBEAT_FILE")
    return Path(override) if override else Path(tempfile.gettempdir()) / HB_NAME


def touch_heartbeat_file() -> None:
    with contextlib.suppress(OSError):
        heartbeat_file().touch()
