from app.criteria import apply, default_criteria, merge_consecutive, process
from app.models import Court, Sport

from .conftest import DST_END_SUNDAY, MONDAY, SATURDAY, SUNDAY, criteria, dt, slot


def test_merge_consecutive_30min_slots(court):
    slots = [slot(court, MONDAY, 19, 19.5), slot(court, MONDAY, 19.5, 20), slot(court, MONDAY, 20, 20.5)]
    blocks = merge_consecutive(slots)
    assert len(blocks) == 1
    assert blocks[0].start == dt(MONDAY, 19) and blocks[0].end == dt(MONDAY, 20, 30)
    assert blocks[0].minutes == 90


def test_merge_keeps_gaps_separate(court):
    slots = [slot(court, MONDAY, 7, 8), slot(court, MONDAY, 9, 10)]
    blocks = merge_consecutive(slots)
    assert [(b.start.hour, b.end.hour) for b in blocks] == [(7, 8), (9, 10)]


def test_merge_is_per_court(club, court):
    other = Court(club=club, id="2", name="Court 2")
    slots = [slot(court, MONDAY, 7, 8), slot(other, MONDAY, 8, 9)]
    assert len(merge_consecutive(slots)) == 2


def test_merge_handles_unsorted_and_overlapping(court):
    slots = [slot(court, MONDAY, 8, 9), slot(court, MONDAY, 7, 8.5)]
    blocks = merge_consecutive(slots)
    assert len(blocks) == 1
    assert blocks[0].start == dt(MONDAY, 7) and blocks[0].end == dt(MONDAY, 9)


def test_any_time_of_day_is_kept(court):
    blocks = merge_consecutive([slot(court, MONDAY, 12, 16), slot(court, MONDAY, 19, 21)])
    out = apply(blocks, criteria(MONDAY))
    assert [(b.start.hour, b.end.hour) for b in out] == [(12, 16), (19, 21)]


def test_block_is_not_clipped(court):
    blocks = merge_consecutive([slot(court, MONDAY, 6, 23)])
    out = apply(blocks, criteria(MONDAY))
    assert len(out) == 1 and out[0].start == dt(MONDAY, 6) and out[0].end == dt(MONDAY, 23)


def test_min_minutes_default_two_hours(court):
    blocks = merge_consecutive([slot(court, MONDAY, 19, 20.5), slot(court, MONDAY, 7, 9)])
    out = apply(blocks, criteria(MONDAY))
    assert [(b.start.hour, b.end.hour) for b in out] == [(7, 9)]


def test_min_minutes_is_configurable(court):
    blocks = merge_consecutive([slot(court, MONDAY, 19, 20)])
    assert apply(blocks, criteria(MONDAY)) == []
    assert len(apply(blocks, criteria(MONDAY, min_minutes=60))) == 1


def test_weekend_block_until_midnight(court):
    blocks = merge_consecutive([slot(court, SATURDAY, 22, 23.983)])  # 22:00-23:59
    out = apply(blocks, criteria(SATURDAY, min_minutes=110))
    assert len(out) == 1


def test_date_range_filter(court):
    blocks = merge_consecutive([slot(court, SATURDAY, 10, 12), slot(court, SUNDAY, 10, 12)])
    assert [b.day for b in apply(blocks, criteria(SUNDAY))] == [SUNDAY]
    assert len(apply(blocks, criteria(SATURDAY, SUNDAY))) == 2


def test_non_tennis_courts_dropped(club):
    padel = Court(club=club, id="p1", name="Padel 1", sport=Sport.PADEL)
    blocks = merge_consecutive([slot(padel, SATURDAY, 10, 12)])
    assert apply(blocks, criteria(SATURDAY)) == []


def test_indoor_filter(club):
    indoor = Court(club=club, id="i", name="Hall 1", indoor=True)
    outdoor = Court(club=club, id="o", name="Court 1", indoor=False)
    unknown = Court(club=club, id="u", name="Court ?")
    blocks = merge_consecutive(
        [slot(indoor, SATURDAY, 10, 12), slot(outdoor, SATURDAY, 10, 12), slot(unknown, SATURDAY, 10, 12)]
    )
    assert len(apply(blocks, criteria(SATURDAY, indoor="any"))) == 3
    assert [b.court.id for b in apply(blocks, criteria(SATURDAY, indoor="indoor"))] == ["i"]
    assert [b.court.id for b in apply(blocks, criteria(SATURDAY, indoor="outdoor"))] == ["o"]


def test_club_filter(club, court):
    from app.models import Club

    other_club = Club(key="other", name="Other", url="https://o.test")
    other_court = Court(club=other_club, id="1", name="C1")
    blocks = merge_consecutive([slot(court, SATURDAY, 10, 12), slot(other_court, SATURDAY, 10, 12)])
    out = apply(blocks, criteria(SATURDAY, clubs=["other"]))
    assert [b.court.club.key for b in out] == ["other"]


def test_dst_end_duration_uses_wall_clock(court):
    # On DST-end night 01:00-04:00 local is 4 elapsed hours, but booking systems
    # (and users) reason in wall-clock hours, so the block counts as 3h.
    blocks = merge_consecutive([slot(court, DST_END_SUNDAY, 1, 4)])
    out = apply(blocks, criteria(DST_END_SUNDAY))
    assert len(out) == 1
    assert out[0].minutes == 180


def test_process_end_to_end_sorted(club, court):
    other = Court(club=club, id="2", name="Court 2")
    slots = [
        slot(other, MONDAY, 19, 21),
        slot(court, MONDAY, 7, 8),
        slot(court, MONDAY, 8, 9),
        slot(court, MONDAY, 9, 10),
    ]
    out = process(slots, criteria(MONDAY))
    assert [(b.court.id, b.start.hour) for b in out] == [("1", 7), ("2", 19)]


def test_default_criteria():
    c = default_criteria(MONDAY, 14)
    assert (c.date_to - c.date_from).days == 13
    assert c.min_minutes == 120
