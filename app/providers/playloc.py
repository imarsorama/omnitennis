"""Adapter for clubs hosted on the Playloc booking platform (<slug>.playloc.app).

Playloc is a Laravel + Inertia.js app. The calendar page embeds its props as
JSON in a ``data-page`` attribute and also serves the same props as JSON when
requested with ``X-Inertia`` headers. Availability is public (no login).

Data model per day (``props.data[YYYY-MM-DD]``):
- ``open``: whether the club is open that day
- ``unavailableUntil``: "HH:MM" cut-off for today (past / too-late slots)
- ``fields``: courts with ``min``/``max`` opening hours, ``hoursTaken`` (list of
  occupied 30-minute unit starts) and ``info`` (indoor/outdoor, surface)

Each request returns 3 days starting at ``?date=``; the bookable horizon is
``minDate``..``maxDate`` (currently 7 days).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx

from ..models import Club, Court, Slot, Sport
from .base import DEFAULT_HEADERS, Provider, ProviderError, register

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Zagreb")
UNIT = timedelta(minutes=30)
_DATA_PAGE_RE = re.compile(r'data-page="([^"]+)"')

SPORT_SLUGS = {
    "tenis": Sport.TENNIS,
    "tennis": Sport.TENNIS,
    "padel": Sport.PADEL,
    "squash": Sport.SQUASH,
    "skvos": Sport.SQUASH,
}

SURFACES = {
    "zemlja": "clay",
    "clay": "clay",
    "hard": "hard",
    "beton": "hard",
    "trava": "grass",
    "umjetna trava": "artificial grass",
    "tepih": "carpet",
    "carpet": "carpet",
}


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _hour_to_time(h: float) -> time:
    return time(int(h), int(round((h - int(h)) * 60)))


def parse_info(info: list[str] | None) -> tuple[bool | None, str | None]:
    """Map Playloc's info strings to (indoor, surface)."""
    indoor: bool | None = None
    surface: str | None = None
    for raw in info or []:
        s = raw.strip().lower()
        if s.startswith("unutarnji") or "indoor" in s or "balon" in s or "dvorana" in s:
            indoor = True
        elif s.startswith("vanjski") or "outdoor" in s:
            indoor = False
        if s.startswith("podloga:"):
            value = s.split(":", 1)[1].strip()
            surface = SURFACES.get(value, value)
    return indoor, surface


class PlaylocProvider(Provider):
    """Generic Playloc adapter; subclass with ``key``, ``slug`` and ``club``."""

    slug: ClassVar[str]
    sport_slug: ClassVar[str] = "tenis"
    days_per_page: ClassVar[int] = 3

    def __init__(self) -> None:
        self.base_url = f"https://{self.slug}.playloc.app"
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True
        )
        self._version: str | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ HTTP

    def _params(self, day: date) -> dict[str, str]:
        return {"date": day.isoformat(), "sport": self.sport_slug}

    async def _fetch_html_page(self, day: date) -> dict[str, Any]:
        r = await self.client.get("/", params=self._params(day))
        r.raise_for_status()
        m = _DATA_PAGE_RE.search(r.text)
        if not m:
            raise ProviderError(f"{self.slug}: no Inertia data-page found in HTML")
        page = json.loads(html.unescape(m.group(1)))
        self._version = page.get("version")
        return page

    async def _fetch_page(self, day: date) -> dict[str, Any]:
        """Return the Inertia page object {component, props, version} for ``day``."""
        if self._version is None:
            return await self._fetch_html_page(day)
        headers = {
            "X-Inertia": "true",
            "X-Inertia-Version": self._version,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = await self.client.get("/", params=self._params(day), headers=headers)
        if r.status_code == 409 or "json" not in r.headers.get("content-type", ""):
            # Asset version changed (deploy) - refresh via the HTML page.
            return await self._fetch_html_page(day)
        r.raise_for_status()
        return r.json()

    # --------------------------------------------------------------- parsing

    def _courts_from_fields(self, fields: list[dict[str, Any]], sport: Sport) -> dict[int, Court]:
        courts: dict[int, Court] = {}
        for f in fields:
            indoor, surface = parse_info(f.get("info"))
            courts[int(f["id"])] = Court(
                club=self.club, id=str(f["id"]), name=f["name"], sport=sport, indoor=indoor, surface=surface
            )
        return courts

    def booking_url(self, day: date) -> str:
        return f"{self.base_url}/?date={day.isoformat()}&sport={self.sport_slug}"

    def parse_day(self, day: date, day_data: dict[str, Any], sport: Sport) -> list[Slot]:
        if not day_data.get("open", True):
            return []
        cutoff: time | None = None
        if day_data.get("unavailableUntil"):
            cutoff = _parse_hhmm(day_data["unavailableUntil"])

        courts = self._courts_from_fields(day_data.get("fields", []), sport)
        slots: list[Slot] = []
        url = self.booking_url(day)
        for f in day_data.get("fields", []):
            court = courts[int(f["id"])]
            if court.sport != Sport.TENNIS:
                continue
            if f.get("restrictions"):
                log.debug("%s %s %s has restrictions: %s", self.slug, day, f["name"], f["restrictions"])
            taken = {_parse_hhmm(h) for h in f.get("hoursTaken", [])}
            open_from = datetime.combine(day, _hour_to_time(float(f["min"])), tzinfo=TZ)
            open_to = datetime.combine(day, _hour_to_time(float(f["max"])), tzinfo=TZ)
            t = open_from
            while t + UNIT <= open_to:
                if t.time() not in taken and (cutoff is None or t.time() >= cutoff):
                    slots.append(Slot(court=court, start=t, end=t + UNIT, booking_url=url))
                t += UNIT
        return slots

    def parse_page(self, page: dict[str, Any]) -> tuple[list[Slot], date | None, date | None]:
        props = page["props"]
        sport_slug = (props.get("selectedSport") or {}).get("slug", self.sport_slug)
        sport = SPORT_SLUGS.get(sport_slug, Sport.OTHER)
        slots: list[Slot] = []
        for day_str, day_data in (props.get("data") or {}).items():
            slots.extend(self.parse_day(date.fromisoformat(day_str), day_data, sport))
        min_d = date.fromisoformat(props["minDate"]) if props.get("minDate") else None
        max_d = date.fromisoformat(props["maxDate"]) if props.get("maxDate") else None
        return slots, min_d, max_d

    # ----------------------------------------------------------------- fetch

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        async with self._lock:
            first = await self._fetch_page(date_from)
        slots, _min_d, max_d = self.parse_page(first)
        seen_days = set((first["props"].get("data") or {}).keys())

        end = min(date_to, max_d) if max_d else date_to
        # Request remaining pages concurrently, stepping by the page size.
        cursors: list[date] = []
        cur = date_from + timedelta(days=self.days_per_page)
        while cur <= end:
            cursors.append(cur)
            cur += timedelta(days=self.days_per_page)
        if cursors:
            pages = await asyncio.gather(*(self._fetch_page(c) for c in cursors))
            for page in pages:
                data = page["props"].get("data") or {}
                for day_str in list(data):
                    if day_str in seen_days:
                        del data[day_str]
                    else:
                        seen_days.add(day_str)
                page_slots, _, _ = self.parse_page(page)
                slots.extend(page_slots)

        return [s for s in slots if date_from <= s.start.date() <= date_to]


@register
class HatkMladost(PlaylocProvider):
    key = "mladost"
    slug = "hatk-mladost"
    club = Club(
        key="mladost",
        name="HATK Mladost",
        url="https://hatk-mladost.playloc.app/",
        address="Jarunska ul. 5, Zagreb",
    )
