import asyncio
import time

import pytest

from synapse.cache import TTLCache
from synapse.tasks import TaskRegistry


def test_cache_set_get():
    c = TTLCache(max_size=3, ttl=60)
    c.set("a", 1)
    assert c.get("a") == 1
    assert c.get("missing") is None
    assert c.stats()["hits"] == 1
    assert c.stats()["misses"] == 1


def test_cache_lru_eviction():
    c = TTLCache(max_size=2, ttl=60)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")  # touch a so b becomes LRU
    c.set("c", 3)
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3


def test_cache_ttl_expiry():
    c = TTLCache(max_size=5, ttl=0.05)
    c.set("x", 42)
    assert c.get("x") == 42
    time.sleep(0.08)
    assert c.get("x") is None


def test_task_registry_lifecycle():
    reg = TaskRegistry()
    tid = reg.register("analyze", "test job")
    assert reg.get(tid)["status"] == "running"
    reg.progress(tid, "halfway")
    assert reg.get(tid)["progress"] == "halfway"
    reg.complete(tid, {"ok": True})
    t = reg.get(tid)
    assert t["status"] == "done"
    assert t["result"] == {"ok": True}
    assert t["finished"] is not None
    assert reg.stats()["done"] == 1


@pytest.mark.asyncio
async def test_task_cancel():
    reg = TaskRegistry()
    tid = reg.register("scan", "long scan")

    async def forever():
        await asyncio.sleep(100)

    at = asyncio.create_task(forever())
    reg.attach(tid, at)
    assert reg.cancel(tid) is True
    assert reg.get(tid)["status"] == "cancelled"
    assert reg.cancel(tid) is False  # already done
    with pytest.raises(asyncio.CancelledError):
        await at
