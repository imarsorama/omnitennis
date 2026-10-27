import asyncio
from datetime import date

import pytest

from app.aggregator import Aggregator
from app.models import Club, Court, Slot
from app.providers.base import Provider, ProviderError

from .conftest import SATURDAY, criteria, dt


class FakeProvider(Provider):
    def __init__(self, key: str, *, fail: bool = False, delay: float = 0.0) -> None:
        self.key = key
        self.club = Club(key=key, name=key.title(), url=f"https://{key}.test")
        self.court = Court(club=self.club, id="1", name="Court 1")
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderError("boom")
        return [
            Slot(court=self.court, start=dt(SATURDAY, 10), end=dt(SATURDAY, 11), booking_url="u"),
            Slot(court=self.court, start=dt(SATURDAY, 11), end=dt(SATURDAY, 12), booking_url="u"),
        ]


def make(providers, tz, ttl=60.0, timeout=1.0, now=None) -> Aggregator:
    fixed = now or dt(SATURDAY, 0)
    return Aggregator(
        providers, tz=tz, cache_ttl_seconds=ttl, provider_timeout_seconds=timeout, clock=lambda: fixed
    )


async def test_past_slots_are_dropped(tz):
    p = FakeProvider("p")
    agg = make([p], tz, now=dt(SATURDAY, 10, 30))  # first slot (10-11) already started
    (r,) = await agg.availability(criteria(SATURDAY, min_minutes=60))
    assert [(b.start.hour, b.end.hour) for b in r.blocks] == [(11, 12)]


async def test_error_isolation(tz):
    good, bad = FakeProvider("good"), FakeProvider("bad", fail=True)
    agg = make([good, bad], tz)
    results = await agg.availability(criteria(SATURDAY))
    by_key = {r.provider: r for r in results}
    assert by_key["good"].status == "ok" and len(by_key["good"].blocks) == 1
    assert by_key["bad"].status == "error" and "boom" in (by_key["bad"].error or "")
    assert by_key["bad"].blocks == []


async def test_timeout_is_reported_as_error(tz):
    slow = FakeProvider("slow", delay=0.5)
    agg = make([slow], tz, timeout=0.05)
    (res,) = await agg.availability(criteria(SATURDAY))
    assert res.status == "error" and "Timed out" in (res.error or "")


async def test_cache_hit_and_force_refresh(tz):
    p = FakeProvider("p")
    agg = make([p], tz)
    (r1,) = await agg.availability(criteria(SATURDAY))
    (r2,) = await agg.availability(criteria(SATURDAY))
    assert r1.status == "ok" and r2.status == "cached"
    assert p.calls == 1
    (r3,) = await agg.availability(criteria(SATURDAY), force=True)
    assert r3.status == "ok" and p.calls == 2


async def test_cache_expires(tz):
    p = FakeProvider("p")
    agg = make([p], tz, ttl=0.01)
    await agg.availability(criteria(SATURDAY))
    await asyncio.sleep(0.03)
    (r,) = await agg.availability(criteria(SATURDAY))
    assert r.status == "ok" and p.calls == 2


async def test_criteria_change_reuses_cached_raw_slots(tz):
    p = FakeProvider("p")
    agg = make([p], tz)
    (r1,) = await agg.availability(criteria(SATURDAY))
    (r2,) = await agg.availability(criteria(SATURDAY, min_minutes=180))
    assert p.calls == 1
    assert len(r1.blocks) == 1 and r2.blocks == []


async def test_club_filter_skips_providers(tz):
    a, b = FakeProvider("a"), FakeProvider("b")
    agg = make([a, b], tz)
    results = await agg.availability(criteria(SATURDAY, clubs=["b"]))
    assert [r.provider for r in results] == ["b"]
    assert a.calls == 0


async def test_errors_are_not_cached(tz):
    p = FakeProvider("p", fail=True)
    agg = make([p], tz)
    await agg.availability(criteria(SATURDAY))
    p.fail = False
    (r,) = await agg.availability(criteria(SATURDAY))
    assert r.status == "ok" and p.calls == 2
