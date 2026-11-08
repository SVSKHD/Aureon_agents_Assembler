"""Phase 22 static and concurrency checks (no DB): the MT5 feed has no write path, and the
one-heavy-model-at-a-time rule holds for the coding/repair lock, the research AI lane, the
image worker and the Claude review runner."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app
from tests.routes import api_routes

ROOT = Path(__file__).resolve().parents[1]
ORDER_NAME = re.compile(
    r"(place|send|submit|modify|amend|cancel|close)_?(order|position|trade)|order_(send|check)",
    re.I,
)


def _py(*parts: str) -> list[Path]:
    base = ROOT.joinpath(*parts)
    return [base] if base.suffix == ".py" else sorted(base.rglob("*.py"))


# --- MT5: read-only ----------------------------------------------------------------------


def test_mt5_adapter_and_broker_interface_have_no_order_methods() -> None:
    files = _py("app", "agents", "trading", "mt5") + _py("app", "agents", "trading", "base.py")
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert not ORDER_NAME.search(node.name), f"{f}: {node.name}"
            if isinstance(node, ast.Attribute):
                # HTTP writes from the status source: only GET is allowed.
                assert node.attr not in ("post", "put", "patch", "delete"), f"{f}: .{node.attr}"
    from app.agents.trading.base import BrokerAdapter
    from app.agents.trading.mt5.adapter import MT5Adapter

    for cls in (BrokerAdapter, MT5Adapter):
        assert not [m for m in dir(cls) if ORDER_NAME.search(m)], cls


def test_no_order_send_and_no_metatrader_import_anywhere() -> None:
    hits = []
    for top in ("app", "scripts", "deploy", "alembic"):
        for f in ROOT.joinpath(top).rglob("*"):
            if f.is_file() and f.suffix in (".py", ".sh", ".sql", ".conf", ".yml", ".yaml"):
                text = f.read_text(errors="replace")
                mt5_import = re.search(r"^\s*(import|from)\s+MetaTrader5", text, re.M)
                if "order_send" in text or mt5_import:
                    hits.append(str(f.relative_to(ROOT)))
    assert hits == []


def test_mt5_routes_are_read_only(settings: Settings) -> None:
    routes = {
        (m, r.path) for r in api_routes(create_app(settings)) if "/mt5" in r.path for m in r.methods
    }
    # GET the status; PUT registers a contract spec in OUR database (admin + CSRF); POST
    # ingest stores the bot's push (bearer token). Nothing sends anything to the bot or MT5.
    assert routes == {
        ("GET", "/api/trading/mt5/status"),
        ("PUT", "/api/trading/mt5/instruments/{symbol}"),
        ("POST", "/api/trading/mt5/ingest"),
        ("GET", "/api/trading/mt5/bot-status"),
        ("GET", "/api/trading/mt5/market"),
        ("GET", "/api/trading/mt5/positions"),
        ("GET", "/api/trading/mt5/signals"),
        ("GET", "/api/trading/mt5/failures"),
        ("GET", "/api/trading/mt5/strategy"),
        ("GET", "/api/trading/mt5/report"),
        # Aureon V1.6.3 (docs/34): the Windows collector uploads broker deal history into OUR
        # database (collector bearer token); nothing is sent to the bot or MT5.
        ("POST", "/api/v1/ingest/mt5_deals"),
    }


# --- one heavy model at a time -----------------------------------------------------------


def _inside_lock(tree: ast.AST, call_name: str, lock: str) -> list[bool]:
    """For each call to `call_name`, is it lexically inside `async with <lock>:`?"""
    out: list[bool] = []

    def visit(node: ast.AST, locked: bool) -> None:
        if isinstance(node, ast.AsyncWith):
            here = any(
                isinstance(i.context_expr, ast.Name) and i.context_expr.id == lock
                for i in node.items
            )
            for child in node.body:
                visit(child, locked or here)
            return
        if isinstance(node, ast.Call) and ast.unparse(node.func) == call_name:
            out.append(locked)
        for child in ast.iter_child_nodes(node):
            visit(child, locked)

    visit(tree, False)
    return out


def test_code_tasks_and_repairs_run_under_the_heavy_model_lock() -> None:
    tree = ast.parse((ROOT / "app" / "workers" / "coding.py").read_text())
    assert _inside_lock(tree, "coding.run_task", "HEAVY_MODEL_LOCK") == [True]
    assert _inside_lock(tree, "repairs.run_repair", "HEAVY_MODEL_LOCK") == [True]
    # Git ops hold the token in a child's env: never while a test process may run.
    assert _inside_lock(tree, "coding_git.run_op", "HEAVY_MODEL_LOCK") == [True]


async def test_repair_loop_holds_the_lock_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.ollama.heavy import HEAVY_MODEL_LOCK
    from app.services import repairs
    from app.workers import coding as worker

    stop = asyncio.Event()
    ids = [uuid.uuid4(), uuid.uuid4()]
    seen: list[bool] = []

    async def claim(maker: Any, settings: Any) -> uuid.UUID | None:
        if not ids:
            stop.set()
            return None
        return ids.pop()

    async def run_repair(*a: Any) -> str:
        seen.append(HEAVY_MODEL_LOCK.locked())
        await asyncio.sleep(0)
        return "pr_opened"

    class Reg:
        async def record_run(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr(repairs, "claim", claim)
    monkeypatch.setattr(repairs, "run_repair", run_repair)
    s = type("S", (), {"REPAIR_POLL_SECONDS": 0.01})()

    # A "code task" holds the lock first: the repair must wait for it.
    order: list[str] = []
    async with HEAVY_MODEL_LOCK:
        task = asyncio.create_task(
            worker.repair_loop(None, s, None, None, None, Reg(), stop, {})  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)
        order.append("code-task-done")
        assert seen == []  # blocked behind the code task
    await asyncio.wait_for(task, 5)
    assert seen == [True, True]
    assert order == ["code-task-done"]


async def _max_concurrency(run: Any, n: int) -> int:
    active = peak = 0
    done = asyncio.Event()
    count = 0

    async def job() -> None:
        nonlocal active, peak, count
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        count += 1
        if count == n:
            done.set()

    await run(job, n)
    await asyncio.wait_for(done.wait(), 5)
    return peak


async def test_claude_reviews_run_one_at_a_time_with_a_bounded_queue() -> None:
    from app.services.claude_review import ReviewQueueFull, ReviewRunner

    runner = ReviewRunner(max_queue=3)

    async def run(job: Any, n: int) -> None:
        for _ in range(n):
            runner.submit(job)

    assert await _max_concurrency(run, 4) == 1
    await runner.drain()
    blocker = asyncio.Event()

    async def wait() -> None:
        await blocker.wait()

    for _ in range(4):  # one running + max_queue waiting
        runner.submit(wait)
    with pytest.raises(ReviewQueueFull):
        runner.submit(wait)
    blocker.set()
    await runner.drain()


async def test_research_ai_lane_has_one_consumer_and_a_bounded_queue(settings: Settings) -> None:
    from app.workers.research import AI_RESEARCH, ResearchWorker

    s = settings.model_copy(update={"RESEARCH_QUEUE_MAX": 2})
    w = ResearchWorker(s, None, {})  # type: ignore[arg-type]
    lane = w.lane(AI_RESEARCH)
    assert lane.queue.maxsize == 2
    assert lane.offer(1) and lane.offer(2) and not lane.offer(3) and lane.dropped == 1
    # main() starts exactly one consumer per lane (handlers() is one entry per agent).
    assert list(w.handlers()).count(AI_RESEARCH.id) == 1
    src = (ROOT / "app" / "workers" / "research.py").read_text()
    assert "worker.consume(SPECS_BY_ID[k], h)) for k, h in worker.handlers().items()" in src

    while not lane.queue.empty():
        lane.queue.get_nowait()
        lane.queue.task_done()

    async def run(job: Any, n: int) -> None:
        async def handler(_: Any) -> None:
            await job()

        consumer = asyncio.create_task(w.consume(AI_RESEARCH, handler))
        for i in range(n):
            await lane.queue.put(i)
        await lane.queue.join()
        consumer.cancel()

    assert await _max_concurrency(run, 3) == 1


def test_image_worker_is_single_instance_single_job() -> None:
    from app.workers import imaging

    src = (ROOT / "app" / "workers" / "imaging.py").read_text()
    # A Redis NX lock: a second worker refuses to start.
    assert "redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL)" in src
    assert imaging.LOCK_KEY == "aureon:imaging:worker-lock"
    # One `current_job` slot; the serve loop awaits each job before claiming the next.
    assert src.count("self.current_job =") >= 1 and "asyncio.gather" not in src


def test_chat_generations_are_serialised_by_default(settings: Settings) -> None:
    assert Settings.model_fields["OLLAMA_MAX_CONCURRENT"].default == 1
    assert settings.OLLAMA_MAX_CONCURRENT == 1


# --- Cloudflare tunnel + nginx config -----------------------------------------------------


def test_cloudflared_ingress_routes_only_to_nginx_on_loopback() -> None:
    import yaml

    cfg = yaml.safe_load((ROOT / "deploy" / "cloudflare" / "config.yml.example").read_text())
    rules = cfg["ingress"]
    assert rules[-1] == {"service": "http_status:404"}
    hosts = {r.get("hostname") for r in rules[:-1]}
    assert hosts == {"spasta.online", "api.spasta.online"}
    for r in rules[:-1]:
        assert r["service"] == "http://127.0.0.1:8088", r  # nginx, never :8000 directly
    assert "8000" not in json.dumps(cfg)  # no rule targets the API port
    bypass = [r for r in rules[:-1] if "path" in r]
    assert {r["path"] for r in bypass} == {"^/api/v1/github/webhooks$", "^/api/public/"}
    for r in bypass:
        assert "access" not in r.get("originRequest", {})
    protected = [r for r in rules[:-1] if "path" not in r]
    assert {r["hostname"] for r in protected} == hosts
    for r in protected:
        acc = r["originRequest"]["access"]
        assert acc["required"] is True and acc["teamName"] and acc["audTag"]
    # Bypass rules come before the hostname-wide rules (first match wins).
    assert max(rules.index(r) for r in bypass) < min(rules.index(r) for r in protected)
    assert cfg["metrics"].startswith("127.0.0.1:")


def test_nginx_api_host_requires_the_access_header_except_bypass_paths() -> None:
    conf = (ROOT / "deploy" / "nginx" / "api-spasta-online.conf").read_text()
    body = "\n".join(line.split("#", 1)[0] for line in conf.splitlines())
    assert "listen 127.0.0.1:8088;" in body and "server_name api.spasta.online;" in body
    assert "$http_cf_access_jwt_assertion" in body
    blocks = re.findall(r"location\s+([^{]+)\{(.*?)\n    \}", body, re.S)
    by = {loc.strip(): blk for loc, blk in blocks}
    for loc in ("/api/", "= /api/finance/attachments"):
        assert "if ($aureon_cf_jwt_ok = 0) { return 403; }" in by[loc], loc
    for loc in ("= /api/v1/github/webhooks", "^~ /api/public/"):
        assert "aureon_cf_jwt_ok" not in by[loc], loc
        assert "limit_except" in by[loc], loc
    assert "return 404" in by["= /api/health/ready"] and "return 404" in by["/"]
    for blk in by.values():
        for target in re.findall(r"proxy_pass\s+(\S+);", blk):
            assert target == "http://127.0.0.1:8000", target
    tf = (ROOT / "deploy" / "cloudflare" / "access.tf.example").read_text()
    assert tf.count('decision       = "bypass"') == 2
    assert "api.spasta.online/api/v1/github/webhooks" in tf
    assert "session_duration" in tf and "var.owner_email" in tf


async def test_blocking_pops_survive_an_idle_redis_timeout() -> None:
    """redis-py >= 8 raises redis.exceptions.TimeoutError when a >= 5 s blocking pop finds
    nothing. Found by the Phase 22 acceptance run: the image worker's serve loop died on its
    first idle wait and never picked up another job."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from app.workers.imaging import ImagingWorker

    calls = {"n": 0}

    class FakeRedis:
        async def blpop(self, keys: list[str], **kw: Any) -> Any:
            calls["n"] += 1
            if calls["n"] >= 3:
                w.stopping.set()
            raise RedisTimeoutError("Timeout reading from 127.0.0.1:6379")

    w = ImagingWorker.__new__(ImagingWorker)
    w.settings = type("S", (), {"IMAGE_QUEUE_KEY": "q"})()  # type: ignore[assignment]
    w.paused = False
    w.stopping = asyncio.Event()

    async def run_once() -> bool:
        return False

    w.run_once = run_once  # type: ignore[method-assign]
    await asyncio.wait_for(w.serve(FakeRedis()), 5)  # type: ignore[arg-type]
    assert calls["n"] == 3


def test_acceptance_script_has_16_steps_and_no_credential_args() -> None:
    import importlib.util

    path = ROOT / "scripts" / "ops" / "acceptance.py"
    spec = importlib.util.spec_from_file_location("acceptance", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["acceptance"] = mod  # dataclasses resolve their module
    spec.loader.exec_module(mod)
    assert len(mod.STEPS) == 16 and len({s for s, _ in mod.STEPS}) == 16
    for sid, _ in mod.STEPS:
        assert hasattr(mod.Acceptance, f"step_{sid}"), sid
    src = path.read_text()
    assert "--password" not in src and "--email" not in src  # env/stdin only
    assert mod.main(["--base-url", "http://127.0.0.1:1"]) == 2  # no email set -> refuses


def _nginx_body(name: str) -> str:
    conf = (ROOT / "deploy" / "nginx" / name).read_text()
    return "\n".join(line.split("#", 1)[0] for line in conf.splitlines())


def test_nginx_tailscale_host_serves_spa_and_same_origin_api() -> None:
    body = _nginx_body("spasta-tailscale.conf")
    assert "listen 127.0.0.1:8088;" in body and "default_server" not in body
    assert "server_name svskhd8.taila439d5.ts.net;" in body
    assert "root /srv/www/spasta/current;" in body
    assert "include /opt/private-ai/aureon/deploy/nginx/security-headers.conf;" in body
    assert "include /opt/private-ai/aureon/deploy/nginx/aureon-api.conf;" in body
    assert "try_files $uri $uri/ /index.html;" in body
    # No upstream of its own: /api/ goes only through aureon-api.conf (-> 127.0.0.1:8000).
    assert "proxy_pass" not in body
    api = _nginx_body("aureon-api.conf")
    assert re.search(r"location = /api/health/ready \{\s*return 404;", api)
    assert set(re.findall(r"proxy_pass\s+(\S+);", api)) == {"http://127.0.0.1:8000"}
    assert "connect-src 'self'" in _nginx_body("security-headers.conf")


def test_nginx_unknown_host_on_8088_gets_444() -> None:
    body = _nginx_body("default-8088.conf")
    assert "listen 127.0.0.1:8088 default_server;" in body
    assert "return 444;" in body and "location" not in body
    # Only one default_server for the port, and every listener stays on loopback.
    confs = sorted((ROOT / "deploy" / "nginx").glob("*.conf"))
    assert sum("default_server" in _nginx_body(p.name) for p in confs) == 1
    for p in confs:
        for addr in re.findall(r"^\s*listen\s+([^\s;]+)", _nginx_body(p.name), re.M):
            assert addr == "127.0.0.1:8088", (p.name, addr)


def test_nginx_spa_lets_its_own_pages_use_the_microphone() -> None:
    # Voice (dictation + navigation) needs the mic on the SPA's own documents; microphone=()
    # made the browser refuse before any prompt. Nothing else is widened.
    body = _nginx_body("security-headers.conf")
    (policy,) = re.findall(r'add_header Permissions-Policy "([^"]+)" always;', body)
    features = dict(f.split("=", 1) for f in policy.split(", "))
    assert features["microphone"] == "(self)"
    for off in ("camera", "geolocation", "payment", "usb"):
        assert features[off] == "()", off
    # The Web Speech API needs no CSP change: no blob:, ws(s):, worker or media origins.
    (csp,) = re.findall(r'add_header Content-Security-Policy "([^"]+)" always;', body)
    assert "connect-src 'self';" in csp
    assert "blob:" not in csp.split("connect-src", 1)[1].split(";", 1)[0]
    assert "worker-src" not in csp and "media-src" not in csp
    # JSON API responses are not documents: they keep the mic off.
    assert "microphone=()" in _nginx_body("aureon-api.conf")
