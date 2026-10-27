from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Credentials:
    __slots__ = ("username", "password")

    def __init__(self, username: str | None, password: str | None) -> None:
        self.username = username
        self.password = password

    @property
    def present(self) -> bool:
        return bool(self.username and self.password)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMNI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    timezone: str = "Europe/Zagreb"
    cache_ttl_seconds: int = 600
    provider_timeout_seconds: float = 20.0
    default_horizon_days: int = 14
    enabled_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mladost", "maksimir", "zagi"]
    )

    @field_validator("enabled_providers", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def credentials(self, provider_key: str) -> Credentials:
        """Read OMNI_<KEY>_USERNAME / OMNI_<KEY>_PASSWORD from the environment."""
        prefix = f"OMNI_{provider_key.upper()}_"
        return Credentials(os.getenv(prefix + "USERNAME"), os.getenv(prefix + "PASSWORD"))


@lru_cache
def get_settings() -> Settings:
    # pydantic-settings reads .env for its own fields only; export it so that
    # per-provider credentials (arbitrary OMNI_<KEY>_* vars) are reachable too.
    load_dotenv()
    return Settings()
