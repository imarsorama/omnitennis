"""Build a static copy of the app for GitHub Pages.

``python -m app.snapshot --out site`` fetches availability from every enabled
provider once and writes:

- ``availability.json`` - merged free blocks (>= 30 min) plus provider status,
- ``index.html`` / ``site.js`` / ``style.css`` - a browser-only viewer that
  filters and renders the JSON client-side (no server needed).

A scheduled GitHub Actions workflow (``.github/workflows/pages.yml``) runs this
and deploys the folder to GitHub Pages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .aggregator import Aggregator
from .config import Settings, get_settings
from .models import Criteria, FreeBlock, ProviderResult
from .providers import get_provider

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SITE_SRC = BASE_DIR / "site"
STATIC_DIR = BASE_DIR / "static"
SNAPSHOT_MIN_MINUTES = 30  # keep every merged block; the viewer filters by duration


def _minute_of_day(dt: datetime, day: date) -> int:
    return (dt.date() - day).days * 1440 + dt.hour * 60 + dt.minute


def block_json(b: FreeBlock) -> dict[str, Any]:
    c = b.court
    return {
        "club": c.club.key,
        "court": c.id,
        "court_name": c.name,
        "indoor": c.indoor,
        "surface": c.surface,
        "day": b.day.isoformat(),
        "start_min": _minute_of_day(b.start, b.day),
        "end_min": _minute_of_day(b.end, b.day),
        "start": b.start.isoformat(),
        "end": b.end.isoformat(),
        "minutes": b.minutes,
        "booking_url": b.booking_url,
    }


def provider_json(r: ProviderResult, tz) -> dict[str, Any]:
    return {
        "provider": r.provider,
        "club": r.club.model_dump() if r.club else None,
        "status": r.status,
        "error": r.error,
        "fetched_at": r.fetched_at.astimezone(tz).isoformat() if r.fetched_at else None,
        "elapsed_ms": r.elapsed_ms,
        "blocks": len(r.blocks),
    }


def snapshot_json(results: list[ProviderResult], criteria: Criteria, settings: Settings, now: datetime) -> dict[str, Any]:
    blocks = [block_json(b) for r in results for b in r.blocks]
    blocks.sort(key=lambda b: (b["day"], b["club"], b["court_name"], b["start_min"]))
    return {
        "generated_at": now.isoformat(),
        "timezone": settings.timezone,
        "date_from": criteria.date_from.isoformat(),
        "date_to": criteria.date_to.isoformat(),
        "providers": [provider_json(r, settings.tz) for r in results],
        "blocks": blocks,
    }


async def build_snapshot(settings: Settings, horizon_days: int | None = None) -> dict[str, Any]:
    providers = []
    for key in settings.enabled_providers:
        try:
            providers.append(get_provider(key)())
        except KeyError as exc:
            log.error("%s", exc)
    agg = Aggregator(
        providers,
        tz=settings.tz,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        provider_timeout_seconds=settings.provider_timeout_seconds,
    )
    try:
        now = datetime.now(settings.tz)
        today = now.date()
        days = horizon_days or settings.default_horizon_days
        criteria = Criteria(date_from=today, date_to=today + timedelta(days=days - 1), min_minutes=SNAPSHOT_MIN_MINUTES)
        results = await agg.availability(criteria, force=True)
    finally:
        await agg.aclose()
    for r in results:
        log.info("%s: %s, %d blocks%s", r.provider, r.status, len(r.blocks), f" ({r.error})" if r.error else "")
    return snapshot_json(results, criteria, settings, now)


def write_site(out_dir: Path, data: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "site.js"):
        shutil.copyfile(SITE_SRC / name, out_dir / name)
    shutil.copyfile(STATIC_DIR / "style.css", out_dir / "style.css")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    (out_dir / "availability.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.snapshot")
    parser.add_argument("--out", type=Path, default=Path("site"), help="output folder (default: ./site)")
    parser.add_argument("--horizon", type=int, default=None, help="days to fetch (default: OMNI_DEFAULT_HORIZON_DAYS)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    data = asyncio.run(build_snapshot(settings, args.horizon))
    write_site(args.out, data)
    failed = [p["provider"] for p in data["providers"] if p["status"] == "error"]
    print(f"wrote {args.out / 'availability.json'}: {len(data['blocks'])} blocks, {len(data['providers'])} providers"
          + (f", failed: {', '.join(failed)}" if failed else ""))
    # Only fail the build when *nothing* could be fetched; partial data is still useful.
    return 1 if data["providers"] and len(failed) == len(data["providers"]) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
