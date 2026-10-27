"""Adapter for Sportski centar Prince (https://prince.hr/app/book?sport=tennis).

The booking page is a TanStack/React SPA backed by a Supabase edge function.
Viewing availability needs no login (name/e-mail are only asked when booking):

- ``GET {api}/availability`` ->
  - ``allocations[]``: busy intervals ``{resource_id, start_date, end_date,
    start_min, end_min, units, blocked?}``; minutes from midnight, a span of
    ``0..1440`` means the whole day, multi-day entries repeat the window daily.
  - ``config.pricing_snapshot.hours[]``: seasonal opening hours
    ``{from_date, to_date, weekdays, open_min, close_min, resource_type}``
    (``weekdays`` use JS numbering, Sunday = 0; ``resource_type`` null = all).
  - ``config.pricing_snapshot.booking_rules``: ``lead_time_min``,
    ``max_days_ahead`` / ``web_max_days_ahead``, ``weekend_enabled``.
  - ``config.pricing_snapshot.spaces``: ``{"T1": {in_offer, slot_minutes,
    allowed_durations}, ...}``; tennis courts are ``T<n>`` <-> resource ``r-t<n>``.
  - ``config.pricing_snapshot.windows[]``: optional league/tournament blocks
    ``{active, proposed, resource_type, resource_ids, from_date, to_date,
    weekdays, start_min, end_min}``.

Free time = 30-minute units inside opening hours minus allocations and windows.
In the winter period (``booking_rules.winter_from_md``..``winter_to_md``, default
1 Oct - 16 Apr) the clay courts are covered by an air dome, so ``indoor`` is
True for those days and False otherwise.
The deep link ``/app/book?sport=tennis&court=T1&day=YYYY-MM-DD&start=HH:MM&dur=60``
pre-fills the booking form.
"""

from __future__ import annotations

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
UNIT_MINUTES = 30
DAY_MINUTES = 1440
DEFAULT_OPEN_MIN, DEFAULT_CLOSE_MIN = 420, 1380  # site fallback when no season matches
DEFAULT_MAX_DAYS_AHEAD = 60
TENNIS_CODE = re.compile(r"^T(\d+)$")
DEFAULT_TENNIS_CODES = ["T1", "T2", "T3", "T4", "T5"]
DEFAULT_WINTER_FROM_MD, DEFAULT_WINTER_TO_MD = "10-01", "04-16"
MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

Interval = tuple[int, int]


def is_winter(day: date, rules: dict[str, Any] | None = None) -> bool:
    """Mirror the site's season switch (air dome over the courts)."""
    rules = rules or {}
    start = rules.get("winter_from_md") if MONTH_DAY.match(str(rules.get("winter_from_md") or "")) else DEFAULT_WINTER_FROM_MD
    end = rules.get("winter_to_md") if MONTH_DAY.match(str(rules.get("winter_to_md") or "")) else DEFAULT_WINTER_TO_MD
    md = day.strftime("%m-%d")
    return start <= md <= end if start <= end else md >= start or md <= end


def resource_id(code: str) -> str:
    """``T3`` -> ``r-t3`` (the site's resource ids)."""
    return "r-" + code.lower()


def js_weekday(day: date) -> int:
    """JavaScript ``getDay()`` numbering: Sunday = 0."""
    return (day.weekday() + 1) % 7


def minutes_to_dt(day: date, minutes: int) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=TZ) + timedelta(minutes=minutes)


def opening_minutes(day: date, hours: list[dict[str, Any]], sport: str = "tennis") -> Interval | None:
    """Mirror the site's season lookup; ``None`` when closed for the day."""
    iso = day.isoformat()
    dow = js_weekday(day)
    matches = [
        h for h in hours
        if h.get("from_date", "") <= iso <= h.get("to_date", "")
        and (h.get("resource_type") in (None, sport))
        and (not h.get("weekdays") or dow in h["weekdays"])
    ]
    if not matches:
        return DEFAULT_OPEN_MIN, DEFAULT_CLOSE_MIN
    matches.sort(key=lambda h: (h.get("resource_type") is None, h.get("sort", 0)))
    chosen = matches[0]
    try:
        open_min, close_min = int(chosen["open_min"]), int(chosen["close_min"])
    except (KeyError, TypeError, ValueError):
        return None
    return (open_min, close_min) if close_min > open_min else None


def allocation_busy(allocations: list[dict[str, Any]], day: date) -> dict[str, list[Interval]]:
    iso = day.isoformat()
    busy: dict[str, list[Interval]] = {}
    for a in allocations:
        if not (a.get("start_date", "") <= iso <= a.get("end_date", a.get("start_date", ""))):
            continue
        try:
            start, end = int(a["start_min"]), int(a["end_min"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            busy.setdefault(str(a.get("resource_id")), []).append((start, end))
    return busy


def window_busy(windows: list[dict[str, Any]], day: date, tennis_ids: list[str]) -> dict[str, list[Interval]]:
    iso = day.isoformat()
    dow = js_weekday(day)
    busy: dict[str, list[Interval]] = {}
    for w in windows:
        if not w.get("active", True) or w.get("proposed"):
            continue
        if w.get("resource_type") not in (None, "tennis"):
            continue
        if iso < w.get("from_date", "") or iso > w.get("to_date", "9999-12-31"):
            continue
        if w.get("weekdays") and dow not in w["weekdays"]:
            continue
        start = int(w.get("start_min") or 0)
        end = int(w.get("end_min") or DAY_MINUTES)
        targets = [r for r in (w.get("resource_ids") or []) if r in tennis_ids] if w.get("resource_ids") else tennis_ids
        for rid in targets:
            busy.setdefault(rid, []).append((start, end))
    return busy


class PrinceProvider(Provider):
    """Adapter for the Prince booking SPA; subclass with ``key``, ``api_url`` and ``club``."""

    api_url: ClassVar[str] = "https://yutkdobleevjdgbnwckm.supabase.co/functions/v1/prince-api"
    site_url: ClassVar[str] = "https://prince.hr"
    sport: ClassVar[Sport] = Sport.TENNIS
    court_names: ClassVar[dict[str, str]] = {f"T{i}": f"Teren {i}" for i in range(1, 10)}

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            headers={**DEFAULT_HEADERS, "Accept": "application/json", "Origin": self.site_url, "Referer": f"{self.site_url}/app/book"},
            timeout=20,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def fetch_availability(self) -> dict[str, Any]:
        r = await self.client.get(f"{self.api_url}/availability")
        if r.status_code >= 400:
            raise ProviderError(f"{self.key}: availability HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        if not isinstance(payload, dict) or "allocations" not in payload:
            raise ProviderError(f"{self.key}: unexpected availability payload")
        return payload

    # --------------------------------------------------------------- parsing

    @staticmethod
    def snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        return ((payload.get("config") or {}).get("pricing_snapshot")) or {}

    def tennis_codes(self, payload: dict[str, Any]) -> list[str]:
        spaces = self.snapshot(payload).get("spaces") or {}
        codes = [
            code for code, space in spaces.items()
            if TENNIS_CODE.match(code) and (space or {}).get("in_offer", True)
        ]
        if not codes and not spaces:
            codes = list(DEFAULT_TENNIS_CODES)
        return sorted(codes, key=lambda c: int(TENNIS_CODE.match(c).group(1)))  # type: ignore[union-attr]

    def court_for(self, code: str, day: date, rules: dict[str, Any] | None = None) -> Court:
        return Court(
            club=self.club,
            id=code,
            name=self.court_names.get(code, f"Teren {code[1:]}"),
            sport=self.sport,
            indoor=is_winter(day, rules),
            surface="clay",
        )

    def booking_url(self, code: str, day: date, start_min: int) -> str:
        return (
            f"{self.site_url}/app/book?sport=tennis&court={code}"
            f"&day={day.isoformat()}&start={start_min // 60:02d}:{start_min % 60:02d}&dur=60"
        )

    def parse_day(self, payload: dict[str, Any], day: date, *, not_before: datetime | None = None) -> list[Slot]:
        snap = self.snapshot(payload)
        rules = snap.get("booking_rules") or {}
        if day.weekday() >= 5 and rules.get("weekend_enabled") is False:
            return []
        opening = opening_minutes(day, snap.get("hours") or [])
        if opening is None:
            return []
        open_min, close_min = opening
        codes = self.tennis_codes(payload)
        ids = [resource_id(c) for c in codes]
        busy = allocation_busy(payload.get("allocations") or [], day)
        for rid, intervals in window_busy(snap.get("windows") or [], day, ids).items():
            busy.setdefault(rid, []).extend(intervals)

        slots: list[Slot] = []
        for code in codes:
            court = self.court_for(code, day, rules)
            taken = busy.get(resource_id(code), [])
            t = open_min
            while t + UNIT_MINUTES <= close_min:
                end = t + UNIT_MINUTES
                start_dt = minutes_to_dt(day, t)
                if (not_before is None or start_dt >= not_before) and not any(s < end and e > t for s, e in taken):
                    slots.append(
                        Slot(court=court, start=start_dt, end=minutes_to_dt(day, end), booking_url=self.booking_url(code, day, t))
                    )
                t = end
        return slots

    # ----------------------------------------------------------------- fetch

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        payload = await self.fetch_availability()
        rules = self.snapshot(payload).get("booking_rules") or {}
        now = datetime.now(TZ)
        today = now.date()
        horizon = rules.get("web_max_days_ahead") or rules.get("max_days_ahead") or DEFAULT_MAX_DAYS_AHEAD
        date_from = max(date_from, today)
        date_to = min(date_to, today + timedelta(days=int(horizon)))
        if date_to < date_from:
            return []
        lead = timedelta(minutes=int(rules.get("lead_time_min") or 0))
        slots: list[Slot] = []
        for i in range((date_to - date_from).days + 1):
            day = date_from + timedelta(days=i)
            not_before = now + lead if day == today else None
            slots.extend(self.parse_day(payload, day, not_before=not_before))
        return slots


@register
class TkPrince(PrinceProvider):
    key = "prince"
    club = Club(
        key="prince",
        name="TK Prince",
        url="https://prince.hr/app/book?sport=tennis",
        address="Sisačka cesta 17c, Zagreb",
    )
