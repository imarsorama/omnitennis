"""Adapter for clubs hosted on Sporty.plus / Tenis.plus (<subdomain>.sporty.plus).

The club site is a Nuxt SPA; the data comes from the public JSON API at
``https://api.tenis.plus/v2``. Availability needs no login (``unauth=true``).

``GET /v2/court?filter[is_active]=1&filter[club_id]=<id>&date=YYYY-MM-DD&parsed2=true``
returns one page of courts, each with ``parsed_reservations`` keyed by ``HHMM``:
free units have ``id == 0`` (``hidden`` means not bookable); booked units carry
the reservation id and ``start_time``/``end_time``. Court metadata: ``type``
(``open`` = outdoor, ``closed`` = indoor), ``surface.badge``, ``sports[].slug``
and ``reservation_duration`` (minutes).

The club id is resolved once via ``GET /v2/club/this`` using the ``Origin``
header of the club's subdomain.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..models import Club, Court, Slot, Sport
from .base import DEFAULT_HEADERS, Provider, ProviderError, register

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Zagreb")
API = "https://api.tenis.plus/v2"

SPORT_SLUGS = {
    "tennis": Sport.TENNIS,
    "tenis": Sport.TENNIS,
    "padel": Sport.PADEL,
    "squash": Sport.SQUASH,
}

SURFACES = {
    "acrylic": "hard",
    "hard": "hard",
    "clay": "clay",
    "grass": "grass",
    "carpet": "carpet",
}


def _hhmm_to_time(value: int | str) -> time:
    v = int(value)
    return time(v // 100, v % 100)


def court_sport(raw: dict[str, Any]) -> Sport:
    slugs = [s.get("slug", "").lower() for s in raw.get("sports") or []]
    if not slugs:
        return Sport.TENNIS  # legacy clubs without sport tagging are tennis clubs
    sports = {SPORT_SLUGS.get(s, Sport.OTHER) for s in slugs}
    if Sport.TENNIS in sports:
        return Sport.TENNIS
    return next(iter(sports))


def court_indoor(raw: dict[str, Any]) -> bool | None:
    t = (raw.get("type") or "").lower()
    if t == "open":
        return False
    if t in ("closed", "hall", "indoor"):
        return True
    return None


def court_surface(raw: dict[str, Any]) -> str | None:
    surface = raw.get("surface") or {}
    badge = (surface.get("badge") or "").lower()
    if badge in ("", "other"):
        return None
    return SURFACES.get(badge, badge)


class SportyPlusProvider(Provider):
    """Generic Sporty.plus adapter; subclass with ``key``, ``subdomain`` and ``club``."""

    subdomain: ClassVar[str]
    club_id: ClassVar[int | None] = None
    page_limit: ClassVar[int] = 50
    max_concurrency: ClassVar[int] = 4

    def __init__(self) -> None:
        self.site_url = f"https://{self.subdomain}.sporty.plus"
        self.client = httpx.AsyncClient(
            base_url=API,
            headers={**DEFAULT_HEADERS, "Origin": self.site_url, "Referer": f"{self.site_url}/app/courts/"},
            timeout=20,
        )
        self._club_id: int | None = self.club_id
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _resolve_club_id(self) -> int:
        async with self._lock:
            if self._club_id is not None:
                return self._club_id
            r = await self.client.get("/club/this")
            r.raise_for_status()
            data = r.json()
            if data.get("subdomain") != self.subdomain:
                raise ProviderError(
                    f"{self.subdomain}: club resolution returned '{data.get('subdomain')}' (id {data.get('id')})"
                )
            self._club_id = int(data["id"])
            return self._club_id

    async def _fetch_day(self, day: date) -> list[dict[str, Any]]:
        club_id = await self._resolve_club_id()
        courts: list[dict[str, Any]] = []
        page = 1
        async with self._sem:
            while True:
                r = await self.client.get(
                    "/court",
                    params={
                        "filter[is_active]": 1,
                        "filter[club_id]": club_id,
                        "page": page,
                        "limit": self.page_limit,
                        "date": day.isoformat(),
                        "unauth": "true",
                        "parsed2": "true",
                    },
                )
                r.raise_for_status()
                body = r.json()
                courts.extend(body.get("data") or [])
                last_page = int((body.get("meta") or {}).get("last_page") or 1)
                if page >= last_page or not (body.get("links") or {}).get("next"):
                    break
                page += 1
        return courts

    # --------------------------------------------------------------- parsing

    def booking_url(self, day: date) -> str:
        # The SPA does not honour a date query parameter; land on the courts page.
        return f"{self.site_url}/app/courts"

    def build_courts(self, raw_courts: list[dict[str, Any]]) -> dict[int, Court]:
        """Map API courts to Court models, de-duplicating identical names."""
        names = Counter(c["name"] for c in raw_courts)
        seen: Counter[str] = Counter()
        courts: dict[int, Court] = {}
        for raw in raw_courts:
            name = raw["name"]
            if names[name] > 1:
                seen[name] += 1
                name = f"{name} #{seen[name]}"
            courts[int(raw["id"])] = Court(
                club=self.club,
                id=str(raw["id"]),
                name=name,
                sport=court_sport(raw),
                indoor=court_indoor(raw),
                surface=court_surface(raw),
            )
        return courts

    def parse_day(self, day: date, raw_courts: list[dict[str, Any]]) -> list[Slot]:
        courts = self.build_courts(raw_courts)
        url = self.booking_url(day)
        slots: list[Slot] = []
        for raw in raw_courts:
            court = courts[int(raw["id"])]
            if court.sport != Sport.TENNIS or not raw.get("is_active", True):
                continue
            default_minutes = int(raw.get("reservation_duration") or 60)
            entries = raw.get("parsed_reservations") or {}
            keys = sorted(entries, key=int)
            for i, key in enumerate(keys):
                entry = entries[key]
                if entry.get("id") or entry.get("hidden"):
                    continue
                start = datetime.combine(day, _hhmm_to_time(entry.get("time", key)), tzinfo=TZ)
                minutes = default_minutes
                if i + 1 < len(keys):
                    nxt = datetime.combine(day, _hhmm_to_time(keys[i + 1]), tzinfo=TZ)
                    minutes = min(minutes, int((nxt - start).total_seconds() // 60))
                if minutes <= 0:
                    continue
                slots.append(Slot(court=court, start=start, end=start + timedelta(minutes=minutes), booking_url=url))
        return slots

    # ----------------------------------------------------------------- fetch

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
        results = await asyncio.gather(*(self._fetch_day(d) for d in days))
        slots: list[Slot] = []
        for day, raw_courts in zip(days, results, strict=True):
            slots.extend(self.parse_day(day, raw_courts))
        return slots


@register
class TkMaksimir(SportyPlusProvider):
    key = "maksimir"
    subdomain = "maksimir"
    club_id = 9
    club = Club(
        key="maksimir",
        name="TC Maksimir",
        url="https://maksimir.sporty.plus/app/courts",
        address="Ulica V. Ravnice 10, Zagreb",
    )
