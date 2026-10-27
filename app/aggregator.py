"""Fan-out to providers, cache raw slots, normalize and filter results."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .cache import TTLCache
from .criteria import process
from .models import Criteria, ProviderResult, Slot
from .providers.base import Provider

log = logging.getLogger(__name__)


class Aggregator:
    def __init__(
        self,
        providers: list[Provider],
        *,
        tz: ZoneInfo,
        cache_ttl_seconds: float,
        provider_timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.providers = providers
        self.tz = tz
        self.timeout = provider_timeout_seconds
        self.cache: TTLCache[list[Slot]] = TTLCache(cache_ttl_seconds)
        self._clock = clock or (lambda: datetime.now(self.tz))

    async def aclose(self) -> None:
        await asyncio.gather(*(p.aclose() for p in self.providers), return_exceptions=True)

    async def _fetch_one(
        self, provider: Provider, date_from: date, date_to: date, force: bool
    ) -> tuple[list[Slot], str, datetime, int, str | None]:
        """Return (slots, status, fetched_at, elapsed_ms, error)."""
        key = (provider.key, date_from, date_to)
        if not force:
            entry = await self.cache.get(key)
            if entry is not None:
                return (
                    entry.value,
                    "cached",
                    datetime.fromtimestamp(entry.stored_wall, tz=timezone.utc),
                    0,
                    None,
                )

        t0 = time.monotonic()
        try:
            slots = await asyncio.wait_for(provider.fetch(date_from, date_to), timeout=self.timeout)
        except asyncio.TimeoutError:
            msg = f"Timed out after {self.timeout:.0f}s"
            log.warning("provider %s: %s", provider.key, msg)
            return [], "error", datetime.now(timezone.utc), int((time.monotonic() - t0) * 1000), msg
        except Exception as exc:  # noqa: BLE001 - isolate any provider failure
            log.exception("provider %s failed", provider.key)
            msg = f"{type(exc).__name__}: {exc}"
            return [], "error", datetime.now(timezone.utc), int((time.monotonic() - t0) * 1000), msg

        entry = await self.cache.set(key, slots)
        elapsed = int((time.monotonic() - t0) * 1000)
        return slots, "ok", datetime.fromtimestamp(entry.stored_wall, tz=timezone.utc), elapsed, None

    async def availability(self, criteria: Criteria, *, force: bool = False) -> list[ProviderResult]:
        selected = [
            p for p in self.providers if not criteria.clubs or p.club.key in criteria.clubs
        ]
        raw = await asyncio.gather(
            *(self._fetch_one(p, criteria.date_from, criteria.date_to, force) for p in selected)
        )
        results: list[ProviderResult] = []
        now = self._clock()
        for provider, (slots, status, fetched_at, elapsed, error) in zip(selected, raw, strict=True):
            blocks = process(slots, criteria, now=now) if slots else []
            results.append(
                ProviderResult(
                    provider=provider.key,
                    club=provider.club,
                    status=status,  # type: ignore[arg-type]
                    blocks=blocks,
                    error=error,
                    fetched_at=fetched_at,
                    elapsed_ms=elapsed,
                )
            )
        return results
