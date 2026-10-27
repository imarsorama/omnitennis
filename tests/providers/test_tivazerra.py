import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest

from app.criteria import merge_consecutive
from app.models import Sport
from app.providers.tivazerra import TZ, TkZagi, venue_indoor

FIXTURE = Path(__file__).parent / "fixtures" / "tivazerra_zagi.json"
DAY = date(2026, 10, 27)


@pytest.fixture
def fx() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def provider():
    p = TkZagi()
    yield p
    await p.aclose()


def test_venue_indoor_heuristics():
    assert venue_indoor({"Name": "Dvorana", "Description": "Paviljon 6"}) is True
    assert venue_indoor({"Name": "Vanjski tereni", "Description": ""}) is False
    assert venue_indoor({"Name": "Tereni", "Description": ""}) is None


def test_parse_day_free_units(provider, fx):
    venue = fx["venues"][0]
    slots = provider.parse_day(venue, DAY, fx["day"])
    starts = {}
    for s in slots:
        starts.setdefault(s.court.id, []).append(s.start.time())
    # Teren 1: open 07-23; busy 8-10,10-12,15-18,18-23 -> free 07:00-08:00, 12:00-15:00
    assert sorted(starts["1:1"]) == [time(7), time(7, 30), time(12), time(12, 30), time(13), time(13, 30), time(14), time(14, 30)]
    # Teren 8: busy ... 12-14, 14.5-16.5 -> 14:00-14:30 free (half-hour granularity)
    assert time(14, 0) in starts["1:8"] and time(14, 30) not in starts["1:8"]
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)


def test_court_metadata_and_url(provider, fx):
    venue = fx["venues"][0]
    slots = provider.parse_day(venue, DAY, fx["day"])
    court = slots[0].court
    assert court.sport == Sport.TENNIS and court.indoor is True and court.club.key == "zagi"
    assert court.name == "Teren 1"
    assert slots[0].booking_url == "https://termini.zagi-sport.hr/Reservations/Tennis/2026-10-27"
    assert slots[0].start.tzinfo is not None


def test_venue_prefix_when_multiple_venues(provider, fx):
    venue = fx["venues"][0]
    slots = provider.parse_day(venue, DAY, fx["day"], prefix_venue=True)
    assert slots[0].court.name == "Dvorana - Teren 1"


def test_closed_day(provider, fx):
    payload = dict(fx["day"], Hours=dict(fx["day"]["Hours"], Closed=True))
    assert provider.parse_day(fx["venues"][0], DAY, payload) == []


def test_lead_time_cutoff(provider, fx):
    not_before = datetime.combine(DAY, time(12, 45), tzinfo=TZ)
    slots = provider.parse_day(fx["venues"][0], DAY, fx["day"], not_before=not_before)
    t1 = sorted(s.start.time() for s in slots if s.court.id == "1:1")
    assert t1[0] == time(13, 0)


def test_merge_blocks(provider, fx):
    slots = provider.parse_day(fx["venues"][0], DAY, fx["day"])
    blocks = merge_consecutive([s for s in slots if s.court.id == "1:1"])
    assert [(b.start.time(), b.end.time()) for b in blocks] == [(time(7), time(8)), (time(12), time(15))]


async def test_fetch_filters_venues_and_clamps_horizon(fx, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/venues":
            return httpx.Response(200, json=fx["venues"])
        if request.url.path == "/api/reservations/settings":
            return httpx.Response(200, json=dict(fx["settings"], FutureReservationLimit=2))
        assert request.url.path.startswith("/api/venue/1/day/"), request.url.path  # badminton venue 3 never queried
        return httpx.Response(200, json=fx["day"])

    p = TkZagi()
    p.client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(handler))
    today = datetime.now(TZ).date()
    try:
        slots = await p.fetch(today, today + timedelta(days=13))
    finally:
        await p.aclose()

    day_calls = sorted(c for c in calls if "/day/" in c)
    assert len(day_calls) == 3  # today, +1, +2 (clamped by FutureReservationLimit=2)
    assert all(c.startswith("/api/venue/1/") for c in day_calls)
    assert {s.start.date() for s in slots} <= {today + timedelta(days=i) for i in range(3)}
    assert all(s.court.name.startswith("Teren") for s in slots)  # single tennis venue: no prefix
