import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest

from app.criteria import merge_consecutive
from app.models import Sport
from app.providers.base import ProviderError
from app.providers.prince import (
    TZ,
    TkPrince,
    allocation_busy,
    is_winter,
    js_weekday,
    opening_minutes,
    resource_id,
    window_busy,
)

FIXTURE = Path(__file__).parent / "fixtures" / "prince_availability.json"
DAY = date(2026, 10, 28)  # Wednesday


@pytest.fixture
def fx() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
async def provider():
    p = TkPrince()
    yield p
    await p.aclose()


def starts_by_court(slots):
    out: dict[str, list[time]] = {}
    for s in slots:
        out.setdefault(s.court.id, []).append(s.start.time())
    for v in out.values():
        v.sort()
    return out


def test_helpers():
    assert resource_id("T3") == "r-t3"
    assert js_weekday(date(2026, 11, 1)) == 0  # Sunday
    assert js_weekday(date(2026, 10, 28)) == 3  # Wednesday


def test_is_winter_air_dome():
    assert is_winter(date(2026, 10, 1)) and is_winter(date(2027, 1, 15)) and is_winter(date(2027, 4, 16))
    assert not is_winter(date(2027, 4, 17)) and not is_winter(date(2026, 7, 1)) and not is_winter(date(2026, 9, 30))
    rules = {"winter_from_md": "11-01", "winter_to_md": "03-31"}
    assert not is_winter(date(2026, 10, 15), rules) and is_winter(date(2026, 11, 1), rules)
    assert is_winter(date(2026, 10, 15), {"winter_from_md": "bogus"})  # invalid -> defaults


def test_opening_minutes_seasons(fx):
    hours = fx["config"]["pricing_snapshot"]["hours"]
    assert opening_minutes(DAY, hours) == (480, 1380)  # winter
    assert opening_minutes(date(2026, 7, 1), hours) == (420, 1380)  # summer
    assert opening_minutes(date(2030, 6, 1), hours) == (420, 1380)  # no season -> site default
    weekday_only = [dict(hours[1], weekdays=[1, 2, 3, 4, 5])]
    assert opening_minutes(date(2026, 10, 31), weekday_only) == (420, 1380)  # Saturday falls back
    assert opening_minutes(DAY, [dict(hours[1], open_min=600, close_min=600)]) is None


def test_allocation_busy_handles_spans_and_full_days(fx):
    busy = allocation_busy(fx["allocations"], DAY)
    assert (540, 600) in busy["r-t5"]  # multi-day 27..31 Oct
    assert (600, 660) in busy["r-t1"]
    assert "r-t4" not in busy
    busy29 = allocation_busy(fx["allocations"], date(2026, 10, 29))
    assert (0, 1440) in busy29["r-t4"]


def test_window_busy_expansion():
    ids = ["r-t1", "r-t2"]
    windows = [
        {"active": True, "proposed": False, "resource_type": "tennis", "resource_ids": ["r-t2"],
         "from_date": "2026-10-01", "to_date": "2027-03-31", "weekdays": [], "start_min": None, "end_min": None},
        {"active": True, "proposed": False, "resource_type": "tennis", "resource_ids": [],
         "from_date": "2026-10-01", "to_date": "2027-03-31", "weekdays": [5], "start_min": 1020, "end_min": None},
        {"active": False, "resource_type": "tennis", "resource_ids": [], "from_date": "2026-01-01", "to_date": "2027-01-01"},
        {"active": True, "resource_type": "football", "resource_ids": [], "from_date": "2026-01-01", "to_date": "2027-01-01"},
    ]
    assert window_busy(windows, DAY, ids) == {"r-t2": [(0, 1440)]}
    friday = window_busy(windows, date(2026, 10, 30), ids)
    assert friday == {"r-t2": [(0, 1440), (1020, 1440)], "r-t1": [(1020, 1440)]}


def test_parse_day_free_units(provider, fx):
    slots = provider.parse_day(fx, DAY)
    starts = starts_by_court(slots)
    assert set(starts) == {"T1", "T2", "T3", "T4", "T5"}
    # winter hours 08:00-23:00 -> 30 units; T4 has nothing booked
    assert len(starts["T4"]) == 30 and starts["T4"][0] == time(8) and starts["T4"][-1] == time(22, 30)
    # T1 busy 10:00-11:00 and 17:00-20:00
    assert time(10) not in starts["T1"] and time(10, 30) not in starts["T1"] and time(11) in starts["T1"]
    assert time(17) not in starts["T1"] and time(19, 30) not in starts["T1"] and time(20) in starts["T1"]
    # T5: multi-day block 09:00-10:00 plus 18:00-21:30
    assert time(9) not in starts["T5"] and time(9, 30) not in starts["T5"]
    assert time(21) not in starts["T5"] and time(21, 30) in starts["T5"]
    assert all(s.end - s.start == timedelta(minutes=30) for s in slots)
    assert all(s.start.tzinfo is not None for s in slots)


def test_full_day_block(provider, fx):
    starts = starts_by_court(provider.parse_day(fx, date(2026, 10, 29)))
    assert "T4" not in starts and {"T1", "T2", "T3", "T5"} <= set(starts)


def test_court_metadata_and_deep_link(provider, fx):
    slots = provider.parse_day(fx, DAY)
    first_t2 = min((s for s in slots if s.court.id == "T2"), key=lambda s: s.start)
    assert first_t2.court.name == "Teren 2"
    assert first_t2.court.sport == Sport.TENNIS and first_t2.court.surface == "clay"
    assert first_t2.court.indoor is True  # late October: air dome
    assert first_t2.court.club.key == "prince"
    summer = provider.parse_day(fx, date(2026, 7, 1))
    assert summer and all(s.court.indoor is False for s in summer)
    assert first_t2.booking_url == "https://prince.hr/app/book?sport=tennis&court=T2&day=2026-10-28&start=08:00&dur=60"
    s2130 = next(s for s in slots if s.court.id == "T5" and s.start.time() == time(21, 30))
    assert s2130.booking_url.endswith("start=21:30&dur=60")


def test_merge_blocks(provider, fx):
    slots = provider.parse_day(fx, DAY)
    blocks = merge_consecutive([s for s in slots if s.court.id == "T1"])
    assert [(b.start.time(), b.end.time()) for b in blocks] == [
        (time(8), time(10)),
        (time(11), time(17)),
        (time(20), time(23)),
    ]


def test_lead_time_cutoff(provider, fx):
    not_before = datetime.combine(DAY, time(12, 10), tzinfo=TZ)
    starts = starts_by_court(provider.parse_day(fx, DAY, not_before=not_before))
    assert starts["T4"][0] == time(12, 30)


def test_weekend_disabled_and_not_in_offer(provider, fx):
    payload = json.loads(json.dumps(fx))
    payload["config"]["pricing_snapshot"]["booking_rules"]["weekend_enabled"] = False
    assert provider.parse_day(payload, date(2026, 10, 31)) == []  # Saturday
    assert provider.parse_day(payload, DAY)  # weekday unaffected
    payload["config"]["pricing_snapshot"]["spaces"]["T3"]["in_offer"] = False
    assert "T3" not in starts_by_court(provider.parse_day(payload, DAY))


def test_windows_block_courts(provider, fx):
    payload = json.loads(json.dumps(fx))
    payload["config"]["pricing_snapshot"]["windows"] = [
        {"id": "league-t2", "kind": "league", "active": True, "proposed": False, "resource_type": "tennis",
         "resource_ids": ["r-t2"], "from_date": "2026-10-01", "to_date": "2027-03-31", "weekdays": [],
         "start_min": None, "end_min": None},
    ]
    starts = starts_by_court(provider.parse_day(payload, DAY))
    assert "T2" not in starts and "T1" in starts


def test_defaults_without_spaces(provider, fx):
    payload = json.loads(json.dumps(fx))
    del payload["config"]["pricing_snapshot"]["spaces"]
    assert set(starts_by_court(provider.parse_day(payload, DAY))) == {"T1", "T2", "T3", "T4", "T5"}


async def test_fetch_clamps_horizon_and_lead_time(fx):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path.endswith("/availability")
        return httpx.Response(200, json=fx)

    p = TkPrince()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = datetime.now(TZ)
    today = now.date()
    try:
        slots = await p.fetch(today - timedelta(days=2), today + timedelta(days=30))
    finally:
        await p.aclose()

    assert len(calls) == 1  # one request serves every day
    days = {s.start.date() for s in slots}
    horizon = fx["config"]["pricing_snapshot"]["booking_rules"]["max_days_ahead"]
    assert min(days) >= today and max(days) <= today + timedelta(days=horizon)
    assert all(s.start >= now + timedelta(minutes=60) for s in slots if s.start.date() == today)


async def test_fetch_error_surfaces():
    p = TkPrince()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    try:
        with pytest.raises(ProviderError, match="HTTP 500"):
            await p.fetch(DAY, DAY)
    finally:
        await p.aclose()
