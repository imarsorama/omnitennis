from __future__ import annotations

import asyncio
import time
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    stored_at: float  # time.monotonic()
    stored_wall: float  # time.time()

    def age(self) -> float:
        return time.monotonic() - self.stored_at


class TTLCache(Generic[T]):
    """Minimal async-safe in-memory TTL cache."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._data: dict[Hashable, CacheEntry[T]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: Hashable) -> CacheEntry[T] | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.age() > self.ttl:
                del self._data[key]
                return None
            return entry

    async def set(self, key: Hashable, value: T) -> CacheEntry[T]:
        entry = CacheEntry(value=value, stored_at=time.monotonic(), stored_wall=time.time())
        async with self._lock:
            self._data[key] = entry
        return entry

    async def invalidate(self, key: Hashable | None = None) -> None:
        async with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)
