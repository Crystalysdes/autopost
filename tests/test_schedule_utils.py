from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.services.schedule_utils import (
    ScheduleError,
    ScheduleSpec,
    effective_jitter,
    following_slots,
    max_gap_hours,
    next_slot,
    parse_date,
    parse_times,
    parse_window,
    plan_next,
    spread_times,
)

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
ALL = list(range(7))


def ts(year, month, day, hour, minute, tz=MSK) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


def local(value: int, tz=MSK) -> str:
    return datetime.fromtimestamp(value, tz).strftime("%a %d.%m %H:%M")


# ------------------------------------------------------------------ разбор ввода


def test_parse_times_normalizes_sorts_and_dedupes():
    assert parse_times("18:00, 9:00 13.30; 9") == ["09:00", "13:30", "18:00"]


@pytest.mark.parametrize("bad", ["", "25:00", "12:60", "abc", "9:5"])
def test_parse_times_rejects_garbage(bad):
    with pytest.raises(ScheduleError):
        parse_times(bad)


def test_parse_window_variants():
    assert parse_window("09:00-21:00") == (540, 1260)
    assert parse_window("с 9 до 21") == (540, 1260)
    assert parse_window("22:00 — 02:00") == (1320, 120)
    assert parse_window("9-24") == (540, 0)
    assert parse_window("круглосуточно") == (0, 0)
    with pytest.raises(ScheduleError):
        parse_window("в обед")


def test_spread_times_inclusive_window():
    assert spread_times(4, 540, 1260) == ["09:00", "13:00", "17:00", "21:00"]
    assert spread_times(1, 540, 1260) == ["09:00"]
    assert spread_times(3, 1320, 120) == ["00:00", "02:00", "22:00"]  # через полночь


def test_spread_times_full_day_is_even():
    assert spread_times(24, 0, 0) == [f"{h:02d}:00" for h in range(24)]
    assert spread_times(3, 0, 0) == ["00:00", "08:00", "16:00"]


def test_spread_times_rejects_too_many_for_window():
    with pytest.raises(ScheduleError):
        spread_times(20, 540, 550)
    with pytest.raises(ScheduleError):
        spread_times(0, 540, 1260)


def test_parse_date_formats():
    today = date(2026, 9, 25)
    assert parse_date("30.09", today) == date(2026, 9, 30)
    assert parse_date("01.01", today) == date(2027, 1, 1)  # без года — ближайшая в будущем
    assert parse_date("25.09.2026", today) == date(2026, 9, 25)
    assert parse_date("1.10.26", today) == date(2026, 10, 1)
    assert parse_date("2026-10-05", today) == date(2026, 10, 5)
    with pytest.raises(ScheduleError):
        parse_date("31.02", today)
    with pytest.raises(ScheduleError):
        parse_date("завтра", today)


# ------------------------------------------------------------------ слоты


def test_next_slot_same_day_and_rollover():
    times = ["09:00", "18:00"]
    assert local(next_slot(times, ALL, MSK, ts(2026, 9, 25, 8, 0))) == "Fri 25.09 09:00"
    assert local(next_slot(times, ALL, MSK, ts(2026, 9, 25, 9, 0))) == "Fri 25.09 18:00"  # строго после
    assert local(next_slot(times, ALL, MSK, ts(2026, 9, 25, 19, 0))) == "Sat 26.09 09:00"


def test_next_slot_respects_weekdays():
    # 25.09.2026 — пятница; только по понедельникам
    assert local(next_slot(["10:00"], [0], MSK, ts(2026, 9, 25, 12, 0))) == "Mon 28.09 10:00"


def test_next_slot_period_bounds():
    after = ts(2026, 9, 25, 12, 0)
    assert local(next_slot(["10:00"], ALL, MSK, after, start_date="2026-10-10")) == "Sat 10.10 10:00"
    assert next_slot(["10:00"], ALL, MSK, after, end_date="2026-09-25") is None
    assert local(next_slot(["13:00"], ALL, MSK, after, end_date="2026-09-25")) == "Fri 25.09 13:00"


def test_next_slot_empty_inputs():
    assert next_slot([], ALL, MSK, 0) is None
    assert next_slot(["10:00"], [], MSK, 0) is None


def test_next_slot_dst_spring_forward_keeps_utc_order():
    # 29.03.2026 в Берлине 02:00 -> 03:00. «02:30» становится 03:30 и идёт ПОСЛЕ 03:15.
    times = ["02:30", "03:15"]
    after = ts(2026, 3, 29, 0, 0, BERLIN)
    first = next_slot(times, ALL, BERLIN, after)
    second = next_slot(times, ALL, BERLIN, first)
    assert datetime.fromtimestamp(first, BERLIN).strftime("%H:%M") == "03:15"
    assert datetime.fromtimestamp(second, BERLIN).strftime("%H:%M") == "03:30"
    third = next_slot(times, ALL, BERLIN, second)
    assert datetime.fromtimestamp(third, BERLIN).strftime("%d %H:%M") == "30 02:30"


def test_next_slot_dst_fall_back_posts_once():
    # 25.10.2026 в Берлине 03:00 -> 02:00: час 02:xx повторяется, но публикация одна
    after = ts(2026, 10, 25, 0, 0, BERLIN)
    first = next_slot(["02:30"], ALL, BERLIN, after)
    second = next_slot(["02:30"], ALL, BERLIN, first)
    assert second - first > 23 * 3600


def test_effective_jitter_clamped_by_gap():
    assert effective_jitter(["09:00", "09:10"], 15) == 3
    assert effective_jitter(["09:00", "21:00"], 30) == 30
    assert effective_jitter(["09:00"], 15) == 15
    assert effective_jitter([], 15) == 0
    assert effective_jitter(["09:00", "09:01"], 30) == 0


def test_max_gap_hours():
    assert max_gap_hours(["09:00"], ALL) == 24
    assert max_gap_hours(["09:00"], [0]) == 24 * 7
    assert max_gap_hours(["09:00", "21:00"], ALL) == 12


def test_following_slots():
    spec = ScheduleSpec(times=["09:00", "18:00"], weekdays=ALL)
    slots = following_slots(spec, MSK, ts(2026, 9, 25, 10, 0), 3)
    assert [local(s) for s in slots] == ["Fri 25.09 18:00", "Sat 26.09 09:00", "Sat 26.09 18:00"]


@pytest.mark.parametrize("seed", range(40))
def test_simulation_every_slot_exactly_once(seed):
    """Моделируем тики планировщика со случайным разбросом: каждый слот ровно один раз."""
    rng = random.Random(seed)
    times = sorted({f"{rng.randint(0, 23):02d}:{rng.choice([0, 5, 10, 30, 45]):02d}" for _ in range(rng.randint(1, 6))})
    spec = ScheduleSpec(times=times, weekdays=ALL, jitter_min=rng.choice([0, 5, 10, 15, 30]))
    tz = rng.choice([MSK, BERLIN])
    start = int(datetime(2026, 3, 27, rng.randint(0, 23), 0, tzinfo=UTC).timestamp())  # захватываем переход DST
    end = start + 5 * 24 * 3600
    grace = 15 * 60

    now = start
    slot, run = plan_next(spec, tz, now, now, rng)
    posted = []
    while now < end:
        now += 15
        if run is not None and run <= now:
            if now - run > grace:
                slot, run = plan_next(spec, tz, now, now, rng)
            else:
                posted.append(slot)
                slot, run = plan_next(spec, tz, slot, now, rng)

    expected = []
    current = next_slot(spec.times, spec.weekdays, tz, start)
    jitter = effective_jitter(spec.times, spec.jitter_min) * 60
    while current is not None and current + jitter < end - 60:
        expected.append(current)
        current = next_slot(spec.times, spec.weekdays, tz, current)
    assert posted[: len(expected)] == expected
    assert len(posted) == len(set(posted))


def test_plan_next_never_in_past():
    rng = random.Random(1)
    spec = ScheduleSpec(times=["10:00"], weekdays=ALL, jitter_min=30)
    now = ts(2026, 9, 25, 9, 59)
    for _ in range(50):
        slot, run = plan_next(spec, MSK, now, now, rng)
        assert run >= now
        assert abs(run - slot) <= 30 * 60 or run == now
    assert plan_next(ScheduleSpec(times=[], weekdays=ALL), MSK, now, now, rng) == (None, None)


def test_timestamps_helper_sanity():
    assert ts(2026, 9, 25, 12, 0) - ts(2026, 9, 24, 12, 0) == int(timedelta(days=1).total_seconds())
