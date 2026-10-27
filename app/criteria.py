"""Merging of free slots into blocks and filtering by user criteria."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .models import Court, Criteria, FreeBlock, Slot, Sport

DEFAULT_MIN_MINUTES = 120


def default_criteria(today: date, horizon_days: int) -> Criteria:
    return Criteria(
        date_from=today,
        date_to=today + timedelta(days=horizon_days - 1),
        min_minutes=DEFAULT_MIN_MINUTES,
    )


def merge_consecutive(slots: list[Slot]) -> list[FreeBlock]:
    """Merge touching/overlapping free slots on the same court into blocks.

    Slots are grouped per court, sorted by start, and merged whenever the next
    slot starts at or before the current block ends. The booking URL of the
    first slot in the block is kept.
    """
    by_court: dict[tuple[str, str], list[Slot]] = defaultdict(list)
    for s in slots:
        by_court[s.court.key].append(s)

    blocks: list[FreeBlock] = []
    for court_slots in by_court.values():
        court_slots.sort(key=lambda s: s.start)
        current: FreeBlock | None = None
        for s in court_slots:
            if current is not None and s.start <= current.end:
                if s.end > current.end:
                    current = current.model_copy(update={"end": s.end})
                continue
            if current is not None:
                blocks.append(current)
            current = FreeBlock(
                court=s.court, start=s.start, end=s.end, booking_url=s.booking_url
            )
        if current is not None:
            blocks.append(current)

    blocks.sort(key=lambda b: (b.start, b.court.club.name, b.court.name))
    return blocks


def _court_matches(court: Court, criteria: Criteria) -> bool:
    if court.sport != Sport.TENNIS:
        return False
    if criteria.clubs and court.club.key not in criteria.clubs:
        return False
    if criteria.indoor == "indoor" and court.indoor is not True:
        return False
    if criteria.indoor == "outdoor" and court.indoor is not False:
        return False
    return True


def apply(blocks: list[FreeBlock], criteria: Criteria) -> list[FreeBlock]:
    """Keep blocks on matching courts, within the date range and long enough."""
    out = [
        b
        for b in blocks
        if _court_matches(b.court, criteria)
        and criteria.date_from <= b.day <= criteria.date_to
        and b.minutes >= criteria.min_minutes
    ]
    out.sort(key=lambda b: (b.start, b.court.club.name, b.court.name))
    return out


def drop_past(slots: list[Slot], now: datetime) -> list[Slot]:
    """Drop slots that have already started (they cannot be booked anymore)."""
    return [s for s in slots if s.start >= now]


def process(slots: list[Slot], criteria: Criteria, now: datetime | None = None) -> list[FreeBlock]:
    if now is not None:
        slots = drop_past(slots, now)
    return apply(merge_consecutive(slots), criteria)
