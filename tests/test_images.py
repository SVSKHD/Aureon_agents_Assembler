"""Phase 4: image jobs end to end with the stub generator and the real worker logic."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.imaging.generator import StubGenerator
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.auth import User
from app.models.imaging import GeneratedAsset, ImageJob
from app.services.imaging import safe_asset_file
from app.workers.imaging import ImagingWorker

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"


class Env:
    def __init__(
        self,
        app: FastAPI,
        settings: Settings,
        a: AsyncClient,
        b: AsyncClient,
        csrf: dict[str, str],
        db_url: str,
    ) -> None:
        self.app, self.settings, self.a, self.b = app, settings, a, b
        self.csrf, self.db_url = csrf, db_url

    def worker(self, delay: float = 0.0) -> ImagingWorker:
        return ImagingWorker(self.settings, self.app.state.sessionmaker, StubGenerator(delay))

    def generate(self, who: str = "a", **body: Any) -> Any:
        c = self.a if who == "a" else self.b
        return c.post(
            "/api/images/generate",
            json={"prompt": "a lighthouse", "steps": 4, **body},
            headers={"x-csrf-token": self.csrf[who]},
        )

    def post(self, who: str, url: str) -> Any:
        c = self.a if who == "a" else self.b
        return c.post(url, headers={"x-csrf-token": self.csrf[who]})

    def delete(self, who: str, url: str) -> Any:
        c = self.a if who == "a" else self.b
        return c.delete(url, headers={"x-csrf-token": self.csrf[who]})

    async def sql(self, stmt: Any) -> None:
        engine = create_async_engine(self.db_url)
        async with engine.begin() as conn:
            await conn.execute(stmt)
        await engine.dispose()


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        async with engine.begin() as conn:
            for email in ("a@spasta.online", "b@spasta.online"):
                await conn.execute(
                    User.__table__.insert().values(
                        email=email, password_hash=hash_password(PASSWORD), role_id=2
                    )
                )
        await engine.dispose()
        s = Settings.model_validate(
            {
                **settings.model_dump(),
                "IMAGE_GENERATOR": "stub",
                "IMAGE_POLL_SECONDS": 0.05,
                **overrides,
            }
        )
        app = create_app(s)
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            async with (
                AsyncClient(transport=t, base_url="https://test") as a,
                AsyncClient(transport=t, base_url="https://test") as b,
            ):
                csrf = {}
                for who, c in (("a", a), ("b", b)):
                    r = await c.post(
                        "/api/auth/login",
                        json={"email": f"{who}@spasta.online", "password": PASSWORD},
                    )
                    csrf[who] = r.json()["csrf_token"]
                yield Env(app, s, a, b, csrf, db_clean)

    return factory


async def _wait_status(env: Env, job_id: str, want: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = (await env.a.get(f"/api/images/jobs/{job_id}")).json()
        if body["status"] == want:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"job never reached {want}: {body}")


# --- lifecycle ----------------------------------------------------------------------------


async def test_full_lifecycle(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.generate(seed=42, negative_prompt="blurry", size="512x768")
        assert r.status_code == 202
        job = r.json()
        assert (job["status"], job["queue_position"], job["seed"]) == ("queued", 1, 42)
        assert (job["width"], job["height"]) == (512, 768)
        # Redis got a wake-up for the worker.
        assert job["id"].encode() in await env.app.state.redis.lrange(
            env.settings.IMAGE_QUEUE_KEY, 0, -1
        )

        assert await env.worker().run_once() is True
        assert await env.worker().run_once() is False  # queue drained

        done = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()
        assert (done["status"], done["progress"], done["attempts"]) == ("done", 100, 1)
        assert done["asset_id"]

        path = env.settings.DATA_ROOT / "images" / done_user(env) / f"{job['id']}.png"
        assert path.is_file()

        gallery = (await env.a.get("/api/images")).json()
        assert gallery["total"] == 1
        item = gallery["items"][0]
        assert item["id"] == done["asset_id"] and item["prompt"] == "a lighthouse"
        assert (item["width"], item["height"]) == (512, 768)

        f = await env.a.get(item["url"])
        assert f.status_code == 200
        assert f.headers["content-type"] == "image/png"
        assert f.headers["x-content-type-options"] == "nosniff"
        assert f.content.startswith(b"\x89PNG")

        assert (await env.delete("a", f"/api/images/{item['id']}")).status_code == 204
        assert not path.exists()
        assert (await env.a.get(item["url"])).status_code == 404
        assert (await env.a.get("/api/images")).json()["total"] == 0


def done_user(env: Env) -> str:
    # Single subdirectory = user A's id.
    (only,) = (env.settings.DATA_ROOT / "images").iterdir()
    return only.name


async def test_random_seed_and_defaults(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_DEFAULT_STEPS=7) as env:
        job = (
            await env.a.post(
                "/api/images/generate",
                json={"prompt": "x"},
                headers={"x-csrf-token": env.csrf["a"]},
            )
        ).json()
        assert job["steps"] == 7 and (job["width"], job["height"]) == (512, 512)
        assert 0 <= job["seed"] < 2**32
        assert job["model"] == "sd-1.5"


async def test_validation(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_MAX_STEPS=30) as env:
        assert (await env.generate(size="1024x1024")).status_code == 422
        assert (await env.generate(steps=31)).status_code == 422
        assert (await env.generate(prompt="bad\x00prompt")).status_code == 422
        assert (await env.generate(prompt="")).status_code == 422
        r = await env.a.post("/api/images/generate", json={"prompt": "x"})
        assert r.status_code == 403  # CSRF


async def test_refuses_when_hdd_not_mounted(make_env: EnvFactory) -> None:
    async with make_env() as env:
        (env.settings.data_marker_path).unlink()
        assert (await env.generate()).status_code == 503


# --- queue cap ----------------------------------------------------------------------------


async def test_queue_cap_returns_429(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_QUEUE_MAX=2, IMAGE_RETRY_AFTER=321) as env:
        first = (await env.generate()).json()
        second = (await env.generate(who="b")).json()
        assert second["queue_position"] == 2
        full = await env.generate()
        assert full.status_code == 429 and full.headers["retry-after"] == "321"
        # Cancelling a queued job frees a place.
        assert (await env.post("a", f"/api/images/jobs/{first['id']}/cancel")).json()[
            "status"
        ] == "cancelled"
        assert (await env.generate()).status_code == 202


async def test_concurrent_enqueues_respect_cap(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_QUEUE_MAX=3) as env:
        results = await asyncio.gather(*(env.generate() for _ in range(6)))
        codes = sorted(r.status_code for r in results)
        assert codes == [202, 202, 202, 429, 429, 429]


# --- cancellation, shutdown, recovery -----------------------------------------------------


async def test_cancel_queued_job_is_never_run(make_env: EnvFactory) -> None:
    async with make_env() as env:
        job = (await env.generate()).json()
        r = await env.post("a", f"/api/images/jobs/{job['id']}/cancel")
        assert r.json()["status"] == "cancelled"
        assert await env.worker().run_once() is False


async def test_cancel_running_job_stops_generation(make_env: EnvFactory) -> None:
    async with make_env() as env:
        job = (await env.generate(steps=50)).json()
        run = asyncio.create_task(env.worker(delay=5.0).run_once())
        await _wait_status(env, job["id"], "running")
        r = (await env.post("a", f"/api/images/jobs/{job['id']}/cancel")).json()
        assert (r["status"], r["cancel_requested"]) == ("running", True)
        await asyncio.wait_for(run, 3)
        final = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()
        assert final["status"] == "cancelled" and final["asset_id"] is None
        images = env.settings.DATA_ROOT / "images"
        assert not any(p.is_file() for p in images.rglob("*")) if images.exists() else True


async def test_progress_is_saved_while_running(make_env: EnvFactory) -> None:
    async with make_env() as env:
        job = (await env.generate(steps=20)).json()
        run = asyncio.create_task(env.worker(delay=1.0).run_once())
        seen = set()
        while not run.done():
            seen.add((await env.a.get(f"/api/images/jobs/{job['id']}")).json()["progress"])
            await asyncio.sleep(0.05)
        assert any(0 < p < 100 for p in seen)


async def test_shutdown_requeues_running_job(make_env: EnvFactory) -> None:
    async with make_env() as env:
        job = (await env.generate()).json()
        worker = env.worker(delay=5.0)
        run = asyncio.create_task(worker.run_once())
        await _wait_status(env, job["id"], "running")
        worker.stopping.set()
        await asyncio.wait_for(run, 3)
        back = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()
        assert (back["status"], back["attempts"], back["progress"]) == ("queued", 1, 0)
        assert await env.worker().run_once() is True
        assert (await env.a.get(f"/api/images/jobs/{job['id']}")).json()["status"] == "done"


async def test_recovery_after_crash(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_MAX_ATTEMPTS=2) as env:
        retry = (await env.generate()).json()["id"]
        give_up = (await env.generate()).json()["id"]
        await env.sql(
            update(ImageJob).where(ImageJob.id == retry).values(status="running", attempts=1)
        )
        await env.sql(
            update(ImageJob).where(ImageJob.id == give_up).values(status="running", attempts=2)
        )
        await env.worker().recover()
        assert (await env.a.get(f"/api/images/jobs/{retry}")).json()["status"] == "queued"
        failed = (await env.a.get(f"/api/images/jobs/{give_up}")).json()
        assert failed["status"] == "failed" and "worker stopped" in failed["error"]


async def test_generator_failure_marks_job_failed(make_env: EnvFactory) -> None:
    class Broken(StubGenerator):
        async def generate(self, req: Any, output: Path, on_progress: Any) -> None:
            await asyncio.to_thread(output.write_bytes, b"not a png")

    async with make_env() as env:
        job = (await env.generate()).json()
        w = ImagingWorker(env.settings, env.app.state.sessionmaker, Broken())
        await w.run_once()
        body = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()
        assert body["status"] == "failed" and "not a valid PNG" in body["error"]
        assert not list((env.settings.DATA_ROOT / "images").rglob("*.png"))


async def test_job_timeout(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_JOB_TIMEOUT_SECONDS=0.3) as env:
        job = (await env.generate()).json()
        await env.worker(delay=10.0).run_once()
        body = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()
        assert body["status"] == "failed" and "timed out" in body["error"]


# --- path safety + isolation --------------------------------------------------------------


async def test_path_traversal_is_rejected(make_env: EnvFactory, tmp_path: Path) -> None:
    async with make_env() as env:
        job = (await env.generate()).json()
        await env.worker().run_once()
        asset_id = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()["asset_id"]
        url = f"/api/images/{asset_id}/file"
        secret = env.settings.DATA_ROOT / "secret.png"
        secret.write_bytes(b"\x89PNG top secret")
        for bad in (
            "../../../../etc/passwd",
            "images/../secret.png",
            "/etc/hostname",
            "secret.png",
        ):
            await env.sql(
                update(GeneratedAsset).where(GeneratedAsset.id == asset_id).values(path=bad)
            )
            assert (await env.a.get(url)).status_code == 404, bad
        # A symlink inside images/ pointing outside is refused too.
        link_dir = env.settings.DATA_ROOT / "images" / "x"
        link_dir.mkdir(parents=True)
        (link_dir / "evil.png").symlink_to(secret)
        assert safe_asset_file(env.settings, "images/x/evil.png") is None
        assert (await env.a.get("/api/images/not-a-uuid/file")).status_code == 422


async def test_user_isolation(make_env: EnvFactory) -> None:
    async with make_env() as env:
        job = (await env.generate()).json()
        await env.worker().run_once()
        asset_id = (await env.a.get(f"/api/images/jobs/{job['id']}")).json()["asset_id"]

        assert (await env.b.get(f"/api/images/jobs/{job['id']}")).status_code == 404
        assert (await env.post("b", f"/api/images/jobs/{job['id']}/cancel")).status_code == 404
        assert (await env.b.get(f"/api/images/{asset_id}/file")).status_code == 404
        assert (await env.delete("b", f"/api/images/{asset_id}")).status_code == 404
        assert (await env.b.get("/api/images")).json()["total"] == 0
        # Still intact for the owner.
        assert (await env.a.get(f"/api/images/{asset_id}/file")).status_code == 200


async def test_models_selection_and_per_model_steps(make_env: EnvFactory) -> None:
    models = {
        "dreamshaper8": {"file": "/m/ds8.gguf", "steps": 20, "cfg": 7.0, "sampler": "euler_a"},
        "dreamshaper8-lcm": {"file": "/m/lcm.gguf", "steps": 6, "cfg": 1.5, "sampler": "lcm",
                             "note": "fast"},
    }  # fmt: skip
    async with make_env(IMAGE_MODELS=models, IMAGE_MODEL_NAME="dreamshaper8") as env:
        r = await env.a.get("/api/images/models")
        assert r.status_code == 200
        items = {m["name"]: m for m in r.json()["items"]}
        assert items["dreamshaper8"]["default"] and items["dreamshaper8-lcm"]["steps"] == 6
        hdr = {"x-csrf-token": env.csrf["a"]}
        r = await env.a.post(
            "/api/images/generate", json={"prompt": "a lighthouse", "model": "dreamshaper8-lcm"},
            headers=hdr,
        )  # fmt: skip
        assert r.status_code == 202, r.text
        assert r.json()["model"] == "dreamshaper8-lcm" and r.json()["steps"] == 6
        r = await env.a.post(
            "/api/images/generate", json={"prompt": "x", "model": "sdxl"}, headers=hdr
        )
        assert r.status_code == 422


async def test_disabled_image_agent_refuses(make_env: EnvFactory) -> None:
    async with make_env(AGENT_IMAGE_ENABLED=False) as env:
        r = await env.a.post(
            "/api/images/generate", json={"prompt": "x"}, headers={"x-csrf-token": env.csrf["a"]}
        )
        assert r.status_code == 503
