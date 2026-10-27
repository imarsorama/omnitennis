"""Fixture-driven providers for development and tests.

Availability is generated deterministically from (club, date) so repeated
loads are stable within a day while still looking realistic. Raw data includes
padel courts to exercise the "tennis only" rule at adapter level.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..models import Club, Court, Slot, Sport
from .base import Provider, ProviderError, register

TZ = ZoneInfo("Europe/Zagreb")


class _MockBase(Provider):
    slot_minutes: int
    open_hour: int
    close_hour: int
    courts_spec: list[tuple[str, str, Sport, bool | None, str | None]]
    busy_ratio: float = 0.55
    delay_s: float = 0.2

    @property
    def courts(self) -> list[Court]:
        return [
            Court(club=self.club, id=cid, name=name, sport=sport, indoor=indoor, surface=surface)
            for cid, name, sport, indoor, surface in self.courts_spec
        ]

    def booking_url(self, court: Court, start: datetime) -> str:
        return f"{self.club.url}/book?court={court.id}&date={start.date().isoformat()}&time={start:%H:%M}"

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        await asyncio.sleep(self.delay_s)
        slots: list[Slot] = []
        day = date_from
        while day <= date_to:
            for court in self.courts:
                if court.sport != Sport.TENNIS:
                    continue  # adapter-level exclusion of padel/squash
                rng = random.Random(f"{self.key}:{court.id}:{day.isoformat()}")
                # Weekends are busier in the middle of the day; weekdays in the evening.
                t = datetime.combine(day, time(self.open_hour, 0), tzinfo=TZ)
                close = datetime.combine(day, time(self.close_hour, 0), tzinfo=TZ)
                while t < close:
                    hour = t.hour
                    if day.weekday() >= 5:
                        busy_p = 0.75 if 10 <= hour < 18 else 0.35
                    else:
                        busy_p = 0.8 if 17 <= hour < 21 else 0.4
                    end = t + timedelta(minutes=self.slot_minutes)
                    if rng.random() > busy_p:
                        slots.append(
                            Slot(court=court, start=t, end=end, booking_url=self.booking_url(court, t))
                        )
                    t = end
            day += timedelta(days=1)
        return slots


@register
class MockSalata(_MockBase):
    key = "mock_salata"
    club = Club(
        key="mock_salata",
        name="TK Šalata (mock)",
        url="https://mock-salata.example",
        address="Schlosserove stube 2, Zagreb",
    )
    slot_minutes = 60
    open_hour = 7
    close_hour = 23
    courts_spec = [
        ("1", "Teren 1", Sport.TENNIS, False, "clay"),
        ("2", "Teren 2", Sport.TENNIS, False, "clay"),
        ("3", "Teren 3", Sport.TENNIS, False, "clay"),
        ("h1", "Balon 1", Sport.TENNIS, True, "clay"),
        ("p1", "Padel 1", Sport.PADEL, True, None),
    ]


@register
class MockJarun(_MockBase):
    key = "mock_jarun"
    club = Club(
        key="mock_jarun",
        name="TC Jarun (mock)",
        url="https://mock-jarun.example",
        address="Aleja Matije Ljubeka, Zagreb",
    )
    slot_minutes = 30
    open_hour = 6
    close_hour = 22
    courts_spec = [
        ("c1", "Court A", Sport.TENNIS, True, "hard"),
        ("c2", "Court B", Sport.TENNIS, True, "hard"),
        ("c3", "Court C", Sport.TENNIS, None, None),
        ("s1", "Squash 1", Sport.SQUASH, True, None),
    ]


@register
class MockBroken(_MockBase):
    """Always fails; enable it to see how provider errors are surfaced."""

    key = "mock_broken"
    club = Club(key="mock_broken", name="Broken Club (mock)", url="https://mock-broken.example")
    slot_minutes = 60
    open_hour = 8
    close_hour = 22
    courts_spec = []

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        await asyncio.sleep(0.1)
        raise ProviderError("Simulated upstream failure (HTTP 503)")
