"""A small in-memory stand-in for redis.asyncio.Redis (only what the Dhan code uses).

`fail = True` makes every command raise redis.exceptions.ConnectionError, so tests can take
Redis "down" and bring it back. Pub/sub delivers to subscribers created from the same fake.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError


def _b(v: Any) -> bytes:
    return v if isinstance(v, bytes) else str(v).encode()


class FakePubSub:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.channels: set[str] = set()
        self.q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, *channels: str) -> None:
        self.r._check()
        self.channels.update(channels)
        self.r._subs.append(self)

    async def get_message(
        self,
        ignore_subscribe_messages: bool = False,
        timeout: float = 0.0,  # noqa: ASYNC109 - redis-py signature
    ) -> dict[str, Any] | None:
        self.r._check()
        try:
            return await asyncio.wait_for(self.q.get(), max(timeout, 0.001))
        except TimeoutError:
            self.r._check()
            return None

    async def aclose(self) -> None:
        if self in self.r._subs:
            self.r._subs.remove(self)


class FakePipeline:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def queue(*a: Any, **kw: Any) -> FakePipeline:
            self.ops.append((name, a, kw))
            return self

        return queue

    async def execute(self) -> list[Any]:
        self.r._check()
        return [await getattr(self.r, n)(*a, **kw) for n, a, kw in self.ops]


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.hashes: dict[str, dict[bytes, bytes]] = {}
        self.lists: dict[str, list[bytes]] = {}
        self.expiry: dict[str, float] = {}
        self.published: list[tuple[str, bytes]] = []
        self._subs: list[FakePubSub] = []
        self.fail = False
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise RedisConnectionError("fake redis is down")

    def _alive(self, key: str) -> bool:
        exp = self.expiry.get(key)
        if exp is not None and exp <= time.time():
            for d in (self.kv, self.hashes, self.lists):
                d.pop(key, None)  # type: ignore[attr-defined]
            self.expiry.pop(key, None)
            return False
        return True

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> bytes | None:
        self._check()
        return self.kv.get(key) if self._alive(key) else None

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: float | None = None,
    ) -> bool | None:
        self._check()
        exists = self._alive(key) and key in self.kv
        if (nx and exists) or (xx and not exists):
            return None
        self.kv[key] = _b(value)
        if ex is not None:
            self.expiry[key] = time.time() + float(ex)
        else:
            self.expiry.pop(key, None)
        return True

    async def delete(self, *keys: str) -> int:
        self._check()
        n = 0
        for k in keys:
            for d in (self.kv, self.hashes, self.lists):
                if k in d:
                    del d[k]  # type: ignore[attr-defined]
                    n += 1
            self.expiry.pop(k, None)
        return n

    async def expire(self, key: str, seconds: float) -> bool:
        self._check()
        self.expiry[key] = time.time() + float(seconds)
        return True

    async def ttl(self, key: str) -> int:
        self._check()
        exp = self.expiry.get(key)
        return -1 if exp is None else max(0, int(exp - time.time()))

    async def hset(
        self, key: str, field: Any = None, value: Any = None, mapping: dict[Any, Any] | None = None
    ) -> int:
        self._check()
        h = self.hashes.setdefault(key, {})
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        for k, v in items.items():
            h[_b(k)] = _b(v)
        return len(items)

    async def hget(self, key: str, field: Any) -> bytes | None:
        self._check()
        return self.hashes.get(key, {}).get(_b(field))

    async def hgetall(self, key: str) -> dict[bytes, bytes]:
        self._check()
        return dict(self.hashes.get(key, {}))

    async def publish(self, channel: str, message: Any) -> int:
        self._check()
        data = _b(message)
        self.published.append((channel, data))
        n = 0
        for sub in list(self._subs):
            if channel in sub.channels:
                sub.q.put_nowait({"type": "message", "channel": _b(channel), "data": data})
                n += 1
        return n

    async def lpush(self, key: str, *values: Any) -> int:
        self._check()
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, _b(v))
        return len(lst)

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        self._check()
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : (end + 1 if end != -1 else None)]
        return True

    async def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        self._check()
        lst = self.lists.get(key, [])
        return lst[start : (end + 1 if end != -1 else None)]

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def aclose(self) -> None:
        return None
