---
name: Zagreb Tennis Availability Aggregator
overview: Build a local, single-user FastAPI web app that fans out to pluggable per-booking-system adapters, normalizes court availability into a unified model, merges consecutive free slots into blocks, applies editable criteria (weekday 07–10 / 19–22, weekends any time, min 2h, tennis only), and shows results grouped by day → club → court with deep links to book.
todos:
  - id: scaffold
    content: "Scaffold project: pyproject.toml (fastapi, uvicorn, httpx, jinja2, pydantic-settings, pytest), .env.example, README, app/ package"
    status: completed
  - id: models-config
    content: Implement app/config.py (settings, provider toggles, credentials) and app/models.py (Club, Court, Slot, FreeBlock, Criteria, ProviderResult)
    status: completed
  - id: criteria
    content: "Implement app/criteria.py: defaults (weekday 07-10/19-22, weekend all day, min 120 min), merge_consecutive, window clipping + filtering; tests in tests/test_criteria.py"
    status: completed
  - id: providers-base
    content: "Implement app/providers/base.py: Provider protocol, registry, CookieSession and TokenSession auth helpers"
    status: completed
  - id: mock-provider
    content: Implement app/providers/mock.py with fixture data (multiple clubs, mixed slot granularity, padel courts to exclude, indoor/outdoor)
    status: completed
  - id: aggregator-cache
    content: Implement app/cache.py (TTL) and app/aggregator.py (asyncio fan-out, per-provider timeout, error isolation, status); tests
    status: completed
  - id: api-ui
    content: "Implement FastAPI routes and Jinja2/HTMX UI: criteria form persisted in localStorage, results grouped day -> club -> court with Book links, provider status badges, force refresh"
    status: completed
  - id: verify
    content: Run tests, start server, verify UI end-to-end with mock provider
    status: completed
  - id: phase2-adapters
    content: "Phase 2: add one adapter per booking system as endpoints/credentials are provided (inspect, record fixture, implement fetch, map courts, booking_url)"
    status: in_progress
isProject: false
---

# Zagreb Tennis Availability Aggregator

## Decisions (from Q&A)
- Python 3.12 + FastAPI, `httpx` (async) for HTTP, Jinja2 + HTMX for UI; Playwright as an optional extra only if a system turns out to be JS-rendered.
- Single user, runs on localhost. No accounts. Criteria persisted in browser `localStorage`.
- On-demand fetch with in-memory TTL cache (default 10 min) + "Force refresh".
- Horizon default 14 days (editable). Deep-link to club booking page per slot.
- Defaults: weekdays 07:00–10:00 and 19:00–22:00; weekends any time; min block 2h, fully inside the window; tennis only (padel/squash dropped at adapter level); indoor/outdoor + surface shown as info with optional filter.
- Booking systems are unknown yet → adapter pattern; Phase 1 ships with a mock adapter, real adapters added one by one when endpoints arrive.

## Architecture

```mermaid
flowchart LR
  UI[Browser UI HTMX] -->|GET /api/availability + criteria| API[FastAPI]
  API --> AGG[Aggregator]
  AGG --> CACHE[TTL cache per provider]
  AGG -->|asyncio.gather, isolated errors| P1[Provider A]
  AGG --> P2[Provider B]
  AGG --> PM[Mock provider]
  P1 --> N[Normalize to Slot]
  P2 --> N
  PM --> N
  N --> MERGE[Merge consecutive slots per court]
  MERGE --> FILTER[Apply criteria]
  FILTER --> UI
```

## Project layout
- `pyproject.toml`, `.env.example`, `README.md`
- `app/main.py` – FastAPI app, routes: `GET /` (page), `GET /api/availability` (JSON), `GET /partials/results` (HTMX fragment), `GET /api/providers` (status)
- `app/config.py` – `pydantic-settings`; provider enable flags + credentials from env (`OMNI_<PROVIDER>_USERNAME/PASSWORD`), cache TTL, timezone `Europe/Zagreb`
- `app/models.py` – `Club`, `Court` (name, sport, indoor: bool|None, surface: str|None), `Slot` (court, start, end, booking_url), `FreeBlock` (merged), `Criteria` (date range, weekday windows, weekend windows, min_minutes, indoor filter, club filter), `ProviderResult` (blocks + status/error)
- `app/criteria.py` – defaults; `merge_consecutive(slots) -> blocks`; `apply(blocks, criteria)`: clip block to window per day-type, keep if clipped length ≥ min
- `app/cache.py` – simple async TTL cache keyed by (provider, date range)
- `app/aggregator.py` – fan-out to enabled providers with per-provider timeout; one failing provider never fails the page; returns status per provider
- `app/providers/base.py` – `Provider` protocol: `key`, `club`, `async fetch(date_from, date_to) -> list[Slot]`; registry; shared helpers: `CookieSession` (form login → cookie jar, re-login on 401/redirect) and `TokenSession` (API login → bearer header, refresh on expiry)
- `app/providers/mock.py` – fixture-driven provider used for development and tests
- `app/providers/<system>.py` – one file per booking system (Phase 2)
- `app/templates/index.html`, `app/templates/partials/results.html` – filter form (criteria), results grouped day → club → court → block rows with "Book" link, provider status badges (ok / cached / error)
- `app/static/app.js` – load/save criteria in `localStorage`, serialize to query params, HTMX trigger
- `tests/test_criteria.py` (merging, window clipping, weekend/weekday, 2h minimum, DST edges), `tests/test_aggregator.py` (error isolation, cache), `tests/providers/` with recorded fixtures per adapter

## Phase 1 – core + mock (this plan)
1. Scaffold project, settings, models.
2. Implement merge + criteria logic with tests.
3. Aggregator with cache and error isolation.
4. UI with editable criteria and grouped list view.
5. Mock provider with realistic fixture (multiple clubs, 30-min and 60-min granularity, padel courts to verify exclusion).

## Phase 2 – real adapters (one per endpoint you provide)
For each system: inspect endpoint (JSON vs HTML vs JS-rendered), record a fixture, implement `fetch()`, map court → sport/indoor/surface, build `booking_url`, add credentials to `.env.example` if required. If JS-rendered, add `playwright` optional dependency and a `BrowserSession` helper.

## Run
`uv run uvicorn app.main:app --reload` (or `pip install -e . && uvicorn ...`), open `http://localhost:8000`.