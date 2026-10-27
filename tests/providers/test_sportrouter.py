import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.config import get_settings
from app.criteria import merge_consecutive
from app.models import Sport
from app.providers.base import ProviderError
from app.providers.sportrouter import (
    API,
    SEARCH_PATH,
    SportRouterProvider,
    TkGradanski,
    court_indoor,
    court_sport,
    main,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sportrouter_gradanski.json"
DAY = date(2026, 10, 28)
TZ = ZoneInfo("Europe/Sarajevo")


@pytest.fixture
def fx() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("OMNI_SPORTROUTER_USERNAME", "user@example.com")
    monkeypatch.setenv("OMNI_SPORTROUTER_PASSWORD", "secret")
    monkeypatch.delenv("OMNI_SPORTROUTER_REFRESH_TOKEN", raising=False)


@pytest.fixture
async def provider():
    p = TkGradanski()
    yield p
    await p.aclose()


def test_court_helpers():
    assert court_indoor("OUTDOOR") is False
    assert court_indoor("INDOOR") is True
    assert court_indoor(None) is None
    assert court_sport({"sports": ["BADMINTON", "TENNIS"]}) == Sport.TENNIS
    assert court_sport({"sports": ["PADEL"]}) == Sport.PADEL
    assert court_sport({"sports": ["FOOTBALL"]}) == Sport.OTHER


def test_search_body_same_day_zero_minutes():
    body = SportRouterProvider.search_body(96, "TENNIS", DAY)
    assert body == {
        "sport": "TENNIS",
        "from": "2026-10-28T00:00:00",
        "to": "2026-10-29T00:00:00",
        "duration": 60,
        "sportsCenterId": 96,
    }
    now = datetime(2026, 10, 28, 17, 25, tzinfo=TZ)
    assert SportRouterProvider.search_body(96, "TENNIS", DAY, now)["from"] == "2026-10-28T17:00:00"
    # not_before on another day does not affect the request
    assert SportRouterProvider.search_body(96, "TENNIS", DAY + timedelta(days=1), now)["from"] == "2026-10-29T00:00:00"


def test_parse_offers_to_per_court_slots(provider, fx):
    slots = provider.parse_search(fx["search"], TZ)
    starts: dict[str, list[time]] = {}
    for s in slots:
        starts.setdefault(s.court.id, []).append(s.start.time())
    # 12 hourly offers; court 396 is taken at 19:00 -> 11 slots, others 12
    assert len(slots) == 12 * 3 - 1
    assert time(19) not in starts["396"] and time(19) in starts["397"] and time(19) in starts["398"]
    # 16:00 and 17:00 are fully booked, 08:00 is the first offer
    assert time(16) not in starts["398"] and time(17) not in starts["398"]
    assert min(starts["398"]) == time(8) and max(starts["398"]) == time(21)
    assert all(s.end - s.start == timedelta(minutes=60) for s in slots)
    assert all(s.start.tzinfo is not None and s.start.date() == DAY for s in slots)


def test_court_metadata_and_url(provider, fx):
    slots = provider.parse_search(fx["search"], TZ)
    court = next(s.court for s in slots if s.court.id == "397")
    assert court.name == "Teniski teren 2 - single only"
    assert court.sport == Sport.TENNIS and court.indoor is False and court.surface == "clay"
    assert court.club.key == "gradanski"
    assert slots[0].booking_url == "https://sportrouter.com/"


def test_merge_blocks(provider, fx):
    slots = provider.parse_search(fx["search"], TZ)
    blocks = merge_consecutive([s for s in slots if s.court.id == "396"])
    assert [(b.start.time(), b.end.time()) for b in blocks] == [
        (time(8), time(16)),
        (time(18), time(19)),
        (time(20), time(22)),
    ]


def test_parse_ignores_other_centers_sports_and_courts(provider, fx):
    center = fx["search"][0]
    other = dict(center, id=80)
    padel_offer = dict(center["offers"][0], sport="PADEL")
    mixed_offer = dict(
        center["offers"][1],
        courts=center["offers"][1]["courts"] + [{"id": 999, "name": "Padel 1", "sports": ["PADEL"], "courtType": "INDOOR"}],
    )
    payload = [other, dict(center, offers=[padel_offer, mixed_offer])]
    slots = provider.parse_search(payload, TZ)
    assert {s.court.id for s in slots} == {"396", "397", "398"}
    assert {s.start.time() for s in slots} == {time(9)}


def test_parse_empty_when_center_absent(provider):
    assert provider.parse_search([], TZ) == []
    assert provider.parse_search([{"id": 80, "offers": [{"from": "x"}]}], TZ) == []


async def test_fetch_logs_in_once_and_searches_per_day(fx, creds):
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/auth/login":
            assert body == {"email": "user@example.com", "password": "secret"}
            return httpx.Response(200, json={"accessToken": "tok", "refreshToken": "ref"})
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path == "/api/sports-center/96":
            return httpx.Response(200, json=fx["details"])
        assert request.url.path == SEARCH_PATH
        assert body["sportsCenterId"] == 96 and body["duration"] == 60 and body["sport"] == "TENNIS"
        assert body["from"][:10] == (date.fromisoformat(body["to"][:10]) - timedelta(days=1)).isoformat()
        assert body["from"].endswith(":00:00") and body["to"].endswith("T00:00:00")
        day = body["from"][:10]
        if day != today.isoformat():
            return httpx.Response(200, json=[])
        # Serve the recorded day re-dated to the requested day.
        center = dict(fx["search"][0])
        center["offers"] = [
            dict(o, **{"from": day + o["from"][10:], "to": day + o["to"][10:]}) for o in center["offers"]
        ]
        return httpx.Response(200, json=[center])

    today = datetime.now(TZ).date()
    p = TkGradanski()
    p.session.client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))
    try:
        slots = await p.fetch(today - timedelta(days=3), today + timedelta(days=30))
    finally:
        await p.aclose()

    assert [c for c in calls if c[1] == "/auth/login"] == [("POST", "/auth/login", {"email": "user@example.com", "password": "secret"})]
    searches = [c for c in calls if c[1] == SEARCH_PATH]
    assert len(searches) == p.max_days_ahead + 1  # past days dropped, future clamped
    assert searches[0][2]["from"][:10] == today.isoformat()
    assert len(slots) == 35 and {s.start.date() for s in slots} == {today}


async def test_fetch_relogins_on_401(fx, creds):
    logins = 0
    searches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logins, searches
        if request.url.path == "/auth/login":
            logins += 1
            return httpx.Response(200, json={"accessToken": f"tok{logins}", "refreshToken": "r"})
        if request.url.path == "/api/sports-center/96":
            return httpx.Response(200, json=fx["details"])
        searches += 1
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"message": "Authentication failed"})
        return httpx.Response(200, json=[])

    p = TkGradanski()
    p.max_days_ahead = 0  # type: ignore[misc]
    p.session.client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))
    today = datetime.now(TZ).date()
    try:
        assert await p.fetch(today, today) == []
    finally:
        await p.aclose()
    assert logins == 2 and searches == 2


async def test_fetch_without_credentials_raises(monkeypatch):
    for var in ("OMNI_SPORTROUTER_USERNAME", "OMNI_SPORTROUTER_PASSWORD", "OMNI_SPORTROUTER_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    p = TkGradanski()
    p.session.client = httpx.AsyncClient(
        base_url=API, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    try:
        with pytest.raises(ProviderError, match="OMNI_SPORTROUTER_USERNAME"):
            await p.fetch(DAY, DAY)
    finally:
        await p.aclose()


async def test_refresh_token_login(monkeypatch, fx):
    monkeypatch.delenv("OMNI_SPORTROUTER_USERNAME", raising=False)
    monkeypatch.delenv("OMNI_SPORTROUTER_PASSWORD", raising=False)
    monkeypatch.setenv("OMNI_SPORTROUTER_REFRESH_TOKEN", "refresh-me")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/auth/refresh":
            assert request.url.params["refreshToken"] == "refresh-me"
            return httpx.Response(200, json={"accessToken": "fresh"})
        assert request.headers["Authorization"] == "Bearer fresh"
        if request.url.path == "/api/sports-center/96":
            return httpx.Response(200, json=fx["details"])
        return httpx.Response(200, json=[])

    p = TkGradanski()
    p.max_days_ahead = 0  # type: ignore[misc]
    p.session.client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))
    today = datetime.now(TZ).date()
    try:
        assert await p.fetch(today, today) == []
    finally:
        await p.aclose()
    assert any("/auth/refresh" in u for u in seen)
    assert TkGradanski.credentials_present(get_settings())


async def test_login_error_surfaces_message(creds):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authentication failed"})

    p = TkGradanski()
    p.session.client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError, match="HTTP 401.*Authentication failed"):
            await p.fetch(DAY, DAY)
    finally:
        await p.aclose()


def test_register_cli(monkeypatch, capsys):
    async def fake_register(email: str, password: str) -> dict:
        assert (email, password) == ("me@example.com", "pw")
        return {"accessToken": "a", "refreshToken": "b"}

    monkeypatch.setattr("app.providers.sportrouter.register_account", fake_register)
    assert main(["register", "me@example.com", "pw"]) == 0
    out = capsys.readouterr().out
    assert "OMNI_SPORTROUTER_USERNAME=me@example.com" in out and "pw" not in out.replace("OMNI_SPORTROUTER_PASSWORD", "")
