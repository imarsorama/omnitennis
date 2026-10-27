from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _next_weekend() -> tuple[date, date]:
    d = date.today() + timedelta(days=1)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d, d + timedelta(days=1)


SATURDAY, SUNDAY = _next_weekend()  # always in the future so nothing is dropped as "past"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "OmniTennis" in r.text
    assert 'name="min_minutes"' in r.text
    assert "weekday_windows" not in r.text


def test_api_availability_defaults(client):
    r = client.get("/api/availability", params={"date_from": SATURDAY, "date_to": SUNDAY})
    assert r.status_code == 200
    body = r.json()
    assert {p["status"] for p in body["providers"]} <= {"ok", "cached"}
    assert body["blocks"], "mock providers should yield some weekend blocks"
    for b in body["blocks"]:
        assert b["minutes"] >= 120
        assert b["booking_url"].startswith("https://")
        assert "Padel" not in b["court_name"] and "Squash" not in b["court_name"]


def test_api_availability_invalid_range(client):
    r = client.get("/api/availability", params={"date_from": SUNDAY, "date_to": SATURDAY})
    assert r.status_code == 422


def test_partial_results_renders_timeline(client):
    r = client.get("/partials/results", params={"date_from": SATURDAY, "date_to": SUNDAY, "min_minutes": 60})
    assert r.status_code == 200
    assert 'class="timeline"' in r.text and 'class="blk"' in r.text and 'class="tl-hour"' in r.text
    assert "badge-ok" in r.text or "badge-cached" in r.text


def test_timeline_layout_positions():
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from app.main import timeline_layout
    from app.models import Club, Court, FreeBlock

    tz = ZoneInfo("Europe/Zagreb")
    club = Club(key="c", name="C", url="https://c.test")
    court = Court(club=club, id="1", name="Court 1")
    day = SATURDAY
    blocks = [
        FreeBlock(court=court, start=datetime.combine(day, time(8), tzinfo=tz), end=datetime.combine(day, time(10), tzinfo=tz), booking_url="u"),
        FreeBlock(court=court, start=datetime.combine(day, time(20), tzinfo=tz), end=datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz), booking_url="u"),
    ]
    days = [{"day": day, "clubs": [{"club": club, "courts": [{"court": court, "blocks": blocks}], "count": 2}], "count": 2}]
    axis = timeline_layout(days)

    assert axis["start_label"] == "08:00" and axis["end_label"] == "24:00"
    assert [h["label"] for h in axis["hours"]][:3] == ["08", "09", "10"] and axis["hours"][-1]["label"] == "24"
    placed = days[0]["clubs"][0]["courts"][0]["placed"]
    assert placed[0]["left"] == 0 and placed[0]["width"] == pytest.approx(2 / 16 * 100)
    assert placed[1]["left"] == pytest.approx(12 / 16 * 100) and placed[1]["width"] == pytest.approx(4 / 16 * 100)


def test_club_filter_via_api(client):
    r = client.get("/api/availability", params={"date_from": SATURDAY, "date_to": SUNDAY, "clubs": "mock_jarun"})
    body = r.json()
    assert [p["provider"] for p in body["providers"]] == ["mock_jarun"]
    assert all(b["club"] == "mock_jarun" for b in body["blocks"])


def test_providers_endpoint(client):
    r = client.get("/api/providers")
    rows = {row["key"]: row for row in r.json()}
    assert rows["mock_salata"]["enabled"] is True
    assert rows["mock_broken"]["enabled"] is False
