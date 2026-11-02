"""GET /api/system/status: host resources, data stores, agents, containers, host services.

Nothing here runs a command or writes anything:
- CPU, RAM and disk come from psutil (/proc inside the container, which shows the host's
  totals).
- Containers come from a Docker socket PROXY that only allows GET /containers (see
  compose.yaml). The API never mounts docker.sock itself, because even a ":ro" socket mount
  gives full control of Docker.
- systemd units (cloudflared, docker, nginx) come from a JSON file that the host-side helper
  deploy/host-status writes with `systemctl is-active` for a fixed list of units.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import psutil
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient
from app.agents.registry import IMAGING, effective_state
from app.core.config import Settings
from app.models.agents import Agent
from app.models.imaging import ImageJob

Section = dict[str, Any]


async def _guard(fn: Callable[[], Awaitable[Section]], limit: float) -> Section:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(limit):
            out = await fn()
        out.setdefault("ok", True)
    except TimeoutError:
        out = {"ok": False, "error": f"timeout after {limit:g}s"}
    except Exception as exc:  # only the type: messages can contain hosts or DSNs
        out = {"ok": False, "error": type(exc).__name__}
    out["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return out


def _disk(path: str) -> Section:
    try:
        u = psutil.disk_usage(path)
    except OSError as exc:
        return {"path": path, "ok": False, "error": type(exc).__name__}
    return {
        "path": path,
        "ok": u.percent < 90,
        "total_bytes": u.total,
        "used_bytes": u.used,
        "free_bytes": u.free,
        "percent": u.percent,
    }


def host_resources(settings: Settings) -> Section:
    vm, swap = psutil.virtual_memory(), psutil.swap_memory()
    load1, load5, load15 = psutil.getloadavg()
    disks = {"ssd": _disk("/"), "hdd": _disk(str(settings.DATA_ROOT))}
    return {
        "ok": vm.percent < 95 and all(d["ok"] for d in disks.values()),
        "cpu": {
            "count": psutil.cpu_count(),
            "percent": psutil.cpu_percent(interval=0.2),
            "load_avg": [load1, load5, load15],
        },
        "memory": {
            "total_bytes": vm.total,
            "available_bytes": vm.available,
            "percent": vm.percent,
            "swap_percent": swap.percent,
        },
        "disks": disks,
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


def host_services(settings: Settings) -> Section:
    path = settings.HOST_STATUS_FILE
    try:
        age = time.time() - path.stat().st_mtime
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"ok": False, "error": "host status helper not installed (deploy/host-status)"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    units = {str(k): str(v) for k, v in (data.get("units") or {}).items()}
    stale = age > settings.HOST_STATUS_MAX_AGE_SECONDS
    return {
        "ok": not stale and all(v == "active" for v in units.values()),
        "stale": stale,
        "age_seconds": round(age),
        "units": units,
    }


class SystemStatus:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        maker: async_sessionmaker[AsyncSession],
        redis: Redis,
        http: httpx.AsyncClient,
        ollama: OllamaClient,
    ) -> None:
        self.settings, self.engine, self.maker = settings, engine, maker
        self.redis, self.http, self.ollama = redis, http, ollama

    async def _postgres(self) -> Section:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT current_setting('server_version'),"
                        " pg_database_size(current_database()),"
                        " (SELECT count(*) FROM pg_stat_activity"
                        "  WHERE datname = current_database())"
                    )
                )
            ).one()
        return {"version": row[0], "database_bytes": int(row[1]), "connections": int(row[2])}

    async def _redis(self) -> Section:
        info = await self.redis.info("memory")
        return {"used_memory_bytes": int(info.get("used_memory", 0))}

    async def _ollama(self) -> Section:
        running = await self.ollama.running()
        installed = await self.ollama.list_models()
        return {
            "installed_models": len(installed),
            "loaded_models": [m.get("name") for m in running],
        }

    async def _imaging(self) -> Section:
        async with self.maker() as db:
            agent = await db.get(Agent, IMAGING.id)
            counts = dict(
                (
                    await db.execute(
                        select(ImageJob.status, func.count())
                        .where(ImageJob.status.in_(("queued", "running")))
                        .group_by(ImageJob.status)
                    )
                )
                .tuples()
                .all()
            )
        state = effective_state(agent, self.settings.AGENT_STALE_SECONDS) if agent else "absent"
        return {
            "ok": state in ("idle", "busy", "paused"),
            "worker": state,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
        }

    async def _docker(self) -> Section:
        if not self.settings.DOCKER_API_URL:
            return {"ok": False, "error": "DOCKER_API_URL not set"}
        resp = await self.http.get(
            f"{self.settings.DOCKER_API_URL.rstrip('/')}/containers/json", params={"all": "1"}
        )
        resp.raise_for_status()
        containers = [
            {
                "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
            for c in resp.json()
        ]
        unhealthy = [
            c["name"]
            for c in containers
            if c["state"] != "running" or "unhealthy" in (c["status"] or "")
        ]
        return {
            "ok": not unhealthy,
            "containers": sorted(containers, key=lambda c: c["name"]),
            "not_running_or_unhealthy": unhealthy,
        }

    async def collect(self) -> dict[str, Any]:
        t = self.settings.HEALTH_CHECK_TIMEOUT
        names = ["host", "postgres", "redis", "ollama", "imaging", "docker", "host_services"]
        results = await asyncio.gather(
            _guard(lambda: asyncio.to_thread(host_resources, self.settings), t + 1),
            _guard(self._postgres, t),
            _guard(self._redis, t),
            _guard(self._ollama, t),
            _guard(self._imaging, t),
            _guard(self._docker, t),
            _guard(lambda: asyncio.to_thread(host_services, self.settings), t),
        )
        sections = dict(zip(names, results, strict=True))
        return {
            "status": "ok" if all(s["ok"] for s in results) else "degraded",
            "generated_at": datetime.now(UTC).isoformat(),
            **sections,
        }
