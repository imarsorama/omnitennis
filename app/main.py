from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .aggregator import Aggregator
from .config import Settings, get_settings
from .criteria import DEFAULT_MIN_MINUTES
from .models import Criteria, FreeBlock, IndoorFilter, ProviderResult
from .providers import all_providers, get_provider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    providers = []
    for key in settings.enabled_providers:
        try:
            providers.append(get_provider(key)())
        except KeyError as exc:
            log.error("%s", exc)
    log.info("Enabled providers: %s", [p.key for p in providers])
    app.state.aggregator = Aggregator(
        providers,
        tz=settings.tz,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        provider_timeout_seconds=settings.provider_timeout_seconds,
    )
    try:
        yield
    finally:
        await app.state.aggregator.aclose()


app = FastAPI(title="OmniTennis", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def get_aggregator(request: Request) -> Aggregator:
    return request.app.state.aggregator


# --------------------------------------------------------------------------- #
# Criteria parsing from query params
# --------------------------------------------------------------------------- #


def build_criteria(
    settings: Annotated[Settings, Depends(get_settings)],
    date_from: date | None = None,
    date_to: date | None = None,
    min_minutes: int = DEFAULT_MIN_MINUTES,
    indoor: IndoorFilter = "any",
    clubs: str | None = Query(None, description="comma-separated club keys"),
) -> Criteria:
    today = datetime.now(settings.tz).date()
    d_from = date_from or today
    d_to = date_to or (d_from + timedelta(days=settings.default_horizon_days - 1))
    try:
        return Criteria(
            date_from=d_from,
            date_to=d_to,
            min_minutes=min_minutes,
            indoor=indoor,
            clubs=[c.strip() for c in clubs.split(",") if c.strip()] if clubs else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Grouping for presentation
# --------------------------------------------------------------------------- #


def group_blocks(results: list[ProviderResult]) -> list[dict[str, Any]]:
    """day -> club -> court -> [blocks], all sorted."""
    days: dict[date, dict[str, dict[str, list[FreeBlock]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    club_names: dict[str, str] = {}
    court_names: dict[tuple[str, str], Any] = {}
    for r in results:
        for b in r.blocks:
            c = b.court
            club_names[c.club.key] = c.club.name
            court_names[(c.club.key, c.id)] = c
            days[b.day][c.club.key][c.id].append(b)

    out: list[dict[str, Any]] = []
    for day in sorted(days):
        clubs = []
        for club_key in sorted(days[day], key=lambda k: club_names[k]):
            courts = []
            for court_id in sorted(days[day][club_key], key=lambda cid: court_names[(club_key, cid)].name):
                blocks = sorted(days[day][club_key][court_id], key=lambda b: b.start)
                courts.append({"court": court_names[(club_key, court_id)], "blocks": blocks})
            clubs.append({"club": courts[0]["court"].club, "courts": courts, "count": sum(len(c["blocks"]) for c in courts)})
        out.append({"day": day, "clubs": clubs, "count": sum(c["count"] for c in clubs)})
    return out


def _minute_of_day(dt: datetime, day: date) -> int:
    """Minutes since midnight of ``day``; a timestamp on the next day maps to >= 1440."""
    return (dt.date() - day).days * 1440 + dt.hour * 60 + dt.minute


def timeline_layout(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute a shared hour axis and percentage positions for blocks.

    The axis spans from the earliest block start to the latest
    block end, rounded outwards to full hours, and is identical for every day so
    the days line up vertically.
    """
    lo, hi = 24 * 60, 0
    for d in days:
        for c in d["clubs"]:
            for ct in c["courts"]:
                for b in ct["blocks"]:
                    lo = min(lo, _minute_of_day(b.start, d["day"]))
                    hi = max(hi, _minute_of_day(b.end, d["day"]))
    if hi <= lo:  # no blocks: fall back to a sensible default range
        lo, hi = 7 * 60, 23 * 60
    lo = (lo // 60) * 60
    hi = min(24 * 60, -(-hi // 60) * 60)
    span = hi - lo

    def pos(start_min: int, end_min: int) -> dict[str, float]:
        start_min, end_min = max(start_min, lo), min(end_min, hi)
        return {"left": (start_min - lo) / span * 100, "width": max(end_min - start_min, 0) / span * 100}

    hours = [{"label": f"{h // 60:02d}", "left": (h - lo) / span * 100} for h in range(lo, hi + 1, 60)]

    for d in days:
        for c in d["clubs"]:
            for ct in c["courts"]:
                ct["placed"] = [
                    {"block": b, **pos(_minute_of_day(b.start, d["day"]), _minute_of_day(b.end, d["day"]))}
                    for b in ct["blocks"]
                ]
    return {"hours": hours, "start_label": f"{lo // 60:02d}:00", "end_label": f"{hi // 60:02d}:00"}


def provider_summary(results: list[ProviderResult], tz) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        rows.append(
            {
                "provider": r.provider,
                "club": r.club,
                "status": r.status,
                "error": r.error,
                "blocks": len(r.blocks),
                "fetched_at": r.fetched_at.astimezone(tz) if r.fetched_at else None,
                "elapsed_ms": r.elapsed_ms,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    agg: Annotated[Aggregator, Depends(get_aggregator)],
):
    today = datetime.now(settings.tz).date()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "defaults": {
                "date_from": today.isoformat(),
                "date_to": (today + timedelta(days=settings.default_horizon_days - 1)).isoformat(),
                "min_minutes": DEFAULT_MIN_MINUTES,
                "indoor": "any",
            },
            "clubs": [p.club for p in agg.providers],
            "cache_ttl_minutes": settings.cache_ttl_seconds // 60,
        },
    )


@app.get("/partials/results", response_class=HTMLResponse)
async def partial_results(
    request: Request,
    criteria: Annotated[Criteria, Depends(build_criteria)],
    settings: Annotated[Settings, Depends(get_settings)],
    agg: Annotated[Aggregator, Depends(get_aggregator)],
    force: bool = False,
):
    results = await agg.availability(criteria, force=force)
    days = group_blocks(results)
    axis = timeline_layout(days)
    return templates.TemplateResponse(
        request,
        "partials/results.html",
        {
            "days": days,
            "axis": axis,
            "providers": provider_summary(results, settings.tz),
            "criteria": criteria,
            "total": sum(len(r.blocks) for r in results),
            "now": datetime.now(settings.tz),
        },
    )


@app.get("/api/availability")
async def api_availability(
    criteria: Annotated[Criteria, Depends(build_criteria)],
    agg: Annotated[Aggregator, Depends(get_aggregator)],
    force: bool = False,
):
    results = await agg.availability(criteria, force=force)
    return {
        "criteria": criteria,
        "providers": [r.model_dump(exclude={"blocks"}) for r in results],
        "blocks": [
            {
                "club": b.court.club.key,
                "club_name": b.court.club.name,
                "court": b.court.id,
                "court_name": b.court.name,
                "indoor": b.court.indoor,
                "surface": b.court.surface,
                "start": b.start,
                "end": b.end,
                "minutes": b.minutes,
                "booking_url": b.booking_url,
            }
            for r in results
            for b in r.blocks
        ],
    }


@app.get("/api/providers")
async def api_providers(
    settings: Annotated[Settings, Depends(get_settings)],
    agg: Annotated[Aggregator, Depends(get_aggregator)],
):
    enabled = {p.key for p in agg.providers}
    return [
        {
            "key": key,
            "club": cls.club.model_dump() if getattr(cls, "club", None) else None,
            "enabled": key in enabled,
            "credentials_present": settings.credentials(key).present,
        }
        for key, cls in sorted(all_providers().items())
    ]
