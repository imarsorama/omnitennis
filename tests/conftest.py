import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

# API tests run against the offline mock providers regardless of local .env.
os.environ["OMNI_ENABLED_PROVIDERS"] = "mock_salata,mock_jarun"

from app.models import Club, Court, Criteria, Slot, Sport

TZ = ZoneInfo("Europe/Zagreb")


@pytest.fixture
def tz() -> ZoneInfo:
    return TZ


@pytest.fixture
def club() -> Club:
    return Club(key="tc", name="Test Club", url="https://example.test")


@pytest.fixture
def court(club: Club) -> Court:
    return Court(club=club, id="1", name="Court 1", sport=Sport.TENNIS, indoor=False)


def dt(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=TZ)


def slot(court: Court, d: date, h0: float, h1: float) -> Slot:
    def to_dt(h: float) -> datetime:
        return dt(d, int(h), int(round((h - int(h)) * 60)))

    return Slot(court=court, start=to_dt(h0), end=to_dt(h1), booking_url="https://example.test/book")


def criteria(d_from: date, d_to: date | None = None, **kw) -> Criteria:
    return Criteria(date_from=d_from, date_to=d_to or d_from, **kw)


# Known calendar anchors
MONDAY = date(2026, 11, 2)
SATURDAY = date(2026, 11, 7)
SUNDAY = date(2026, 11, 8)
DST_END_SUNDAY = date(2026, 10, 25)  # clocks go back 03:00 -> 02:00 in Europe/Zagreb

__all__ = ["TZ", "dt", "slot", "criteria", "MONDAY", "SATURDAY", "SUNDAY", "DST_END_SUNDAY"]
