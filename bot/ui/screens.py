"""Экраны интерфейса. Каждая функция возвращает (текст в HTML, клавиатура) или None,
если объект уже удалён."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.app import App
from bot.db.models import Campaign, Chat, Post, now_ts
from bot.services import schedule_utils as su
from bot.services.buttons import buttons_to_html, count_buttons, has_icons
from bot.services.content import custom_emoji_count, post_text, post_title, supports_buttons
from bot.services.scheduler import predict_runs, spec_of
from bot.ui import texts as t
from bot.ui.callbacks import (
    ApplyDraft,
    CampAct,
    ChatAct,
    Nav,
    OptAct,
    PickAct,
    PostAct,
    SchedAct,
    SetAct,
)
from bot.ui.keyboards import BLUE, GREEN, RED, add_channel_link, add_group_link, back, btn, markup, url_btn

Screen = tuple[str, InlineKeyboardMarkup]

PAGE_SIZE = 8
POSTS_PAGE_SIZE = 10
MAX_LIST_BUTTONS = 40  # длинные списки режем, чтобы не упереться в лимиты Telegram
COUNT_CHOICES = (1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24)
WINDOWS = ((540, 1260), (600, 1320), (480, 1380), (720, 1200), (0, 0))
TIMEZONES = (
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Магадан", "Asia/Magadan"),
    ("Камчатка", "Asia/Kamchatka"),
    ("Минск", "Europe/Minsk"),
    ("Киев", "Europe/Kyiv"),
    ("Кишинёв", "Europe/Chisinau"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Ереван", "Asia/Yerevan"),
    ("Баку", "Asia/Baku"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Бишкек", "Asia/Bishkek"),
    ("Душанбе", "Asia/Dushanbe"),
    ("UTC", "UTC"),
)


def _now(app: App) -> int:
    return app.scheduler.now() if app.scheduler else now_ts()


def _pages(total: int, page: int) -> tuple[int, int]:
    pages = max(1, math.ceil(total / PAGE_SIZE))
    return pages, min(max(page, 0), pages - 1)


def _pager(page: int, pages: int, make: Any) -> list[InlineKeyboardButton] | None:
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(btn("«", make(page - 1)))
    row.append(btn(f"{page + 1}/{pages}", make(page)))
    if page < pages - 1:
        row.append(btn("»", make(page + 1)))
    return row


def window_label(window: tuple[int, int]) -> str:
    start, end = window
    if start == end:
        return "круглосуточно"
    return f"{su.fmt_minutes(start)}–{su.fmt_minutes(end)}"


# ------------------------------------------------------------------------- главное


async def main_menu(app: App) -> Screen:
    now = _now(app)
    tz = app.settings.tz
    chats = await app.repo.list_chats()
    active_chats = sum(1 for c in chats if c.status == "active")
    pending = sum(1 for c in chats if c.status == "pending")
    total, active = await app.repo.campaign_totals()
    counts = await app.repo.log_counts_since(t.day_start_ts(tz, now))
    upcoming = [(c, chat) for c, chat in await app.repo.active_campaigns_with_chats() if c.next_run_ts]

    lines = ["<b>🤖 Автопостинг</b>", ""]
    chat_line = f"💬 Чатов: <b>{active_chats}</b>"
    if pending:
        chat_line += f" · ⏳ ждут подтверждения: {pending}"
    lines.append(chat_line)
    lines.append(f"📬 Рассылок работает: <b>{active}</b> из {total}")
    sent_line = f"📤 Сегодня опубликовано: <b>{counts.get('sent', 0)}</b>"
    if counts.get("failed"):
        sent_line += f" · ошибок: {counts['failed']}"
    lines.append(sent_line)
    if upcoming and not app.settings.paused_all:
        campaign, chat = upcoming[0]
        lines.append(f"⏭ Следующая: {t.fmt_ts(campaign.next_run_ts, tz, now)} · {t.esc(chat.title, 30)}")
    if app.settings.paused_all:
        lines += ["", "⏸ <b>Все рассылки на паузе.</b> Возобновить — в настройках."]
    if not chats:
        lines += [
            "",
            "Чтобы начать, добавьте бота администратором в группу или канал — кнопками внизу экрана "
            "или в разделе «Мои чаты». Чат сразу появится здесь.",
        ]
    keyboard = markup(
        [btn("💬 Мои чаты", Nav(to="chats"), BLUE), btn("📝 Черновики", Nav(to="drafts"))],
        [btn("📅 Ближайшие публикации", Nav(to="upcoming"))],
        [btn("⚙️ Настройки", Nav(to="settings")), btn("❓ Помощь", Nav(to="help"))],
    )
    return "\n".join(lines), keyboard


# --------------------------------------------------------------------------- чаты


def chat_icon(chat: Chat, stats: tuple[int, int] | None) -> str:
    if chat.status == "pending":
        return "⏳"
    if chat.status == "left":
        return "🚫"
    if not chat.can_post:
        return "⚠️"
    total, active = stats or (0, 0)
    if active:
        return "🟢"
    if total:
        return "⏸"
    return "⚪"


async def chats_list(app: App, page: int = 0) -> Screen:
    chats = await app.repo.list_chats()
    stats = await app.repo.campaign_stats_by_chat()
    pages, page = _pages(len(chats), page)
    lines = ["<b>💬 Мои чаты</b>", ""]
    if not chats:
        lines.append(
            "Чатов пока нет. Добавьте бота администратором в группу или канал:\n"
            "• кнопками «➕ Добавить…» внизу экрана — Telegram сам предложит выбрать чат и выдать права;\n"
            "• или ссылками ниже;\n"
            "• или вручную через настройки чата → Администраторы.\n\n"
            "Чат появится в этом списке автоматически."
        )
    else:
        lines.append(
            "🟢 рассылки идут · ⏸ на паузе · ⚪ нет рассылок\n⚠️ нет прав · ⏳ ждёт подтверждения · 🚫 бота нет в чате"
        )
    rows: list[list[InlineKeyboardButton] | None] = []
    for chat in chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        icon = chat_icon(chat, stats.get(chat.id))
        type_icon = t.CHAT_ICONS.get(chat.type, "💬")
        rows.append([btn(f"{icon} {type_icon} {t.cut(chat.title, 40)}", Nav(to="chat", id=chat.id))])
    rows.append(_pager(page, pages, lambda p: Nav(to="chats", page=p)))
    if app.bot_username:
        rows.append(
            [
                url_btn("➕ Группа", add_group_link(app.bot_username)),
                url_btn("➕ Канал", add_channel_link(app.bot_username)),
            ]
        )
    rows.append(back("main", text="« Меню"))
    return "\n".join(lines), markup(*rows)


async def chat_view(app: App, chat_id: int) -> Screen | None:
    chat = await app.repo.get_chat(chat_id)
    if chat is None:
        return None
    campaigns = await app.repo.list_campaigns(chat.id)
    type_icon = t.CHAT_ICONS.get(chat.type, "💬")
    subtitle = t.chat_kind(chat.type)
    if chat.username:
        subtitle += f" · @{t.esc(chat.username)}"
    if chat.is_forum:
        subtitle += " · форум"
    lines = [f"<b>{type_icon} {t.esc(chat.title)}</b>", subtitle, f"ID: <code>{chat.tg_id}</code>", ""]

    if chat.status == "pending":
        lines.append("⏳ <b>Бота добавил не админ.</b> Примите чат, чтобы публиковать в нём, или выйдите из него.")
    elif chat.status == "left":
        lines.append(
            "🚫 <b>Бота нет в этом чате</b> — его удалили или лишили прав. "
            "Добавьте бота снова администратором: рассылки и настройки сохранятся."
        )
    else:
        post_mark = "✅" if chat.can_post else "❌"
        pin_mark = "✅" if chat.can_pin else "❌"
        lines.append(f"Права бота: {post_mark} публикация · {pin_mark} закрепление")
        if not chat.can_post:
            lines.append("⚠️ Бот не может публиковать здесь — выдайте ему право «Публикация сообщений».")
    lines.append("")
    if campaigns:
        lines.append(f"📬 Рассылок: <b>{len(campaigns)}</b>")
        if len(campaigns) > MAX_LIST_BUTTONS:
            lines.append(f"Показаны первые {MAX_LIST_BUTTONS}.")
    else:
        lines.append("Рассылок пока нет — создайте первую или примените черновик.")

    rows: list[list[InlineKeyboardButton] | None] = []
    if chat.status == "pending":
        rows.append(
            [
                btn("✅ Принять", ChatAct(a="accept", id=chat.id), GREEN),
                btn("🚪 Выйти", ChatAct(a="leave", id=chat.id), RED),
            ]
        )
    for campaign in campaigns[:MAX_LIST_BUTTONS]:
        icon = "🟢" if campaign.is_active else "⏸"
        rows.append(
            [
                btn(
                    f"{icon} {t.cut(campaign.name, 32)} · {t.times_short(campaign.times)}",
                    Nav(to="camp", id=campaign.id),
                )
            ]
        )
    rows.append(
        [
            btn("➕ Новая рассылка", ChatAct(a="new", id=chat.id), BLUE),
            btn("📝 Из черновика", ChatAct(a="fromdraft", id=chat.id)),
        ]
    )
    if chat.status == "active" and any(not c.is_active for c in campaigns):
        rows.append([btn("▶️ Запустить все на паузе", ChatAct(a="resume", id=chat.id))])
    rows.append(
        [
            btn("🔄 Обновить права", ChatAct(a="refresh", id=chat.id)),
            btn("🗑 Удалить чат", ChatAct(a="del", id=chat.id), RED),
        ]
    )
    rows.append(back("chats", text="« Мои чаты"))
    return "\n".join(lines), markup(*rows)


async def chat_delete_confirm(app: App, chat_id: int) -> Screen | None:
    chat = await app.repo.get_chat(chat_id)
    if chat is None:
        return None
    count = await app.repo.count_campaigns(chat.id)
    text = (
        f"🗑 Удалить «<b>{t.esc(chat.title)}</b>» из бота?\n\n"
        f"Вместе с ним удалятся рассылки этого чата: {count}. Черновики не пострадают.\n\n"
        "• <b>Удалить и выйти</b> — бот покинет чат.\n"
        "• <b>Только удалить</b> — бот останется в чате, но пропадёт из списка "
        "(вернуть можно кнопкой «➕ Добавить…»)."
    )
    keyboard = markup(
        [btn("🚪 Удалить и выйти", ChatAct(a="delleave", id=chat.id), RED)],
        [btn("🗑 Только удалить", ChatAct(a="delonly", id=chat.id), RED)],
        back("chat", chat.id, "✖️ Отмена"),
    )
    return text, keyboard


async def drafts_for_chat(app: App, chat_id: int) -> Screen | None:
    chat = await app.repo.get_chat(chat_id)
    if chat is None:
        return None
    drafts = await app.repo.list_drafts()
    counts = await app.repo.post_counts(d.id for d in drafts)
    lines = [f"<b>📝 Применить черновик к «{t.esc(chat.title)}»</b>", ""]
    if drafts:
        lines.append(
            "Выберите черновик. В чате появится рассылка с его постами, расписанием и опциями "
            "(на паузе — проверьте и запустите). Если черновик тут уже применялся, рассылка обновится."
        )
    else:
        lines.append(
            "Черновиков пока нет. Сохраните любую рассылку кнопкой «💾 В черновики» "
            "или создайте черновик в разделе «📝 Черновики»."
        )
    rows: list[list[InlineKeyboardButton] | None] = [
        [
            btn(
                f"📝 {t.cut(d.name, 30)} · {counts.get(d.id, 0)} пост. · {t.times_short(d.times)}",
                ApplyDraft(chat=chat.id, draft=d.id),
            )
        ]
        for d in drafts[:MAX_LIST_BUTTONS]
    ]
    if len(drafts) > MAX_LIST_BUTTONS:
        lines.append(f"\nПоказаны первые {MAX_LIST_BUTTONS} черновиков.")
    rows.append(back("chat", chat.id))
    return "\n".join(lines), markup(*rows)


# ----------------------------------------------------------------------- рассылки


def _status_line(campaign: Campaign, chat: Chat | None, paused_all: bool) -> str:
    if chat is None or chat.status != "active":
        return "🚫 чат недоступен"
    if not campaign.is_active:
        return "⏸ остановлена"
    if paused_all:
        return "⏸ все рассылки на паузе (см. настройки)"
    return "🟢 работает"


def _uses_premium(posts: list[Post]) -> bool:
    return any(
        (custom_emoji_count(p.kind, p.payload) and not (p.send_mode == "forward" and p.forward_from))
        or has_icons(p.buttons)
        for p in posts
    )


def campaign_warnings(campaign: Campaign, chat: Chat | None, posts: list[Post], tz: Any, now: int) -> list[str]:
    warnings = []
    if chat is not None and chat.type == "channel" and _uses_premium(posts):
        warnings.append(
            "💎 В постах есть премиум-эмодзи. В каналах бот показывает их, только если ему куплен юзернейм "
            "на Fragment — иначе они станут обычными. Альтернатива: переслать пост из своего канала "
            "и включить у него режим «↪️ Пересылкой»."
        )
    if campaign.delete_prev and su.max_gap_hours(campaign.times, campaign.weekdays) > 48:
        warnings.append("🗑 Между публикациями больше 48 часов — Telegram не даст удалить прошлый пост.")
    if chat is not None and campaign.pin and not chat.can_pin:
        warnings.append("📌 У бота нет права закреплять сообщения в этом чате.")
    if campaign.end_date and su.schedule_ended(spec_of(campaign), tz, now):
        warnings.append("📆 Период публикаций уже закончился.")
    return warnings


async def campaign_view(app: App, campaign_id: int, note: str | None = None) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    posts = await app.repo.list_posts(campaign.id)
    chat = await app.repo.get_chat(campaign.chat_id) if campaign.chat_id else None
    tz, now = app.settings.tz, _now(app)
    lines: list[str] = []

    linked = len(await app.repo.linked_campaigns(campaign.id)) if campaign.is_draft else 0
    if campaign.is_draft:
        lines.append(f"<b>📝 Черновик «{t.esc(campaign.name)}»</b>")
        lines.append(f"Применён в чатах: {linked}" if linked else "Ещё не применён ни к одному чату")
    else:
        lines.append(f"<b>📬 Рассылка «{t.esc(campaign.name)}»</b>")
        if chat is not None:
            lines.append(f"{t.CHAT_ICONS.get(chat.type, '💬')} {t.esc(chat.title)}")
        lines.append(f"Статус: {_status_line(campaign, chat, app.settings.paused_all)}")
    lines.append("")

    post_line = f"📝 Постов: <b>{len(posts)}</b>"
    if len(posts) > 1:
        post_line += " · " + ("по очереди" if campaign.rotation == "sequential" else "в случайном порядке")
    lines.append(post_line)
    lines.append(f"⏰ {t.times_label(campaign.times)}")
    day_line = f"📅 {t.weekdays_label(campaign.weekdays)}"
    jitter = su.effective_jitter(campaign.times, campaign.jitter_min)
    if jitter:
        day_line += f" · 🎲 ±{jitter} мин"
    if campaign.start_date or campaign.end_date:
        day_line += f" · 📆 {t.period_label(campaign.start_date, campaign.end_date)}"
    lines.append(day_line)
    lines.append(
        "⚙️ "
        + t.options_label(campaign.silent, campaign.protect, campaign.pin, campaign.delete_prev, campaign.thread_id)
    )

    if not campaign.is_draft:
        predictions = predict_runs(campaign, posts, tz, 3)
        if predictions and not app.settings.paused_all:
            parts = []
            for ts, approx, number in predictions:
                item = ("≈ " if approx else "") + t.fmt_ts(ts, tz, now)
                if number and len(posts) > 1:
                    item += f" (#{number})"
                parts.append(item)
            lines.append("⏭ Ближайшие: " + ", ".join(parts))
        stats = f"📊 Опубликовано: {campaign.sent_count}"
        if campaign.last_sent_ts:
            stats += f" · последний {t.fmt_ts(campaign.last_sent_ts, tz, now)}"
        lines.append(stats)
        if campaign.fail_count:
            lines.append(f"⚠️ Ошибок подряд: {campaign.fail_count}")
        if campaign.last_error:
            lines.append(f"⚠️ <i>{t.esc(campaign.last_error, 300)}</i>")

    missing = []
    if not posts:
        missing.append("добавьте посты")
    if not campaign.times:
        missing.append("задайте время")
    if not campaign.weekdays:
        missing.append("выберите дни")
    if missing:
        lines += ["", "👉 Чтобы запустить: " + ", ".join(missing) + "."]
    warnings = campaign_warnings(campaign, chat, posts, tz, now)
    if warnings:
        lines += ["", *warnings]
    if note:
        lines += ["", note]

    cid = campaign.id
    rows: list[list[InlineKeyboardButton] | None] = []
    if campaign.is_draft:
        linked_count = linked
        rows += [
            [
                btn(f"📝 Посты ({len(posts)})", Nav(to="posts", id=cid)),
                btn("⏰ Расписание", Nav(to="sched", id=cid)),
            ],
            [btn("⚙️ Опции", Nav(to="opts", id=cid)), btn("👁 Предпросмотр", CampAct(a="preview", id=cid))],
            [btn("📤 Применить к чатам", CampAct(a="apply", id=cid), GREEN)],
        ]
        if linked_count:
            rows.append([btn(f"🔄 Обновить во всех ({linked_count})", CampAct(a="sync", id=cid))])
        rows += [
            [
                btn("✏️ Переименовать", CampAct(a="rename", id=cid)),
                btn("🗑 Удалить", CampAct(a="del", id=cid), RED),
            ],
            back("drafts", text="« Черновики"),
        ]
    else:
        toggle = (
            btn("⏸ Остановить", CampAct(a="off", id=cid), RED)
            if campaign.is_active
            else btn("▶️ Запустить", CampAct(a="on", id=cid), GREEN)
        )
        rows += [
            [toggle],
            [
                btn(f"📝 Посты ({len(posts)})", Nav(to="posts", id=cid)),
                btn("⏰ Расписание", Nav(to="sched", id=cid)),
            ],
            [btn("⚙️ Опции", Nav(to="opts", id=cid)), btn("👁 Предпросмотр", CampAct(a="preview", id=cid))],
            [btn("🚀 Отправить сейчас", CampAct(a="send", id=cid))],
            [
                btn("💾 В черновики", CampAct(a="todraft", id=cid)),
                btn("📋 В другие чаты", CampAct(a="copy", id=cid)),
            ],
            [
                btn("✏️ Переименовать", CampAct(a="rename", id=cid)),
                btn("🗑 Удалить", CampAct(a="del", id=cid), RED),
            ],
            back("chat", campaign.chat_id or 0, "« К чату"),
        ]
    return "\n".join(lines), markup(*rows)


async def campaign_delete_confirm(app: App, campaign_id: int) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    what = "черновик" if campaign.is_draft else "рассылку"
    text = f"🗑 Удалить {what} «<b>{t.esc(campaign.name)}</b>» вместе с постами?"
    if campaign.is_draft:
        text += "\n\nРассылки, созданные из него, останутся — просто перестанут быть связаны с черновиком."
    keyboard = markup(
        [btn("🗑 Да, удалить", CampAct(a="del_ok", id=campaign.id), RED)],
        back("camp", campaign.id, "✖️ Отмена"),
    )
    return text, keyboard


async def send_now_confirm(app: App, campaign_id: int) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None or campaign.chat_id is None:
        return None
    chat = await app.repo.get_chat(campaign.chat_id)
    posts = await app.repo.list_posts(campaign.id)
    title = t.esc(chat.title) if chat else "чат"
    text = (
        f"🚀 Опубликовать следующий пост рассылки «<b>{t.esc(campaign.name)}</b>» в «{title}» прямо сейчас?\n\n"
        "Расписание не изменится."
    )
    if len(posts) > 1 and campaign.rotation == "sequential":
        ids = [p.id for p in posts]
        number = (ids.index(campaign.last_post_id) + 1) % len(ids) + 1 if campaign.last_post_id in ids else 1
        text += f"\nБудет отправлен пост #{number}."
    keyboard = markup(
        [btn("🚀 Отправить", CampAct(a="send_ok", id=campaign.id), GREEN)],
        back("camp", campaign.id, "✖️ Отмена"),
    )
    return text, keyboard


async def draft_saved(app: App, draft_id: int, source_id: int) -> Screen | None:
    draft = await app.repo.get_campaign(draft_id)
    if draft is None:
        return None
    text = (
        f"💾 Черновик «<b>{t.esc(draft.name)}</b>» сохранён.\n\n"
        "Теперь его можно применить к другим чатам. Если потом изменить черновик, "
        "кнопка «🔄 Обновить во всех» перенесёт изменения во все чаты, где он применён "
        "(включая эту рассылку)."
    )
    keyboard = markup(
        [btn("📤 Применить к чатам", CampAct(a="apply", id=draft.id), GREEN)],
        [btn("📝 Открыть черновик", Nav(to="camp", id=draft.id))],
        back("camp", source_id, "« К рассылке"),
    )
    return text, keyboard


async def sync_confirm(app: App, draft_id: int) -> Screen | None:
    draft = await app.repo.get_campaign(draft_id)
    if draft is None:
        return None
    linked = await app.repo.linked_campaigns(draft.id)
    chats = {c.id: c for c in await app.repo.list_chats()}
    names = [t.esc(chats[c.chat_id].title, 40) for c in linked if c.chat_id in chats]
    text = (
        f"🔄 Обновить рассылки из черновика «<b>{t.esc(draft.name)}</b>»?\n\n"
        f"Посты, расписание и опции будут заменены в {len(linked)} "
        f"{t.plural(len(linked), 'рассылке', 'рассылках', 'рассылках')}:\n"
        + "\n".join(f"• {name}" for name in names[:20])
        + ("\n…" if len(names) > 20 else "")
        + "\n\nСтатус (работает/пауза) и тема форума у каждой останутся прежними."
    )
    keyboard = markup(
        [btn("🔄 Обновить", CampAct(a="sync_ok", id=draft.id), GREEN)],
        back("camp", draft.id, "✖️ Отмена"),
    )
    return text, keyboard


# --------------------------------------------------------------------------- посты


async def posts_view(app: App, campaign_id: int, page: int = 0) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    posts = await app.repo.list_posts(campaign.id)
    pages = max(1, math.ceil(len(posts) / POSTS_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    first = page * POSTS_PAGE_SIZE
    shown = list(enumerate(posts, start=1))[first : first + POSTS_PAGE_SIZE]
    lines = [f"<b>📝 Посты · «{t.esc(campaign.name)}»</b>", ""]
    if not posts:
        lines.append(
            "Постов пока нет. Нажмите «➕ Добавить посты» и пришлите сообщения: текст, фото, видео, GIF, "
            "файлы, голосовые, кружки или альбомы. Форматирование, спойлеры и премиум-эмодзи сохранятся."
        )
    else:
        if pages > 1:
            lines.append(f"Всего постов: {len(posts)}")
        for index, post in shown:
            snippet = post_text(post.kind, post.payload)
            line = f"{index}. {post_title(post.kind, post.payload)}"
            if snippet:
                line += f" — <i>{t.esc(t.cut(snippet, 50))}</i>"
            if post.buttons:
                line += f" · 🔘{count_buttons(post.buttons)}"
            if post.send_mode == "forward" and post.forward_from:
                line += " · ↪️"
            lines.append(line)
        if len(posts) > 1:
            order = "по очереди" if campaign.rotation == "sequential" else "в случайном порядке (без повтора подряд)"
            lines += ["", f"Посты публикуются {order}: по одному за каждый слот расписания."]
    rows: list[list[InlineKeyboardButton] | None] = []
    for index, post in shown:
        snippet = t.cut(post_text(post.kind, post.payload), 24)
        rows.append(
            [
                btn(
                    f"{index}. {post_title(post.kind, post.payload)} {snippet}".strip(),
                    Nav(to="post", id=post.id),
                )
            ]
        )
    rows.append(_pager(page, pages, lambda p: Nav(to="posts", id=campaign.id, page=p)))
    rows.append([btn("➕ Добавить посты", CampAct(a="add", id=campaign.id), GREEN)])
    if len(posts) > 1:
        label = "🔀 Порядок: по очереди" if campaign.rotation == "sequential" else "🔀 Порядок: случайно"
        rows.append([btn(label, CampAct(a="rot", id=campaign.id))])
    rows.append(back("camp", campaign.id))
    return "\n".join(lines), markup(*rows)


async def post_view(app: App, post_id: int, note: str | None = None) -> Screen | None:
    post = await app.repo.get_post(post_id)
    if post is None:
        return None
    posts = await app.repo.list_posts(post.campaign_id)
    campaign = await app.repo.get_campaign(post.campaign_id)
    index = [p.id for p in posts].index(post.id) + 1
    lines = [
        f"<b>Пост #{index} из {len(posts)}</b> · «{t.esc(campaign.name if campaign else '')}»",
        "",
        f"Тип: {post_title(post.kind, post.payload)}",
    ]
    snippet = post_text(post.kind, post.payload)
    if snippet:
        lines.append(f"Текст: <i>{t.esc(t.cut(snippet, 300))}</i>")
    emoji = custom_emoji_count(post.kind, post.payload)
    if emoji:
        lines.append(f"💎 Премиум-эмодзи: {emoji}")
    forward = post.send_mode == "forward" and bool(post.forward_from)
    if forward:
        lines.append("🔘 Кнопки: при пересылке остаются кнопки оригинального поста")
    elif not supports_buttons(post.kind):
        lines.append("🔘 Кнопки: Telegram не позволяет добавлять кнопки к альбомам")
    elif post.buttons:
        lines += ["🔘 Кнопки:", buttons_to_html(post.buttons)]
    else:
        lines.append("🔘 Кнопок нет")
    if post.forward_from:
        mode = "↪️ пересылка (с плашкой «Переслано из…»)" if forward else "📋 копия от имени бота"
        lines.append(f"Режим: {mode}")
    if note:
        lines += ["", note]

    rows: list[list[InlineKeyboardButton] | None] = []
    first = [btn("👁 Показать", PostAct(a="show", id=post.id))]
    if supports_buttons(post.kind) and not forward:
        first.append(btn("🔘 Кнопки", PostAct(a="btn", id=post.id)))
    rows.append(first)
    rows.append([btn("🔄 Заменить содержимое", PostAct(a="replace", id=post.id))])
    if post.forward_from:
        rows.append(
            [
                btn(
                    "📋 Сделать копией" if forward else "↪️ Публиковать пересылкой",
                    PostAct(a="mode", id=post.id),
                )
            ]
        )
    if len(posts) > 1:
        rows.append([btn("⬆️ Выше", PostAct(a="up", id=post.id)), btn("⬇️ Ниже", PostAct(a="down", id=post.id))])
    rows.append([btn("🗑 Удалить пост", PostAct(a="del", id=post.id), RED)])
    rows.append([btn("« К постам", Nav(to="posts", id=post.campaign_id, page=(index - 1) // POSTS_PAGE_SIZE))])
    return "\n".join(lines), markup(*rows)


async def post_delete_confirm(app: App, post_id: int) -> Screen | None:
    post = await app.repo.get_post(post_id)
    if post is None:
        return None
    text = f"🗑 Удалить пост «{post_title(post.kind, post.payload)}»?"
    keyboard = markup([btn("🗑 Да, удалить", PostAct(a="del_ok", id=post.id), RED)], back("post", post.id, "✖️ Отмена"))
    return text, keyboard


# ---------------------------------------------------------------------- расписание


async def schedule_view(app: App, campaign_id: int, note: str | None = None) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    tz, now = app.settings.tz, _now(app)
    jitter = su.effective_jitter(campaign.times, campaign.jitter_min)
    if not campaign.jitter_min:
        jitter_text = "нет"
    elif jitter != campaign.jitter_min:
        jitter_text = f"±{campaign.jitter_min} мин (фактически ±{jitter}: слоты стоят близко)"
    else:
        jitter_text = f"±{jitter} мин"
    lines = [
        f"<b>⏰ Расписание · «{t.esc(campaign.name)}»</b>",
        "",
        f"🕐 Время: {t.times_label(campaign.times)}",
        f"📅 Дни: {t.weekdays_label(campaign.weekdays)}",
        f"🎲 Разброс: {jitter_text}",
        f"📆 Период: {t.period_label(campaign.start_date, campaign.end_date)}",
        f"🌍 Часовой пояс: {t.tz_label(app.settings.timezone, tz, now)}",
        "",
        "Разброс сдвигает каждую публикацию на случайные ± минуты, чтобы посты выглядели живыми. "
        "Период — даты, в которые рассылка работает (один день + одно время = разовый пост).",
    ]
    if campaign.end_date and su.schedule_ended(spec_of(campaign), tz, now):
        lines += ["", "⚠️ Период уже закончился — поменяйте дату окончания."]
    if note:
        lines += ["", note]
    cid = campaign.id
    selected = set(campaign.weekdays or [])
    day_buttons = [
        btn(("✅" if day in selected else "▫️") + t.WEEKDAYS_SHORT[day], SchedAct(a="wd", id=cid, v=day))
        for day in range(7)
    ]
    start = t.fmt_date(campaign.start_date)[:5] if campaign.start_date else "—"
    end = t.fmt_date(campaign.end_date)[:5] if campaign.end_date else "—"
    rows = [
        [
            btn("🕐 Задать время", SchedAct(a="times", id=cid), BLUE),
            btn("🔢 N раз в день", SchedAct(a="count", id=cid)),
        ],
        day_buttons[:4],
        day_buttons[4:],
        [
            btn("Все дни", SchedAct(a="wds", id=cid, v=0)),
            btn("Будни", SchedAct(a="wds", id=cid, v=1)),
            btn("Выходные", SchedAct(a="wds", id=cid, v=2)),
        ],
        [
            btn(
                f"🎲 Разброс: {'±' + str(campaign.jitter_min) + ' мин' if campaign.jitter_min else 'нет'}",
                SchedAct(a="jit", id=cid),
            )
        ],
        [
            btn(f"📆 Начало: {start}", SchedAct(a="start", id=cid)),
            btn(f"📆 Конец: {end}", SchedAct(a="end", id=cid)),
        ],
        back("camp", cid),
    ]
    return "\n".join(lines), markup(*rows)


async def count_picker(app: App, campaign_id: int) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    text = (
        f"<b>🔢 Сколько раз в день публиковать?</b> · «{t.esc(campaign.name)}»\n\n"
        "Дальше выберете промежуток времени, и бот сам равномерно распределит публикации."
    )
    buttons = [btn(str(n), SchedAct(a="n", id=campaign.id, v=n)) for n in COUNT_CHOICES]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([btn("✏️ Другое число", SchedAct(a="ncustom", id=campaign.id))])
    rows.append(back("sched", campaign.id))
    return text, markup(*rows)


async def window_picker(app: App, campaign_id: int, count: int) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    lines = [f"<b>🔢 {count} {t.plural(count, 'раз', 'раза', 'раз')} в день.</b> В какие часы?", ""]
    rows: list[list[InlineKeyboardButton] | None] = []
    for index, window in enumerate(WINDOWS):
        try:
            times = su.spread_times(count, *window)
        except su.ScheduleError:
            continue
        preview = ", ".join(times[:6]) + (" …" if len(times) > 6 else "")
        lines.append(f"• <b>{window_label(window)}</b>: {preview}")
        rows.append([btn(f"🕘 {window_label(window)}", SchedAct(a="w", id=campaign.id, v=count, w=index))])
    rows.append([btn("✏️ Своё окно", SchedAct(a="wcustom", id=campaign.id, v=count))])
    rows.append([btn("« Назад", SchedAct(a="count", id=campaign.id))])
    return "\n".join(lines), markup(*rows)


# --------------------------------------------------------------------------- опции


async def options_view(app: App, campaign_id: int, note: str | None = None) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    chat = await app.repo.get_chat(campaign.chat_id) if campaign.chat_id else None
    lines = [
        f"<b>⚙️ Опции · «{t.esc(campaign.name)}»</b>",
        "",
        "🔕 <b>Без звука</b> — участники не получат уведомление о посте.",
        "🔒 <b>Защита</b> — пост нельзя переслать и сохранить.",
        "📌 <b>Закреплять</b> — новый пост закрепляется, прошлый открепляется.",
        "🗑 <b>Удалять прошлый</b> — после публикации бот удаляет предыдущий пост этой рассылки "
        "(Telegram разрешает удалять только сообщения моложе 48 часов).",
    ]
    if chat is not None and chat.is_forum:
        lines.append("🧵 <b>Тема</b> — в какую тему форума публиковать (по умолчанию «General»).")
    if chat is not None and campaign.pin and not chat.can_pin:
        lines += ["", "⚠️ У бота нет права закреплять сообщения в этом чате."]
    if campaign.delete_prev and su.max_gap_hours(campaign.times, campaign.weekdays) > 48:
        lines += ["", "⚠️ Между публикациями больше 48 часов — удалить прошлый пост не получится."]
    if note:
        lines += ["", note]

    def mark(value: bool) -> str:
        return "✅" if value else "❌"

    cid = campaign.id
    rows: list[list[InlineKeyboardButton] | None] = [
        [btn(f"🔕 Без звука: {mark(campaign.silent)}", OptAct(a="silent", id=cid))],
        [btn(f"🔒 Защита: {mark(campaign.protect)}", OptAct(a="protect", id=cid))],
        [btn(f"📌 Закреплять: {mark(campaign.pin)}", OptAct(a="pin", id=cid))],
        [btn(f"🗑 Удалять прошлый: {mark(campaign.delete_prev)}", OptAct(a="delprev", id=cid))],
    ]
    if chat is not None and chat.is_forum:
        topic = f"#{campaign.thread_id}" if campaign.thread_id else "General"
        rows.append([btn(f"🧵 Тема: {topic}", OptAct(a="thread", id=cid))])
    rows.append(back("camp", cid))
    return "\n".join(lines), markup(*rows)


# ------------------------------------------------------------------------ черновики


async def drafts_list(app: App, page: int = 0) -> Screen:
    drafts = await app.repo.list_drafts()
    counts = await app.repo.post_counts(d.id for d in drafts)
    linked = await app.repo.linked_counts()
    pages, page = _pages(len(drafts), page)
    lines = [
        "<b>📝 Черновики</b>",
        "",
        "Черновик — заготовка рассылки: посты, расписание и опции. Его можно применить к любым чатам "
        "в пару нажатий, а после правок — обновить везде сразу.",
    ]
    if not drafts:
        lines += [
            "",
            "Черновиков пока нет. Создайте новый или сохраните любую рассылку кнопкой «💾 В черновики».",
        ]
    rows: list[list[InlineKeyboardButton] | None] = []
    for draft in drafts[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        label = f"📝 {t.cut(draft.name, 28)} · {counts.get(draft.id, 0)} пост. · {t.times_short(draft.times)}"
        if linked.get(draft.id):
            label += f" · в {linked[draft.id]} чат."
        rows.append([btn(label, Nav(to="camp", id=draft.id))])
    rows.append(_pager(page, pages, lambda p: Nav(to="drafts", page=p)))
    rows.append([btn("➕ Новый черновик", CampAct(a="newdraft", id=0), GREEN)])
    rows.append(back("main", text="« Меню"))
    return "\n".join(lines), markup(*rows)


async def picker_view(app: App, data: dict[str, Any]) -> Screen | None:
    """Выбор чатов для применения черновика (mode=draft) или копирования рассылки (mode=copy)."""
    source = await app.repo.get_campaign(int(data["src"]))
    if source is None:
        return None
    mode = data["mode"]
    chats = [
        c for c in await app.repo.list_chats(statuses=("active",)) if not (mode == "copy" and c.id == source.chat_id)
    ]
    selected = {int(x) for x in data.get("sel", [])} & {c.id for c in chats}
    linked = {c.chat_id for c in await app.repo.linked_campaigns(source.id)} if mode == "draft" else set()
    pages, page = _pages(len(chats), int(data.get("page", 0)))

    if mode == "draft":
        lines = [f"<b>📤 Применить черновик «{t.esc(source.name)}»</b>", ""]
    else:
        lines = [f"<b>📋 Копировать «{t.esc(source.name)}» в другие чаты</b>", ""]
    if chats:
        lines.append(
            "Отметьте чаты и нажмите кнопку внизу. В каждом появится рассылка с этими постами, расписанием и опциями."
        )
        if linked:
            lines.append("🔄 — черновик уже применён в этом чате: рассылка обновится, а не задублируется.")
        lines += ["", f"Выбрано: <b>{len(selected)}</b> из {len(chats)}"]
    else:
        lines.append("Нет подходящих чатов. Добавьте бота администратором в другие чаты — они появятся здесь.")

    rows: list[list[InlineKeyboardButton] | None] = []
    for chat in chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        mark = "☑️" if chat.id in selected else "⬜"
        suffix = " 🔄" if chat.id in linked else ""
        rows.append([btn(f"{mark} {t.cut(chat.title, 38)}{suffix}", PickAct(a="t", s=source.id, v=chat.id))])
    rows.append(_pager(page, pages, lambda p: PickAct(a="pg", s=source.id, v=p)))
    if chats:
        rows.append(
            [btn("☑️ Выбрать все", PickAct(a="all", s=source.id)), btn("⬜ Снять все", PickAct(a="none", s=source.id))]
        )
        rows.append([btn("🚀 Применить и запустить", PickAct(a="go", s=source.id, v=1), GREEN)])
        rows.append([btn("💾 Применить на паузе", PickAct(a="go", s=source.id, v=0))])
    rows.append(back("camp", source.id, "✖️ Отмена"))
    return "\n".join(lines), markup(*rows)


async def apply_summary(app: App, source_id: int, results: list[tuple[int, bool]], warnings: list[str]) -> Screen:
    source = await app.repo.get_campaign(source_id)
    chats = {c.id: c for c in await app.repo.list_chats()}
    tz, now = app.settings.tz, _now(app)
    created = sum(1 for _, is_new in results if is_new)
    lines = ["<b>✅ Готово</b>", "", f"Создано рассылок: {created} · обновлено: {len(results) - created}", ""]
    for campaign_id, _ in results[:30]:
        campaign = await app.repo.get_campaign(campaign_id)
        if campaign is None or campaign.chat_id not in chats:
            continue
        if campaign.is_active and campaign.next_run_ts:
            status = f"🟢 ближайшая {t.fmt_ts(campaign.next_run_ts, tz, now)}"
        elif campaign.is_active:
            status = "🟢 запущена"
        else:
            status = "⏸ на паузе"
        lines.append(f"• {t.esc(chats[campaign.chat_id].title, 40)} — {status}")
    if len(results) > 30:
        lines.append(f"…и ещё {len(results) - 30}")
    if warnings:
        lines += ["", *(w if len(w) < 1500 else w[:1500] + "…" for w in warnings)]
    back_row = (
        back("camp", source.id, "« К черновику" if source and source.is_draft else "« К рассылке") if source else None
    )
    return "\n".join(lines), markup(back_row, [btn("💬 Мои чаты", Nav(to="chats"))])


# ------------------------------------------------------------------------ настройки


async def settings_view(app: App, note: str | None = None) -> Screen:
    tz, now = app.settings.tz, _now(app)
    local_now = datetime.fromtimestamp(now, tz).strftime("%H:%M")
    paused = app.settings.paused_all
    lines = [
        "<b>⚙️ Настройки</b>",
        "",
        f"🌍 Часовой пояс: <b>{t.tz_label(app.settings.timezone, tz, now)}</b>, сейчас {local_now}",
        "⏯ Рассылки: " + ("⏸ <b>все на паузе</b>" if paused else "▶️ работают"),
        "🔔 Уведомления об ошибках: " + ("вкл" if app.settings.notify_errors else "выкл"),
        "",
        "💾 Резервная копия — бот пришлёт файл базы данных со всеми чатами, рассылками и черновиками.",
    ]
    if note:
        lines += ["", note]
    rows = [
        [btn("🌍 Часовой пояс", Nav(to="tz"))],
        [
            btn("▶️ Возобновить все рассылки", SetAct(a="pause"), GREEN)
            if paused
            else btn("⏸ Поставить все на паузу", SetAct(a="pause"), RED)
        ],
        [btn(f"🔔 Уведомления: {'вкл' if app.settings.notify_errors else 'выкл'}", SetAct(a="notify"))],
        [btn("💾 Резервная копия", SetAct(a="backup"))],
        back("main", text="« Меню"),
    ]
    return "\n".join(lines), markup(*rows)


async def timezone_view(app: App) -> Screen:
    now = _now(app)
    lines = [
        "<b>🌍 Часовой пояс</b>",
        "",
        f"Сейчас: <b>{t.tz_label(app.settings.timezone, app.settings.tz, now)}</b>",
        "Все времена в расписаниях считаются в этом поясе.",
    ]
    buttons = []
    for index, (city, name) in enumerate(TIMEZONES):
        mark = "✅ " if name == app.settings.timezone else ""
        buttons.append(btn(f"{mark}{city} ({t.utc_offset(ZoneInfo(name), now)})", SetAct(a="tz", v=index)))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("✏️ Ввести вручную", SetAct(a="tzin"))])
    rows.append(back("settings"))
    return "\n".join(lines), markup(*rows)


async def upcoming_view(app: App) -> Screen:
    tz, now = app.settings.tz, _now(app)
    items = [(c, chat) for c, chat in await app.repo.active_campaigns_with_chats() if c.next_run_ts][:10]
    lines = ["<b>📅 Ближайшие публикации</b>", ""]
    if app.settings.paused_all:
        lines.append("⏸ Все рассылки на паузе — публикаций не будет, пока не возобновите их в настройках.")
    elif not items:
        lines.append("Запланированных публикаций нет. Запустите рассылку в любом чате.")
    else:
        for campaign, chat in items:
            lines.append(
                f"• <b>{t.fmt_ts(campaign.next_run_ts, tz, now)}</b> — "
                f"{t.esc(chat.title, 30)} · «{t.esc(campaign.name, 30)}»"
            )
    rows: list[list[InlineKeyboardButton] | None] = [
        [btn(f"{t.fmt_ts(c.next_run_ts, tz, now)} · {t.cut(chat.title, 28)}", Nav(to="camp", id=c.id))]
        for c, chat in items
    ]
    rows.append([btn("🔄 Обновить", Nav(to="upcoming")), btn("« Меню", Nav(to="main"))])
    return "\n".join(lines), markup(*rows)


def help_view() -> Screen:
    text = (
        "<b>❓ Как пользоваться</b>\n\n"
        "<b>1. Добавьте бота в чат.</b> Кнопки «➕ Добавить группу/канал» внизу откроют выбор чата — "
        "Telegram сам сделает бота администратором. Можно и вручную: настройки чата → Администраторы. "
        "Для канала нужно право «Публикация сообщений», для закрепления — «Закрепление» "
        "(в канале — «Редактирование»).\n\n"
        "<b>2. Создайте рассылку.</b> Мои чаты → чат → «➕ Новая рассылка». Добавьте посты — просто пришлите "
        "боту сообщения (или перешлите из канала). Затем задайте время и нажмите «▶️ Запустить».\n\n"
        "<b>3. Черновики.</b> «💾 В черновики» сохраняет посты, расписание и опции. Потом «📤 Применить к чатам» "
        "— и та же рассылка появится в любых чатах. Изменили черновик — «🔄 Обновить во всех».\n\n"
        "<b>Кнопки под постом</b> (одна строка — один ряд):\n"
        "<code>Текст - https://ссылка</code>\n"
        "<code>Кнопка 1 - https://a.ru | Кнопка 2 - t.me/channel</code>\n"
        "<code>Купить - https://shop.ru - green</code> (green / red / blue)\n"
        "<code>Промокод - copy:SALE2026</code> (копирует текст)\n"
        "Премиум-эмодзи в начале текста кнопки станет её иконкой.\n\n"
        "<b>Премиум-эмодзи.</b> В группах работают, если у владельца бота (аккаунта, создавшего его в @BotFather) "
        "есть Telegram Premium. В каналах — только если боту куплен юзернейм на Fragment. Для каналов есть обход: "
        "перешлите пост из своего канала и включите у него режим «↪️ Пересылкой».\n\n"
        "<b>Ограничения Telegram.</b> К альбомам нельзя добавить кнопки. Удалить прошлый пост можно, только если "
        "ему меньше 48 часов. Плашку «бот закрепил сообщение» бот удалить не может. "
        "Подпись к медиа — до 1024 символов.\n\n"
        "Команды: /start — меню, /chats — чаты, /drafts — черновики, /settings — настройки, /cancel — отменить ввод."
    )
    return text, markup(back("main", text="« Меню"))


# --------------------------------------------------------------------------- навигация


async def resolve(app: App, nav: Nav) -> Screen | None:
    match nav.to:
        case "main":
            return await main_menu(app)
        case "chats":
            return await chats_list(app, nav.page)
        case "chat":
            return await chat_view(app, nav.id)
        case "camp":
            return await campaign_view(app, nav.id)
        case "posts":
            return await posts_view(app, nav.id, nav.page)
        case "post":
            return await post_view(app, nav.id)
        case "sched":
            return await schedule_view(app, nav.id)
        case "opts":
            return await options_view(app, nav.id)
        case "drafts":
            return await drafts_list(app, nav.page)
        case "settings":
            return await settings_view(app)
        case "tz":
            return await timezone_view(app)
        case "upcoming":
            return await upcoming_view(app)
        case "help":
            return help_view()
    return await main_menu(app)
