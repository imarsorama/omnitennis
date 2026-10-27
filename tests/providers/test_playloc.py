import json
from datetime import date, time
from pathlib import Path

import httpx
import pytest

from app.criteria import merge_consecutive
from app.models import Sport
from app.providers.playloc import HatkMladost, parse_info

FIXTURE = Path(__file__).parent / "fixtures" / "playloc_hatk_mladost.json"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def provider():
    p = HatkMladost()
    yield p
    await p.aclose()


def test_parse_info():
    assert parse_info(["Unutarnji (Grijano)", "Podloga: Zemlja"]) == (True, "clay")
    assert parse_info(["Vanjski", "Podloga: Hard"]) == (False, "hard")
    assert parse_info([]) == (None, None)
    assert parse_info(None) == (None, None)


def test_parse_page_free_units(provider, page):
    slots, min_d, max_d = provider.parse_page(page)
    assert (min_d, max_d) == (date(2026, 10, 26), date(2026, 11, 2))

    # 2026-10-27, Teren 1 (id 97): open 07-23, taken 07:00..21:30 -> free 22:00-23:00
    day = date(2026, 10, 27)
    t1 = sorted(s.start.time() for s in slots if s.court.id == "97" and s.start.date() == day)
    assert t1 == [time(22, 0), time(22, 30)]

    # Teren 6 (id 59): free 07:00-08:00, 12:00-13:00, 22:00-23:00
    t6 = sorted(s.start.time() for s in slots if s.court.id == "59" and s.start.date() == day)
    assert t6 == [time(7, 0), time(7, 30), time(12, 0), time(12, 30), time(22, 0), time(22, 30)]

    # Teren 17 hard court (id 61): open 08-18, only 08:00-08:30 free
    t17 = sorted(s.start.time() for s in slots if s.court.id == "61" and s.start.date() == day)
    assert t17 == [time(8, 0)]


def test_today_cutoff_unavailable_until(provider, page):
    # 2026-10-26 has unavailableUntil 23:00 -> nothing bookable today
    slots, _, _ = provider.parse_page(page)
    assert not [s for s in slots if s.start.date() == date(2026, 10, 26)]


def test_court_metadata(provider, page):
    slots, _, _ = provider.parse_page(page)
    courts = {s.court.id: s.court for s in slots}
    assert courts["97"].indoor is True and courts["97"].surface == "clay"
    assert courts["61"].indoor is False and courts["61"].surface == "hard"
    assert all(c.sport == Sport.TENNIS for c in courts.values())
    assert all(c.club.key == "mladost" for c in courts.values())


def test_merging_yields_blocks(provider, page):
    slots, _, _ = provider.parse_page(page)
    blocks = merge_consecutive([s for s in slots if s.court.id == "59" and s.start.date() == date(2026, 10, 27)])
    assert [(b.start.time(), b.end.time()) for b in blocks] == [
        (time(7, 0), time(8, 0)),
        (time(12, 0), time(13, 0)),
        (time(22, 0), time(23, 0)),
    ]


def test_booking_url_and_timezone(provider, page):
    slots, _, _ = provider.parse_page(page)
    s = slots[0]
    assert s.booking_url.startswith("https://hatk-mladost.playloc.app/?date=")
    assert s.start.tzinfo is not None and s.start.utcoffset() is not None


def test_closed_day_and_non_tennis_sport(provider, page):
    page["props"]["data"]["2026-10-27"]["open"] = False
    slots, _, _ = provider.parse_page(page)
    assert not [s for s in slots if s.start.date() == date(2026, 10, 27)]

    page["props"]["selectedSport"] = {"slug": "padel"}
    slots, _, _ = provider.parse_page(page)
    assert slots == []


async def test_fetch_paginates_with_mock_transport(page):
    """fetch() reads HTML first, then Inertia JSON pages stepping 3 days, clamped to maxDate."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"date": request.url.params.get("date"), "inertia": request.headers.get("x-inertia")})
        day = date.fromisoformat(request.url.params["date"])
        data = {}
        for i in range(3):
            d = day.fromordinal(day.toordinal() + i)
            data[d.isoformat()] = page["props"]["data"]["2026-10-27"]
        body = {"component": page["component"], "version": page["version"], "props": {**page["props"], "data": data}}
        if request.headers.get("x-inertia"):
            return httpx.Response(200, json=body, headers={"content-type": "application/json"})
        encoded = json.dumps(body).replace("&", "&amp;").replace('"', "&quot;")
        return httpx.Response(200, text=f'<div id="app" data-page="{encoded}"></div>')

    p = HatkMladost()
    p.client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(handler))
    try:
        slots = await p.fetch(date(2026, 10, 27), date(2026, 11, 9))  # 14 days requested
    finally:
        await p.aclose()

    assert [c["date"] for c in calls] == ["2026-10-27", "2026-10-30", "2026-11-02"]  # clamped to maxDate 11-02
    assert calls[0]["inertia"] is None and all(c["inertia"] == "true" for c in calls[1:])
    days = sorted({s.start.date() for s in slots})
    assert days[0] == date(2026, 10, 27) and days[-1] == date(2026, 11, 4)
