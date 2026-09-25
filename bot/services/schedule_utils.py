"""Расписание: разбор ввода, генерация времени «N раз в день», расчёт следующего слота.

Все функции чистые (без БД и Telegram) — их легко тестировать.
Моменты времени — UTC epoch-секунды, время суток — минуты от полуночи.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

MAX_TIMES = 144  # не чаще раза в 10 минут
SEARCH_DAYS = 9  # неделя + запас на переходы времени
JITTER_STEPS = (0, 5, 10, 15, 30)
DAY_MINUTES = 24 * 60

_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?$")
_DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?$")
_FULL_DAY_WORDS = {"круглосуточно", "весь день", "24/7", "24ч", "сутки"}


class ScheduleError(ValueError):
    """Ошибка во вводе пользователя; текст показывается как есть."""


# --------------------------------------------------------------------------- время суток


def parse_time(token: str) -> int:
    match = _TIME_RE.match(token.strip())
    if not match:
        raise ScheduleError(f"Не понял время «{token}». Пример: 09:30")
    hours, minutes = int(match.group(1)), int(match.group(2) or 0)
    if hours > 23 or minutes > 59:
        raise ScheduleError(f"Такого времени не бывает: «{token}»")
    return hours * 60 + minutes


def fmt_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def times_to_minutes(times: Sequence[str]) -> list[int]:
    return sorted(parse_time(t) for t in times)


def parse_times(text: str) -> list[str]:
    """«9:00, 13:30 18» -> ["09:00", "13:30", "18:00"]."""
    tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
    if not tokens:
        raise ScheduleError("Не нашёл ни одного времени. Пример: 09:00 13:30 18:00")
    minutes = sorted({parse_time(t) for t in tokens})
    if len(minutes) > MAX_TIMES:
        raise ScheduleError(f"Слишком много публикаций: максимум {MAX_TIMES} в день")
    return [fmt_minutes(m) for m in minutes]


def parse_window(text: str) -> tuple[int, int]:
    """«09:00-21:00», «9-21», «с 9 до 21», «круглосуточно» -> (начало, конец) в минутах.
    Начало == конец означает круглосуточно."""
    lowered = text.strip().lower()
    if lowered in _FULL_DAY_WORDS:
        return 0, 0
    parts = re.findall(r"\d{1,2}(?:[:.]\d{2})?", lowered)
    if len(parts) != 2:
        raise ScheduleError("Нужно два времени — начало и конец. Пример: 09:00-21:00")
    start = parse_time(parts[0])
    end_token = parts[1]
    end = 0 if end_token in ("24", "24:00", "24.00") else parse_time(end_token)
    return start, end


def spread_times(count: int, start: int, end: int) -> list[str]:
    """Равномерно раскладывает count публикаций в окне [start, end] (края включены).

    Окно может переходить через полночь (22:00-02:00). start == end — круглосуточно:
    шаг 24ч / count (24 раза -> каждый час).
    """
    if count < 1:
        raise ScheduleError("Количество публикаций должно быть от 1")
    if count > MAX_TIMES:
        raise ScheduleError(f"Максимум {MAX_TIMES} публикаций в день")
    if start == end:
        step = DAY_MINUTES / count
        minutes = {int(start + i * step + 0.5) % DAY_MINUTES for i in range(count)}
    else:
        span = (end - start) % DAY_MINUTES
        if count == 1:
            minutes = {start}
        else:
            step = span / (count - 1)
            minutes = {int(start + i * step + 0.5) % DAY_MINUTES for i in range(count)}
    if len(minutes) < count:
        raise ScheduleError(
            f"В окне {fmt_minutes(start)}–{fmt_minutes(end)} не помещается {count} публикаций. "
            "Увеличьте окно или уменьшите количество."
        )
    return [fmt_minutes(m) for m in sorted(minutes)]


def min_gap_minutes(times: Sequence[str]) -> int:
    """Минимальный интервал между соседними слотами (с учётом перехода через полночь)."""
    minutes = times_to_minutes(times)
    if len(minutes) < 2:
        return DAY_MINUTES
    gaps = [b - a for a, b in pairwise(minutes)]
    gaps.append(minutes[0] + DAY_MINUTES - minutes[-1])
    return min(gaps)


def effective_jitter(times: Sequence[str], jitter: int) -> int:
    """Разброс не больше трети минимального интервала — иначе посты могут
    поменяться местами или слипнуться."""
    if jitter <= 0 or not times:
        return 0
    return max(0, min(jitter, (min_gap_minutes(times) - 1) // 3))


def max_gap_hours(times: Sequence[str], weekdays: Sequence[int]) -> float:
    """Самый длинный промежуток между публикациями в часах (для предупреждения про 48 ч)."""
    if not times or not weekdays:
        return 0.0
    minutes = times_to_minutes(times)
    days = sorted(set(weekdays))
    points = [d * DAY_MINUTES + m for d in days for m in minutes]
    week = 7 * DAY_MINUTES
    gaps = [b - a for a, b in pairwise(points)]
    gaps.append(points[0] + week - points[-1])
    return max(gaps) / 60


# --------------------------------------------------------------------------- даты


def parse_date(text: str, today: date) -> date:
    """«25.09», «25.09.2026», «2026-09-25». Без года — ближайшая такая дата не в прошлом."""
    value = text.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    match = _DATE_RE.match(value)
    if not match:
        raise ScheduleError("Не понял дату. Пример: 25.09 или 25.09.2026")
    day, month, year_text = int(match.group(1)), int(match.group(2)), match.group(3)
    try:
        if year_text is None:
            result = date(today.year, month, day)
            if result < today:
                result = date(today.year + 1, month, day)
            return result
        year = int(year_text)
        if year < 100:
            year += 2000
        return date(year, month, day)
    except ValueError as exc:
        raise ScheduleError(f"Такой даты нет: «{value}»") from exc


# --------------------------------------------------------------------------- слоты


def next_slot(
    times: Sequence[str],
    weekdays: Sequence[int],
    tz: ZoneInfo,
    after_ts: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> int | None:
    """Ближайший слот строго после after_ts или None (нет времени/дней или период закончился).

    Берём минимум по UTC среди кандидатов, а не первый по местному времени: в день перехода
    на летнее время 02:30 превращается в 03:30 и может оказаться позже слота 03:15.
    """
    if not times or not weekdays:
        return None
    minutes = times_to_minutes(times)
    allowed = set(weekdays)
    first_day = datetime.fromtimestamp(after_ts, tz).date() - timedelta(days=1)
    if start_date:
        first_day = max(first_day, date.fromisoformat(start_date))
    last_day = date.fromisoformat(end_date) if end_date else None

    best: int | None = None
    for offset in range(SEARCH_DAYS):
        day = first_day + timedelta(days=offset)
        if last_day is not None and day > last_day:
            break
        if day.weekday() not in allowed:
            continue
        for m in minutes:
            local = datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=tz)
            ts = int(local.timestamp())
            if ts > after_ts and (best is None or ts < best):
                best = ts
    return best


@dataclass(frozen=True)
class ScheduleSpec:
    times: Sequence[str]
    weekdays: Sequence[int]
    jitter_min: int = 0
    start_date: str | None = None
    end_date: str | None = None


def plan_next(
    spec: ScheduleSpec,
    tz: ZoneInfo,
    after_ts: int,
    now_ts: int,
    rng: random.Random,
) -> tuple[int | None, int | None]:
    """(базовый слот, фактическое время запуска с разбросом)."""
    slot = next_slot(spec.times, spec.weekdays, tz, after_ts, spec.start_date, spec.end_date)
    if slot is None:
        return None, None
    jitter = effective_jitter(spec.times, spec.jitter_min)
    offset = rng.randint(-jitter * 60, jitter * 60) if jitter else 0
    return slot, max(slot + offset, now_ts)


def following_slots(spec: ScheduleSpec, tz: ZoneInfo, after_ts: int, count: int) -> list[int]:
    """Несколько следующих базовых слотов после after_ts (для экрана «ближайшие»)."""
    result: list[int] = []
    current = after_ts
    while len(result) < count:
        slot = next_slot(spec.times, spec.weekdays, tz, current, spec.start_date, spec.end_date)
        if slot is None:
            break
        result.append(slot)
        current = slot
    return result


def schedule_ended(spec: ScheduleSpec, tz: ZoneInfo, now_ts: int) -> bool:
    return (
        bool(spec.end_date) and next_slot(spec.times, spec.weekdays, tz, now_ts, spec.start_date, spec.end_date) is None
    )
