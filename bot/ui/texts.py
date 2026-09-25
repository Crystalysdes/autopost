"""Форматирование и тексты интерфейса (HTML)."""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CHAT_TYPES = {"channel": "канал", "supergroup": "супергруппа", "group": "группа"}
CHAT_ICONS = {"channel": "📢", "supergroup": "👥", "group": "👥"}
# Право бота в канале на ссылки-приглашения: в разных версиях Telegram оно называется по-разному
INVITE_RIGHT = "«Добавление подписчиков» («Пригласительные ссылки»)"


def esc(value: object, limit: int | None = None) -> str:
    """Обрезка ДО экранирования, чтобы не разрезать HTML-сущность."""
    text = "" if value is None else str(value)
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


def cut(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    n1 = n % 10
    if 10 < n < 20:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def utc_offset(tz: ZoneInfo, ts: int) -> str:
    offset = datetime.fromtimestamp(ts, tz).utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def tz_label(name: str, tz: ZoneInfo, ts: int) -> str:
    return f"{name} ({utc_offset(tz, ts)})"


def fmt_ts(ts: int, tz: ZoneInfo, now_ts: int) -> str:
    moment = datetime.fromtimestamp(ts, tz)
    today = datetime.fromtimestamp(now_ts, tz).date()
    day = moment.date()
    if day == today:
        prefix = "сегодня"
    elif day == today + timedelta(days=1):
        prefix = "завтра"
    elif day == today - timedelta(days=1):
        prefix = "вчера"
    else:
        prefix = f"{WEEKDAYS_SHORT[day.weekday()].lower()} {day:%d.%m}"
    return f"{prefix} {moment:%H:%M}"


def fmt_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y") if iso else "—"


def day_start_ts(tz: ZoneInfo, now_ts: int) -> int:
    today = datetime.fromtimestamp(now_ts, tz).date()
    return int(datetime(today.year, today.month, today.day, tzinfo=tz).timestamp())


def weekdays_label(weekdays: Sequence[int]) -> str:
    days = sorted(set(weekdays))
    if days == list(range(7)):
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    if days == [5, 6]:
        return "по выходным"
    if not days:
        return "дни не выбраны"
    return ", ".join(WEEKDAYS_SHORT[d] for d in days)


def times_label(times: Sequence[str]) -> str:
    if not times:
        return "не задано"
    count = len(times)
    shown = list(times) if count <= 12 else [*times[:10], "…"]
    return f"{', '.join(shown)} ({count} {plural(count, 'раз', 'раза', 'раз')} в день)"


def times_short(times: Sequence[str]) -> str:
    if not times:
        return "без расписания"
    return f"{len(times)}/день"


def period_label(start: str | None, end: str | None) -> str:
    if start and end:
        return f"с {fmt_date(start)} по {fmt_date(end)}"
    if start:
        return f"с {fmt_date(start)}"
    if end:
        return f"по {fmt_date(end)}"
    return "без ограничений"


def options_label(silent: bool, protect: bool, pin: bool, delete_prev: bool) -> str:
    parts = []
    if silent:
        parts.append("🔕 без звука")
    if protect:
        parts.append("🔒 защита")
    if pin:
        parts.append("📌 закреплять")
    if delete_prev:
        parts.append("🗑 удалять прошлый")
    return " · ".join(parts) if parts else "стандартные"


def names_label(names: Sequence[str], limit: int = 3, width: int = 30) -> str:
    """«A, B, C и ещё 2» — названия уже экранированы для HTML."""
    shown = [esc(name, width) for name in names[:limit]]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" и ещё {rest}" if rest > 0 else "")


def chat_kind(chat_type: str) -> str:
    return CHAT_TYPES.get(chat_type, chat_type)


# --------------------------------------------------------------------------- уведомления


def chat_added_text(title: str, chat_type: str, can_post: bool, can_pin: bool) -> str:
    lines = [f"✅ Бот добавлен в {chat_kind(chat_type)} «<b>{esc(title)}</b>».", ""]
    if not can_post:
        lines.append("⚠️ У бота нет права публиковать сообщения — выдайте его в настройках администраторов.")
    elif not can_pin:
        lines.append("Всё готово к публикациям. Чтобы закреплять посты, дайте боту право закрепления.")
    else:
        lines.append("Всё готово: создайте рассылку или примените черновик.")
    return "\n".join(lines)


def chat_pending_text(title: str, chat_type: str, actor_name: str, actor_id: int) -> str:
    return (
        f"⚠️ Бота добавил в {chat_kind(chat_type)} «<b>{esc(title)}</b>» другой человек: "
        f'<a href="tg://user?id={actor_id}">{esc(actor_name)}</a> (<code>{actor_id}</code>).\n\n'
        "Принять чат, чтобы публиковать в нём, или выйти из него?"
    )


def chat_back_text(title: str, stopped: int = 0) -> str:
    """stopped — сколько рассылок с этим чатом остановлено (например, старая версия бота останавливала
    их, когда бота удаляли из чата)."""
    text = (
        f"✅ Бот снова в «<b>{esc(title)}</b>».\nЗапущенные рассылки с этим чатом снова публикуют в него по расписанию."
    )
    if stopped:
        text += (
            f"\n\nОстановлено рассылок с этим чатом: {stopped}. Запустите их, когда будете готовы "
            "(кнопка ниже запустит все, где есть посты и время)."
        )
    return text


def chat_lost_text(title: str, reason: str) -> str:
    return (
        f"🚫 Бот потерял доступ к «<b>{esc(title)}</b>»: {esc(reason)}.\n\n"
        "Публикации в этот чат приостановлены, в остальных чатах рассылки работают как обычно. "
        "Верните бота администратором — публикации возобновятся сами, настройки сохранятся."
    )


def lost_post_right_text(title: str) -> str:
    return (
        f"⚠️ В «<b>{esc(title)}</b>» у бота забрали право публиковать сообщения. "
        "Пока его не вернут, посты туда отправляться не будут."
    )


def _failure_lines(failures: Sequence[tuple[str, str]], limit: int = 10) -> str:
    lines = [f"• «{esc(title, 40)}» — <i>{esc(error, 200)}</i>" for title, error in failures[:limit]]
    if len(failures) > limit:
        lines.append(f"…и ещё {len(failures) - limit}")
    return "\n".join(lines)


def send_failed_text(name: str, failures: Sequence[tuple[str, str]], sent: int, total: int) -> str:
    """failures — [(название чата, причина)]; sent из total — в скольких чатах пост вышел."""
    if len(failures) == 1:
        title, error = failures[0]
        head = (
            f"⚠️ Не удалось опубликовать пост рассылки «<b>{esc(name)}</b>» в «{esc(title)}».\n"
            f"Причина: <i>{esc(error, 400)}</i>"
        )
    else:
        head = (
            f"⚠️ Не удалось опубликовать пост рассылки «<b>{esc(name)}</b>» в {len(failures)} "
            f"{plural(len(failures), 'чат', 'чата', 'чатов')}:\n{_failure_lines(failures)}"
        )
    if sent:
        head += f"\nОпубликовано в {sent} из {total} {plural(total, 'чата', 'чатов', 'чатов')}."
    return head + "\n\nБот попробует снова в следующий раз по расписанию."


def auto_paused_text(name: str, failures: Sequence[tuple[str, str]], fails: int) -> str:
    """failures — [(название чата, последняя ошибка)] чатов, поставленных на паузу."""
    streak = f"{fails} {plural(fails, 'ошибка', 'ошибки', 'ошибок')} подряд"
    if len(failures) == 1:
        title, error = failures[0]
        head = (
            f"⏸ Рассылка «<b>{esc(name)}</b>»: публикации в «{esc(title)}» приостановлены — {streak}.\n"
            f"Последняя: <i>{esc(error, 400)}</i>"
        )
    else:
        head = (
            f"⏸ Рассылка «<b>{esc(name)}</b>»: публикации приостановлены в {len(failures)} "
            f"{plural(len(failures), 'чате', 'чатах', 'чатах')} — {streak}:\n{_failure_lines(failures)}"
        )
    return (
        head + "\n\nВ остальных чатах рассылка работает. Исправьте причину и нажмите «▶️ Возобновить» "
        "в «💬 Чаты рассылки»."
    )


def sub_notice_text(name_html: str, channels: Sequence[str]) -> str:
    """name_html — уже готовая ссылка на человека."""
    if len(channels) == 1:
        target = f"на канал «{esc(channels[0], 60)}»"
    else:
        target = "на каналы: " + ", ".join(f"«{esc(title, 40)}»" for title in channels)
    return f"👋 {name_html}, чтобы писать в этом чате, подпишитесь {target} и нажмите «✅ Проверить подписку»."


def no_delete_right_text(title: str) -> str:
    return (
        f"🛡 Защита в «<b>{esc(title)}</b>» не работает: у бота нет права «Удаление сообщений».\n\n"
        "Выдайте его в настройках администраторов чата — спам и сообщения без подписки снова начнут удаляться."
    )


def sub_check_failed_text(channel_title: str, reason: str) -> str:
    return (
        f"🔒 Не получается проверить подписку на «<b>{esc(channel_title)}</b>»: {esc(reason, 200)}.\n\n"
        "Пока это так, писать в чатах можно и без подписки на него. Бот должен быть администратором канала."
    )


def no_invite_link_text(channel_title: str, *, fallback: bool, error: str | None = None) -> str:
    """fallback — есть ли обычная ссылка на канал (публичный канал или сохранённая общая ссылка);
    error — ответ Telegram, если он отказал, хотя право вроде бы есть."""
    if error:
        reason = f"Telegram ответил «{esc(error, 200)}»"
        fix = f"Проверьте, что бот — администратор канала с правом {INVITE_RIGHT}."
    else:
        reason = f"у бота нет права {INVITE_RIGHT}"
        fix = "Выдайте его в настройках администраторов канала."
    now = (
        "Пока в подсказке обычная ссылка на канал вместо личной."
        if fallback
        else "Пока в подсказке нет кнопки на этот закрытый канал — без ссылки в него не вступить."
    )
    return (
        f"🔒 Не получается создать личные ссылки-приглашения в канал «<b>{esc(channel_title)}</b>»: {reason}.\n\n"
        f"{now} {fix}"
    )


def finished_text(name: str) -> str:
    return f"🏁 Рассылка «<b>{esc(name)}</b>» завершена: период публикаций закончился."


def no_posts_text(name: str) -> str:
    return (
        f"⚠️ Рассылка «<b>{esc(name)}</b>» остановлена: в ней не осталось постов. Добавьте посты и запустите её снова."
    )
