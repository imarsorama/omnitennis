import json
from datetime import date, time
from pathlib import Path

import httpx
import pytest

from app.criteria import merge_consecutive
from app.models import Sport
from app.providers.sportyplus import TkMaksimir, court_indoor, court_sport, court_surface

FIXTURE = Path(__file__).parent / "fixtures" / "sportyplus_maksimir.json"
DAY = date(2026, 10, 27)


@pytest.fixture
def body() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def provider():
    p = TkMaksimir()
    yield p
    await p.aclose()


def test_court_mapping_helpers():
    assert court_sport({"sports": [{"slug": "tennis"}]}) == Sport.TENNIS
    assert court_sport({"sports": [{"slug": "squash"}]}) == Sport.SQUASH
    assert court_sport({"sports": []}) == Sport.TENNIS
    assert court_indoor({"type": "open"}) is False
    assert court_indoor({"type": "closed"}) is True
    assert court_indoor({"type": "weird"}) is None
    assert court_surface({"surface": {"badge": "acrylic"}}) == "hard"
    assert court_surface({"surface": {"badge": "clay"}}) == "clay"
    assert court_surface({"surface": {"badge": "other"}}) is None


def test_parse_day_free_units(provider, body):
    slots = provider.parse_day(DAY, body["data"])
    by_court = {}
    for s in slots:
        by_court.setdefault(s.court.id, []).append(s.start.time())
    # Akril vanjski (1170): 07:00 free, 08:00-21:00 booked, 22:00 free
    assert sorted(by_court["1170"]) == [time(7), time(22)]
    # every slot is 60 minutes
    assert {int((s.end - s.start).total_seconds() // 60) for s in slots} == {60}


def test_squash_excluded_and_metadata(provider, body):
    slots = provider.parse_day(DAY, body["data"])
    courts = {s.court.id: s.court for s in slots}
    assert "278" not in courts  # Squash
    assert courts["1170"].indoor is False and courts["1170"].surface == "hard"
    assert courts["373"].indoor is False and courts["373"].surface == "clay"
    assert courts["273"].indoor is True and courts["273"].name == "Dvorana 1"
    assert all(c.sport == Sport.TENNIS and c.club.key == "maksimir" for c in courts.values())


def test_duplicate_names_are_disambiguated(provider, body):
    courts = provider.build_courts(body["data"])
    names = sorted(c.name for c in courts.values() if c.name.startswith("Akril"))
    assert names == ["Akril vanjski #1", "Akril vanjski #2"]


def test_hidden_units_skipped(provider, body):
    body["data"][0]["parsed_reservations"]["700"]["hidden"] = True
    slots = provider.parse_day(DAY, body["data"])
    assert [s.start.time() for s in slots if s.court.id == "1170"] == [time(22)]


def test_merging(provider, body):
    slots = provider.parse_day(DAY, body["data"])
    blocks = merge_consecutive([s for s in slots if s.court.id == "1326"])
    assert all(b.minutes % 60 == 0 for b in blocks)
    assert blocks[0].start.tzinfo is not None


async def test_fetch_one_request_per_day_and_pagination(body):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/club/this"):
            return httpx.Response(200, json={"id": 9, "subdomain": "maksimir"})
        page = request.url.params.get("page")
        calls.append((request.url.params["date"], page))
        if page == "1":
            data = body["data"][:4]
            return httpx.Response(
                200,
                json={"data": data, "links": {"next": "x"}, "meta": {"current_page": 1, "last_page": 2}},
            )
        return httpx.Response(
            200, json={"data": body["data"][4:], "links": {"next": None}, "meta": {"current_page": 2, "last_page": 2}}
        )

    p = TkMaksimir()
    p._club_id = None  # force resolution via /club/this
    p.client = httpx.AsyncClient(base_url="https://api.tenis.plus/v2", transport=httpx.MockTransport(handler))
    try:
        slots = await p.fetch(date(2026, 10, 27), date(2026, 10, 29))
    finally:
        await p.aclose()

    assert sorted(calls) == [
        ("2026-10-27", "1"), ("2026-10-27", "2"),
        ("2026-10-28", "1"), ("2026-10-28", "2"),
        ("2026-10-29", "1"), ("2026-10-29", "2"),
    ]
    assert {s.start.date() for s in slots} == {date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)}
    assert {s.court.id for s in slots} >= {"1170", "273"}  # from both pages


async def test_club_resolution_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 26, "subdomain": "my"})

    p = TkMaksimir()
    p._club_id = None
    p.client = httpx.AsyncClient(base_url="https://api.tenis.plus/v2", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception, match="club resolution"):
            await p.fetch(date(2026, 10, 27), date(2026, 10, 27))
    finally:
        await p.aclose()
