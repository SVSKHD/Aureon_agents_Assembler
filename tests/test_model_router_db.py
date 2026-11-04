"""ai.model_calls rows and GET /api/v1/ai/usage against Postgres (docs/model-routing.md)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.ollama.limiter import GenerationLimiter
from app.services.ai_usage import UsageTracker
from app.services.model_router import ModelRouter
from tests import test_finance_db
from tests.test_finance_db import EnvFactory
from tests.test_model_router import (
    PERSONAL,
    PUBLIC_NEWS,
    FakeOllama,
    FakeOpenRouter,
    MemRedis,
    _settings,
)

pytestmark = pytest.mark.db
make_env = test_finance_db.make_env


async def test_every_call_is_recorded_without_content_and_summed(
    make_env: EnvFactory, db_clean: str
) -> None:
    async with make_env() as env:
        engine = create_async_engine(db_clean)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        r = ModelRouter(
            _settings(env.app.state.settings),
            ollama=FakeOllama(),  # type: ignore[arg-type]
            openrouter=FakeOpenRouter(),  # type: ignore[arg-type]
            usage=UsageTracker(MemRedis()),
            maker=maker,
            limiter=GenerationLimiter(1, 2, 5),
        )
        await r.complete("news", PUBLIC_NEWS, context="news")
        await r.complete("voice", PERSONAL)
        rows = await env.sql(
            "SELECT agent, provider, outcome, route_reason, local_only, prompt_tokens, cost_usd"
            " FROM ai.model_calls ORDER BY id"
        )
        assert [tuple(x) for x in rows] == [
            ("news", "openrouter", "ok", None, False, 30, Decimal("0.00020000")),
            ("voice", "ollama", "ok", "filter", True, 25, None),
        ]
        cols = await env.sql(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='ai'"
            " AND table_name='model_calls'"
        )
        assert not {"prompt", "content", "answer", "messages", "text"} & {c[0] for c in cols}

        r2 = await env.get("admin", "/api/v1/ai/usage")
        assert r2.status_code == 200
        items = {(i["agent"], i["provider"]): i for i in r2.json()["items"]}
        assert items[("news", "openrouter")]["calls"] == 1
        assert items[("voice", "ollama")]["local_only"] == 1
        assert (await env.get("a", "/api/v1/ai/usage")).status_code == 403
        await engine.dispose()
