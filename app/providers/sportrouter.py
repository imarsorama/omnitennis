"""Adapter for clubs on the Sport Router platform (mobile app ``com.sport.router``).

Example: TK Građanski (sports-center id 96). Sport Router has no public web
booking UI; the mobile app talks to ``https://app.sportrouter.com`` and every
``/api/...`` route needs a bearer token of a *user* account:

- ``POST /auth/login`` ``{"email", "password"}`` -> ``{"accessToken", "refreshToken"}``
- ``GET /auth/refresh?refreshToken=...`` -> ``{"accessToken"}`` (for Google-login accounts)
- ``POST /auth/register`` ``{"email", "password"}`` creates a plain account
  (see ``python -m app.providers.sportrouter register``).
- ``GET /api/sports-center/{id}`` -> courts, opening hours, ``timeZone``, ``timesOff``.
- ``POST /api/sports-center/search`` with ``{"sport", "from", "to", "duration",
  "sportsCenterId"}`` -> centers whose ``offers[]`` list, per start time, the
  *free* courts. ``from``/``to`` must be on the same day with zero minutes and
  ``duration`` is 60 or 120, so availability is fetched one day at a time.

Credentials are shared by all Sport Router clubs: ``OMNI_SPORTROUTER_USERNAME`` /
``OMNI_SPORTROUTER_PASSWORD`` (or ``OMNI_SPORTROUTER_REFRESH_TOKEN``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..config import get_settings
from ..models import Club, Court, Slot, Sport
from .base import Provider, ProviderError, TokenSession, register

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

API = "https://app.sportrouter.com"
SEARCH_PATH = "/api/sports-center/search"
SEARCH_DURATION = 60  # minutes; the API accepts 60 or 120 only
DEFAULT_TZ = ZoneInfo("Europe/Zagreb")
REFRESH_TOKEN_ENV = "OMNI_SPORTROUTER_REFRESH_TOKEN"

SPORTS = {
    "TENNIS": Sport.TENNIS,
    "PADEL": Sport.PADEL,
    "SQUASH": Sport.SQUASH,
}

SURFACES = {
    "CLAY": "clay",
    "HARD": "hard",
    "ACRYLIC": "hard",
    "CONCRETE": "hard",
    "GRASS": "grass",
    "ARTIFICIAL_GRASS": "artificial grass",
    "CARPET": "carpet",
}


def court_indoor(court_type: str | None) -> bool | None:
    t = (court_type or "").upper()
    if t in ("INDOOR", "HALL", "COVERED", "BALLOON"):
        return True
    if t == "OUTDOOR":
        return False
    return None


def court_sport(raw: dict[str, Any]) -> Sport:
    codes = [str(s).upper() for s in raw.get("sports") or []]
    if "TENNIS" in codes:
        return Sport.TENNIS
    for code in codes:
        if code in SPORTS:
            return SPORTS[code]
    return Sport.OTHER


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message") or response.text)
    except ValueError:
        return response.text[:200]


class SportRouterProvider(Provider):
    """Generic Sport Router adapter; subclass with ``key``, ``center_id`` and ``club``."""

    center_id: ClassVar[int]
    sport: ClassVar[Sport] = Sport.TENNIS
    credentials_key: ClassVar[str | None] = "sportrouter"
    max_days_ahead: ClassVar[int] = 14
    max_concurrency: ClassVar[int] = 4

    def __init__(self) -> None:
        self.session = TokenSession(
            self._obtain_token,
            base_url=API,
            headers={"Accept": "application/json", "Accept-Language": "hr"},
        )
        self._tz: ZoneInfo | None = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.max_concurrency)

    async def aclose(self) -> None:
        await self.session.aclose()

    @classmethod
    def credentials_present(cls, settings: Settings) -> bool:
        return super().credentials_present(settings) or bool(os.getenv(REFRESH_TOKEN_ENV))

    # ------------------------------------------------------------------ auth

    async def _obtain_token(self, client: httpx.AsyncClient) -> str:
        creds = get_settings().credentials(self.credentials_key or self.key)
        refresh_token = os.getenv(REFRESH_TOKEN_ENV)
        if creds.present:
            r = await client.post("/auth/login", json={"email": creds.username, "password": creds.password})
        elif refresh_token:
            r = await client.get("/auth/refresh", params={"refreshToken": refresh_token})
        else:
            raise ProviderError(
                "Sport Router needs OMNI_SPORTROUTER_USERNAME/OMNI_SPORTROUTER_PASSWORD "
                f"(or {REFRESH_TOKEN_ENV}); see README"
            )
        if r.status_code >= 400:
            raise ProviderError(f"Sport Router login failed (HTTP {r.status_code}): {_error_message(r)}")
        token = r.json().get("accessToken")
        if not token:
            raise ProviderError("Sport Router login returned no accessToken")
        return str(token)

    # ------------------------------------------------------------------ HTTP

    async def _ensure_meta(self) -> ZoneInfo:
        async with self._lock:
            if self._tz is not None:
                return self._tz
            r = await self.session.get(f"/api/sports-center/{self.center_id}")
            if r.status_code >= 400:
                raise ProviderError(
                    f"Sport Router center {self.center_id} unavailable (HTTP {r.status_code}): {_error_message(r)}"
                )
            details = r.json()
            self._tz = DEFAULT_TZ
            try:
                if details.get("timeZone"):
                    self._tz = ZoneInfo(details["timeZone"])
            except ZoneInfoNotFoundError:
                log.warning("%s: unknown timeZone %r, using %s", self.key, details.get("timeZone"), DEFAULT_TZ)
            log.info("%s: %s, %d courts, tz %s", self.key, details.get("name"), len(details.get("courts") or []), self._tz)
            return self._tz

    @staticmethod
    def search_body(center_id: int, sport_code: str, day: date, not_before: datetime | None = None) -> dict[str, Any]:
        start = f"{day.isoformat()}T00:00:00"
        if not_before is not None and not_before.date() == day:
            start = not_before.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "sport": sport_code,
            "from": start,
            "to": f"{(day + timedelta(days=1)).isoformat()}T00:00:00",
            "duration": SEARCH_DURATION,
            "sportsCenterId": center_id,
        }

    async def _search(self, day: date, not_before: datetime | None) -> list[dict[str, Any]]:
        body = self.search_body(self.center_id, self.sport.name, day, not_before)
        async with self._sem:
            r = await self.session.post(SEARCH_PATH, json=body)
        if r.status_code >= 400:
            raise ProviderError(f"Sport Router search failed for {day} (HTTP {r.status_code}): {_error_message(r)}")
        payload = r.json()
        return payload if isinstance(payload, list) else []

    # --------------------------------------------------------------- parsing

    @property
    def booking_url(self) -> str:
        return self.club.url

    def court_for(self, raw: dict[str, Any]) -> Court:
        return Court(
            club=self.club,
            id=str(raw["id"]),
            name=str(raw.get("name") or f"Court {raw.get('number', raw['id'])}"),
            sport=court_sport(raw),
            indoor=court_indoor(raw.get("courtType")),
            surface=SURFACES.get(str(raw.get("surfaceType") or "").upper()),
        )

    def parse_search(self, payload: list[dict[str, Any]], tz: ZoneInfo | None = None) -> list[Slot]:
        tz = tz or self._tz or DEFAULT_TZ
        slots: list[Slot] = []
        for center in payload:
            if int(center.get("id", -1)) != self.center_id:
                continue
            for offer in center.get("offers") or []:
                if SPORTS.get(str(offer.get("sport") or "").upper()) != self.sport:
                    continue
                try:
                    start = datetime.fromisoformat(offer["from"]).replace(tzinfo=tz)
                    end = datetime.fromisoformat(offer["to"]).replace(tzinfo=tz)
                except (KeyError, ValueError):
                    log.warning("%s: skipping malformed offer %r", self.key, offer)
                    continue
                if end <= start:
                    continue
                for raw in offer.get("courts") or []:
                    court = self.court_for(raw)
                    if court.sport != self.sport:
                        continue
                    slots.append(Slot(court=court, start=start, end=end, booking_url=self.booking_url))
        return slots

    # ----------------------------------------------------------------- fetch

    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        tz = await self._ensure_meta()
        now = datetime.now(tz)
        today = now.date()
        date_from = max(date_from, today)
        date_to = min(date_to, today + timedelta(days=self.max_days_ahead))
        if date_to < date_from:
            return []
        days = [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]
        payloads = await asyncio.gather(*(self._search(day, now) for day in days))
        slots: list[Slot] = []
        for payload in payloads:
            slots.extend(self.parse_search(payload, tz))
        return slots


@register
class TkGradanski(SportRouterProvider):
    key = "gradanski"
    center_id = 96
    club = Club(
        key="gradanski",
        name="TK Građanski",
        url="https://sportrouter.com/",
        address="Ul. Vjekoslava Klaića 3, Zagreb",
    )


# --------------------------------------------------------------------------- #
# CLI helper: create a plain (email + password) Sport Router account
# --------------------------------------------------------------------------- #


async def register_account(email: str, password: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=API, timeout=20, headers={"Accept-Language": "hr"}) as client:
        r = await client.post("/auth/register", json={"email": email, "password": password})
    if r.status_code >= 400:
        raise ProviderError(f"Registration failed (HTTP {r.status_code}): {_error_message(r)}")
    return r.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.providers.sportrouter")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="create a Sport Router account usable by this app")
    reg.add_argument("email")
    reg.add_argument("password")
    args = parser.parse_args(argv)

    if args.command == "register":
        try:
            asyncio.run(register_account(args.email, args.password))
        except ProviderError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("Account created. Add to .env:")
        print(f"OMNI_SPORTROUTER_USERNAME={args.email}")
        print("OMNI_SPORTROUTER_PASSWORD=<the password you just used>")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
