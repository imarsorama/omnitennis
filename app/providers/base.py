"""Provider protocol, registry and shared authenticated HTTP session helpers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from ..models import Club, Slot

if TYPE_CHECKING:
    from ..config import Settings


class ProviderError(Exception):
    """Raised by providers for expected failures (login, parsing, upstream)."""


class Provider(ABC):
    """One booking system (usually one club, sometimes several)."""

    key: str
    club: Club
    #: Env prefix for credentials (OMNI_<CREDENTIALS_KEY>_USERNAME / _PASSWORD).
    #: ``None`` means ``key``; platforms with one account for many clubs share a prefix.
    credentials_key: ClassVar[str | None] = None

    @classmethod
    def credentials_present(cls, settings: Settings) -> bool:
        return settings.credentials(cls.credentials_key or cls.key).present

    @abstractmethod
    async def fetch(self, date_from: date, date_to: date) -> list[Slot]:
        """Return free tennis slots in [date_from, date_to] (inclusive).

        Implementations must:
        - return only tennis courts (drop padel/squash/etc.),
        - produce timezone-aware datetimes,
        - set ``booking_url`` to the deepest link available for the slot.
        """

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    if not getattr(cls, "key", None):
        raise ValueError(f"{cls.__name__} must define a non-empty 'key'")
    _REGISTRY[cls.key] = cls
    return cls


def get_provider(key: str) -> type[Provider]:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise KeyError(f"Unknown provider '{key}'. Known: {sorted(_REGISTRY)}") from None


def all_providers() -> dict[str, type[Provider]]:
    return dict(_REGISTRY)


# --------------------------------------------------------------------------- #
# Authenticated session helpers
# --------------------------------------------------------------------------- #

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 OmniTennis/0.1"
    ),
    "Accept-Language": "hr-HR,hr;q=0.9,en;q=0.8",
}


class _BaseSession:
    def __init__(self, base_url: str = "", timeout: float = 20.0, **client_kwargs: Any) -> None:
        headers = {**DEFAULT_HEADERS, **client_kwargs.pop("headers", {})}
        self.client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, follow_redirects=True, headers=headers, **client_kwargs
        )
        self._lock = asyncio.Lock()
        self._authenticated = False

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _ensure_login(self, force: bool = False) -> None:
        async with self._lock:
            if self._authenticated and not force:
                return
            await self._login()
            self._authenticated = True

    async def _login(self) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _is_unauthorized(self, response: httpx.Response) -> bool:
        return response.status_code in (401, 403)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request, logging in first and retrying once if the session expired."""
        await self._ensure_login()
        response = await self.client.request(method, url, **kwargs)
        if self._is_unauthorized(response):
            await self._ensure_login(force=True)
            response = await self.client.request(method, url, **kwargs)
            if self._is_unauthorized(response):
                raise ProviderError(f"Still unauthorized after re-login: {method} {url}")
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


class CookieSession(_BaseSession):
    """Form login that sets a session cookie.

    ``login_data`` builds the form payload. ``logged_out`` is an optional
    predicate used to detect an expired session from a *successful* HTTP
    response (e.g. redirect to the login page or a login form in the body).
    """

    def __init__(
        self,
        login_url: str,
        login_data: Callable[[], Mapping[str, str]],
        *,
        logged_out: Callable[[httpx.Response], bool] | None = None,
        login_ok: Callable[[httpx.Response], bool] | None = None,
        prefetch_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._login_url = login_url
        self._login_data = login_data
        self._logged_out = logged_out
        self._login_ok = login_ok
        self._prefetch_url = prefetch_url

    async def _login(self) -> None:
        if self._prefetch_url:
            # Some systems need a CSRF cookie/token from the login page first.
            await self.client.get(self._prefetch_url)
        response = await self.client.post(self._login_url, data=dict(self._login_data()))
        ok = self._login_ok(response) if self._login_ok else response.status_code < 400
        if not ok:
            raise ProviderError(f"Login failed at {self._login_url} (HTTP {response.status_code})")

    def _is_unauthorized(self, response: httpx.Response) -> bool:
        if super()._is_unauthorized(response):
            return True
        return bool(self._logged_out and self._logged_out(response))


class TokenSession(_BaseSession):
    """API login returning a bearer token.

    ``obtain_token`` receives the client and must return the raw token string.
    """

    def __init__(
        self,
        obtain_token: Callable[[httpx.AsyncClient], Awaitable[str]],
        *,
        header_name: str = "Authorization",
        header_format: str = "Bearer {token}",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._obtain_token = obtain_token
        self._header_name = header_name
        self._header_format = header_format
        self.token: str | None = None

    async def _login(self) -> None:
        self.token = await self._obtain_token(self.client)
        if not self.token:
            raise ProviderError("Token login returned an empty token")
        self.client.headers[self._header_name] = self._header_format.format(token=self.token)
