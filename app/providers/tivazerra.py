"""Adapter for clubs using the Tivazerra reservation system (by Origo Kod).

Example: TK Zagi at https://termini.zagi-sport.hr. Plain JSON API, no login:

- ``GET /api/venues`` - venues with ``SportUrlName`` (``Tennis``, ``Badminton``...)
- ``GET /api/reservations/settings`` - ``FutureReservationLimit`` (days users may book ahead)
- ``GET /api/venue/{id}/day/{YYYY-MM-DD}`` - opening ``Hours`` (``StartTime``/``EndTime``
  in hours, ``Closed``), ``DisableReservationBeforeTime`` (lead time in hours) and
  ``Terrains[]`` each with ``Reservations[]`` (``StartTime``/``EndTime`` as decimal hours,
  half hours allowed).

Free time is derived as 30-minute units within opening hours minus reservations.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..models import Club, Court, Slot, Sport
from .base import DEFAULT_HEADERS, Provider, register

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Zagreb")
UNIT = timedelta(minutes=30)

SPORT_URL_NAMES = {
    "tennis": Sport.TENNIS,
    "tenis": Sport.TENNIS,
    "padel": Sport.PADEL,
    "squash": Sport.SQUASH,
}


def _hours_to_dt(day: date, hours: float) -> datetime:
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    if whole >= 24:
        return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=TZ) + timedelta(minutes=minutes)
    return datetime.combine(day, time(whole, minutes), tzinfo=TZ)


def venue_indoor(venue: dict[str, Any]) -> bool | None:
    text = f"{venue.get('Name', '')} {venue.get('Description', '')}".lower()
    if any(k in text for k in ("dvorana", "balon", "hala", "unutarnj", "indoor")):
        return True
    if any(k in text for k in ("vanjsk", "otvoren", "outdoor")):
        return False
    return None


class TivazerraProvider(Provider):
    """Generic Tivazerra adapter; subclass with ``key``, ``base_url`` and ``club``."""

    base_url: ClassVar[str]
    sport: ClassVar[Sport] = Sport.TENNIS
    max_concurrency: ClassVar[int] = 4
    # Optional per-venue overrides: venue id -> {"indoor": bool, "surface": str}
    venue_overrides: ClassVar[dict[int, dict[str, Any]]] = {}

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers={**DEFAULT_HEADERS, "Accept": "application/json"}, timeout=20
        )
        self._venues: list[dict[str, Any]] | None = None
        self._horizon_days: int | None = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _get_json(self, path: str) -> Any:
        async with self._sem:
            r = await self.client.get(path)
        r.raise_for_status()
        return r.json()

    async def _ensure_meta(self) -> None:
        async with self._lock:
            if self._venues is not None:
                return
            venues, settings = await asyncio.gather(
                self._get_json("/api/venues"), self._get_json("/api/reservations/settings")
            )
            self._venues = [
                v for v in venues
                if not v.get("IsArchived") and v.get("SportActive", 1)
                and SPORT_URL_NAMES.get(str(v.get("SportUrlName", "")).lower()) == self.sport
            ]
            limit = settings.get("FutureReservationLimit")
            self._horizon_days = int(limit) if limit else None
            log.info("%s: tennis venues %s, horizon %s days", self.key, [v["VenueId"] for v in self._venues], limit)

    # --------------------------------------------------------------- parsing

    def booking_url(self, venue: dict[str, Any], day: date) -> str:
        return f"{self.base_url}/Reservations/{venue.get('SportUrlName', 'Tennis')}/{day.isoformat()}"

    def court_for(self, venue: dict[str, Any], terrain: dict[str, Any], prefix_venue: bool) -> Court:
        override = self.venue_overrides.get(int(venue["VenueId"]), {})
        name = terrain["Name"]
        if prefix_venue:
            name = f"{venue['Name']} - {name}"
        return Court(
            club=self.club,
            id=f"{venue['VenueId']}:{terrain['TerrainId']}",
            name=name,
            sport=self.sport,
            indoor=override.get("indoor", venue_indoor(venue)),
            surface=override.get("surface"),
        )

    def parse_day(
        self,
        venue: dict[str, Any],
        day: date,
        payload: dict[str, Any],
        *,
        prefix_venue: bool = False,
        not_before: datetime | None = None,
    ) -> list[Slot]:
        hours = payload.get("Hours") or {}
        if hours.get("Closed") or hours.get("StartTime") is None or hours.get("EndTime") is None:
            return []
        open_from = _hours_to_dt(day, float(hours["StartTime"]))
        open_to = _hours_to_dt(day, float(hours["EndTime"]))
        if open_to <= open_from:
            return []
        url = self.booking_url(venue, day)
        slots: list[Slot] = []
        for terrain in payload.get("Terrains") or []:
            court = self.court_for(venue, terrain, prefix_venue)
            busy = [
                (_hours_to_dt(day, float(r["StartTime"])), _hours_to_dt(day, float(r["EndTime"])))
                for r in terrain.get("Reservations") or []
            ]
            t = open_from
            while t + UNIT <= open_to:
                end = t + UNIT
                if (not_before is None or t >= not_before) and not any(s < end and e > t for s, e in busy):
                    slots.append(Slot(court=court, start=t, end=end, booking_url=url))
                t = end
        return slots

    # ----------------------------------------------------------------- fetch

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        await self._ensure_meta()
        assert self._venues is not None
        today = datetime.now(TZ).date()
        if self._horizon_days:
            date_to = min(date_to, today + timedelta(days=self._horizon_days))
        if date_to < date_from or not self._venues:
            return []

        days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
        prefix = len(self._venues) > 1
        tasks = [
            (venue, day, self._get_json(f"/api/venue/{venue['VenueId']}/day/{day.isoformat()}"))
            for venue in self._venues
            for day in days
        ]
        payloads = await asyncio.gather(*(t[2] for t in tasks))
        now = datetime.now(TZ)
        slots: list[Slot] = []
        for (venue, day, _), payload in zip(tasks, payloads, strict=True):
            lead_hours = float(payload.get("DisableReservationBeforeTime") or 0)
            not_before = now + timedelta(hours=lead_hours) if day == today else None
            slots.extend(self.parse_day(venue, day, payload, prefix_venue=prefix, not_before=not_before))
        return slots


@register
class TkZagi(TivazerraProvider):
    key = "zagi"
    base_url = "https://termini.zagi-sport.hr"
    club = Club(
        key="zagi",
        name="TK Zagi",
        url="https://termini.zagi-sport.hr/Reservations/Tennis",
        address="Paviljon 6, Zagreb",
    )
