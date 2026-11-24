import json
from datetime import date, timedelta

from app.config import Settings
from app.snapshot import SNAPSHOT_MIN_MINUTES, build_snapshot, main, write_site


def settings_for(*providers: str) -> Settings:
    return Settings(enabled_providers=list(providers), cache_ttl_seconds=1, provider_timeout_seconds=5)


async def test_build_snapshot_shape_and_partial_failure():
    data = await build_snapshot(settings_for("mock_salata", "mock_jarun", "mock_broken"), horizon_days=3)

    assert data["timezone"] == "Europe/Zagreb"
    assert date.fromisoformat(data["date_to"]) - date.fromisoformat(data["date_from"]) == timedelta(days=2)
    assert "generated_at" in data

    by_key = {p["provider"]: p for p in data["providers"]}
    assert set(by_key) == {"mock_salata", "mock_jarun", "mock_broken"}
    assert by_key["mock_broken"]["status"] == "error" and by_key["mock_broken"]["error"]
    assert by_key["mock_salata"]["status"] == "ok" and by_key["mock_salata"]["club"]["key"] == "mock_salata"

    assert data["blocks"], "mock providers should yield blocks"
    b = data["blocks"][0]
    assert set(b) >= {"club", "court", "court_name", "indoor", "surface", "day", "start_min", "end_min", "start", "end", "minutes", "booking_url"}
    assert all(x["minutes"] >= SNAPSHOT_MIN_MINUTES for x in data["blocks"])
    assert all(x["end_min"] - x["start_min"] == x["minutes"] for x in data["blocks"])
    assert all(x["start"].startswith(x["day"]) for x in data["blocks"])
    assert all(x["club"] in by_key for x in data["blocks"])
    # mock providers include padel courts which must never appear
    assert all("padel" not in x["court_name"].lower() for x in data["blocks"])
    # sorted by day, club, court, start
    keys = [(x["day"], x["club"], x["court_name"], x["start_min"]) for x in data["blocks"]]
    assert keys == sorted(keys)


def test_workflow_url(monkeypatch):
    from app.snapshot import workflow_url

    for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(k, raising=False)
    assert workflow_url() is None
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "o/r/.github/workflows/pages.yml@refs/heads/main")
    assert workflow_url() == "https://github.com/o/r/actions/workflows/pages.yml"


def test_write_site(tmp_path):
    data = {"generated_at": "x", "timezone": "Europe/Zagreb", "date_from": "2026-01-01", "date_to": "2026-01-02", "providers": [], "blocks": []}
    out = tmp_path / "site"
    write_site(out, data)
    assert {p.name for p in out.iterdir()} == {"index.html", "site.js", "style.css", "availability.json", ".nojekyll"}
    assert json.loads((out / "availability.json").read_text(encoding="utf-8")) == data
    html = (out / "index.html").read_text(encoding="utf-8")
    assert './site.js' in html and './style.css' in html and "unpkg.com" not in html


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_ENABLED_PROVIDERS", "mock_salata,mock_broken")
    monkeypatch.setenv("OMNI_PROVIDER_TIMEOUT_SECONDS", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["--out", str(tmp_path / "a"), "--horizon", "2"]) == 0
        assert (tmp_path / "a" / "availability.json").exists()
        monkeypatch.setenv("OMNI_ENABLED_PROVIDERS", "mock_broken")
        get_settings.cache_clear()
        assert main(["--out", str(tmp_path / "b"), "--horizon", "2"]) == 1  # everything failed
    finally:
        get_settings.cache_clear()
