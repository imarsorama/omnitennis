from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Sport(StrEnum):
    TENNIS = "tennis"
    PADEL = "padel"
    SQUASH = "squash"
    OTHER = "other"


class Club(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    url: str
    address: str | None = None


class Court(BaseModel):
    model_config = ConfigDict(frozen=True)

    club: Club
    id: str
    name: str
    sport: Sport = Sport.TENNIS
    indoor: bool | None = None
    surface: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.club.key, self.id)


class Slot(BaseModel):
    """A single bookable free unit as exposed by the source (e.g. 30 or 60 min)."""

    model_config = ConfigDict(frozen=True)

    court: Court
    start: datetime  # timezone-aware
    end: datetime  # timezone-aware
    booking_url: str

    @model_validator(mode="after")
    def _check(self) -> "Slot":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Slot times must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("Slot end must be after start")
        return self


class FreeBlock(BaseModel):
    """Consecutive free slots on one court merged into a continuous interval."""

    model_config = ConfigDict(frozen=True)

    court: Court
    start: datetime
    end: datetime
    booking_url: str

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def day(self) -> date:
        return self.start.date()


IndoorFilter = Literal["any", "indoor", "outdoor"]


class Criteria(BaseModel):
    date_from: date
    date_to: date  # inclusive
    min_minutes: int = 120
    indoor: IndoorFilter = "any"
    clubs: list[str] | None = None  # club keys; None = all

    @model_validator(mode="after")
    def _check(self) -> "Criteria":
        if self.date_to < self.date_from:
            raise ValueError("date_to must be >= date_from")
        if self.min_minutes <= 0:
            raise ValueError("min_minutes must be positive")
        return self


ProviderStatus = Literal["ok", "cached", "error", "disabled"]


class ProviderResult(BaseModel):
    provider: str
    club: Club | None
    status: ProviderStatus
    blocks: list[FreeBlock] = Field(default_factory=list)
    error: str | None = None
    fetched_at: datetime | None = None
    elapsed_ms: int | None = None
