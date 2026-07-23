"""TTL + LRU cache (pattern ported from HexStrike's HexStrikeCache).

Used for: model discovery results, process lists, analysis results.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, max_size: int = 256, ttl: float = 300.0):
        self.max_size = max_size
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            ts, value = self._data[key]
            if time.time() - ts > self.ttl:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "size": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
