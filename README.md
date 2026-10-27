# OmniTennis – Zagreb tennis court availability aggregator

Single-user, local web app that queries several tennis-club booking systems,
normalizes their free slots, merges consecutive slots into blocks and shows them
on a per-day timeline, filtered by minimum duration (default 2 hours), indoor/outdoor
and club. Tennis only; padel and squash courts are dropped.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

## Run

From the project root (no activation needed):

```
.venv\Scripts\python.exe -m app
```

The server binds to `127.0.0.1` and reads the port from the `PORT` environment
variable (default `8000`; `.env` is loaded first, so `PORT=...` can live there).
Set `RELOAD=1` for auto-reload during development.

Equivalent explicit uvicorn command (cmd.exe syntax; use `$env:PORT` in PowerShell):

```
.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port %PORT%
```

The app is meant to sit behind a local reverse proxy (e.g. `http://omnitennis.localhost`):
all frontend URLs are relative, there is no CORS configuration and no absolute
redirects, so it works on whatever host/port the proxy exposes. Without a proxy,
open http://127.0.0.1:8000.

## Tests

```powershell
pytest
```

## Supported booking systems

| Key        | Club         | Platform                                  | Login |
|------------|--------------|-------------------------------------------|-------|
| `mladost`  | HATK Mladost | Playloc (`hatk-mladost.playloc.app`)      | no    |
| `maksimir` | TC Maksimir  | Sporty.plus (`maksimir.sporty.plus`)      | no    |
| `zagi`     | TK Zagi      | Tivazerra (`termini.zagi-sport.hr`)       | no    |
| `gradanski`| TK Građanski | Sport Router (`app.sportrouter.com`, mobile app) | yes |
| `prince`   | TK Prince    | Custom SPA (`prince.hr/app/book`)         | no    |

Most platforms host multiple clubs; adding another club on the same
platform is a short subclass of `PlaylocProvider` (`app/providers/playloc.py`:
`key`, `slug`, `club`), `SportyPlusProvider` (`app/providers/sportyplus.py`:
`key`, `subdomain`, `club`; `club_id` is resolved automatically),
`TivazerraProvider` (`app/providers/tivazerra.py`: `key`, `base_url`, `club`;
tennis venues are discovered automatically) or `SportRouterProvider`
(`app/providers/sportrouter.py`: `key`, `center_id`, `club`). Prince runs its
own single-club system (`app/providers/prince.py`).

Notes:
- Playloc, Tivazerra and Prince expose a 7-day booking horizon, so later dates return nothing.
- Prince needs a name and e-mail only when booking; reading availability is
  anonymous. The Book link pre-fills court, day and start time. Its clay courts
  sit under an air dome from 1 Oct to 16 Apr, so they count as indoor then.
- Sporty.plus deep links cannot carry a date; the Book link opens the courts
  page and you pick the day there.
- Sport Router has no web booking; the Book link opens `sportrouter.com` and
  the actual booking happens in the Sport Router mobile app. Centres publish
  only a few days ahead, and TK Građanski's outdoor clay courts close for
  winter (Nov–Mar), during which no slots are returned.

### Sport Router login

Sport Router's API requires a signed-in user, even for reading availability.
One account serves every Sport Router club. The mobile app signs in with
Google, but the backend also accepts plain email/password accounts, so create
a dedicated one for this app:

```powershell
.venv\Scripts\python.exe -m app.providers.sportrouter register you@example.com <password>
```

Then put the values in `.env`:

```
OMNI_SPORTROUTER_USERNAME=you@example.com
OMNI_SPORTROUTER_PASSWORD=<password>
```

Alternatively, for an existing Google account, set `OMNI_SPORTROUTER_REFRESH_TOKEN`
to the `refreshToken` the app's OAuth callback (`/auth/google/callback`) hands
back; the provider then calls `GET /auth/refresh` to obtain access tokens.

Development mocks: `mock_salata`, `mock_jarun`, `mock_broken`.

## Adding a booking system (provider)

1. Create `app/providers/<key>.py` with a class implementing `Provider`
   (see `app/providers/base.py` and `app/providers/mock.py`).
2. Register it with `@register` and add `<key>` to `OMNI_ENABLED_PROVIDERS` in `.env`.
3. If login is required, use `CookieSession` or `TokenSession` from `base.py`
   and read credentials via `settings.credentials("<key>")`
   (`OMNI_<KEY>_USERNAME` / `OMNI_<KEY>_PASSWORD`). Set `credentials_key` on the
   class when one account covers several clubs of a platform (e.g. `sportrouter`).
4. Return only tennis courts; set `indoor` / `surface` when the source exposes them
   and build a `booking_url` that lands the user as close as possible to the slot.

If a system is JS-rendered, install the optional `browser` extra
(`pip install -e ".[browser]"` and `playwright install chromium`).
