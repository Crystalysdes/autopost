"""Экраны интерфейса. Каждая функция возвращает (текст в HTML, клавиатура) или None,
если объект уже удалён."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.app import App
from bot.db.models import Campaign, Post, now_ts
from bot.db.repo import DraftApplied, Target
from bot.services import schedule_utils as su
from bot.services.buttons import buttons_to_html, count_buttons, has_icons
from bot.services.campaigns import target_problem
from bot.services.content import custom_emoji_count, post_text, post_title, supports_buttons
from bot.services.scheduler import predict_runs, spec_of
from bot.ui import texts as t
from bot.ui.callbacks import (
    ApplyDraft,
    CampAct,
    ChatAct,
    Guard,
    LibAct,
    Nav,
    OptAct,
    PickAct,
    PostAct,
    SchedAct,
    SetAct,
    TargetAct,
)
from bot.ui.guard import protection_label
from bot.ui.keyboards import BLUE, GREEN, RED, add_channel_link, add_group_link, back, btn, markup, url_btn

Screen = tuple[str, InlineKeyboardMarkup]

PAGE_SIZE = 8
POSTS_PAGE_SIZE = 10
TARGETS_PAGE_SIZE = 10
MAX_LIST_BUTTONS = 40  # длинные списки режем, чтобы не упереться в лимиты Telegram
MAX_PROBLEM_LINES = 8
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


def _pages(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int]:
    pages = max(1, math.ceil(total / size))
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


def _next_jitter(current: int) -> int:
    steps = su.JITTER_STEPS
    index = steps.index(current) if current in steps else 0
    return steps[(index + 1) % len(steps)]


def _chats_word(count: int) -> str:
    return f"{count} {t.plural(count, 'чат', 'чата', 'чатов')}"


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
    upcoming = [c for c in await app.repo.active_campaigns() if c.next_run_ts]

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
    spam_today, sub_today = await app.repo.moderation_counts(t.day_start_ts(tz, now))
    if spam_today or sub_today:
        lines.append(f"🛡 Сегодня удалено: спам {spam_today} · без подписки {sub_today}")
    if upcoming and not app.settings.paused_all:
        campaign = upcoming[0]
        lines.append(f"⏭ Следующая: {t.fmt_ts(campaign.next_run_ts, tz, now)} · «{t.esc(campaign.name, 30)}»")
    if app.settings.paused_all:
        lines += ["", "⏸ <b>Все рассылки на паузе.</b> Возобновить — в настройках."]
    if not chats:
        lines += [
            "",
            "Чтобы начать, добавьте бота администратором в группу или канал — кнопками внизу экрана "
            "или в разделе «Мои чаты». Чат сразу появится здесь.",
        ]
    keyboard = markup(
        [btn("💬 Мои чаты", Nav(to="chats"), BLUE), btn("📬 Рассылки", Nav(to="camps"), BLUE)],
        [btn("🗂 Мои посты", Nav(to="lib")), btn("📝 Черновики", Nav(to="drafts"))],
        [btn("📅 Ближайшие публикации", Nav(to="upcoming"))],
        [btn("⚙️ Настройки", Nav(to="settings")), btn("❓ Помощь", Nav(to="help"))],
    )
    return "\n".join(lines), keyboard


# --------------------------------------------------------------------------- чаты


def chat_icon(chat: Any, stats: tuple[int, int] | None) -> str:
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
    links = await app.repo.chat_links(chat.id)
    paused_links = sum(1 for link in links.values() if link.paused)
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
            "🚫 <b>Бота нет в этом чате</b> — его удалили или лишили прав, рассылки пропускают этот чат. "
            "Добавьте бота снова администратором — публикации возобновятся сами."
        )
    else:
        post_mark = "✅" if chat.can_post else "❌"
        pin_mark = "✅" if chat.can_pin else "❌"
        delete_mark = "✅" if chat.can_delete else "❌"
        lines.append(f"Права бота: {post_mark} публикация · {pin_mark} закрепление · {delete_mark} удаление")
        if not chat.can_post:
            lines.append("⚠️ Бот не может публиковать здесь — выдайте ему право «Публикация сообщений».")
        if chat.type != "channel" and chat.can_delete is False:
            lines.append("⚠️ Без права «Удаление сообщений» защита чата (антиспам, подписка) не работает.")
    lines.append("")
    if campaigns:
        lines.append(f"📬 Рассылок с этим чатом: <b>{len(campaigns)}</b>")
        if paused_links:
            lines.append(f"⏸ В {paused_links} из них публикации сюда на паузе после ошибок.")
        if len(campaigns) > MAX_LIST_BUTTONS:
            lines.append(f"Показаны первые {MAX_LIST_BUTTONS}.")
    else:
        lines.append(
            "Этот чат пока не отмечен ни в одной рассылке. Создайте новую, примените черновик "
            "или отметьте чат в любой рассылке кнопкой «💬 Чаты»."
        )

    rows: list[list[InlineKeyboardButton] | None] = []
    if chat.status == "pending":
        rows.append(
            [
                btn("✅ Принять", ChatAct(a="accept", id=chat.id), GREEN),
                btn("🚪 Выйти", ChatAct(a="leave", id=chat.id), RED),
            ]
        )
    for campaign in campaigns[:MAX_LIST_BUTTONS]:
        link = links.get(campaign.id)
        if not campaign.is_active:
            icon = "⏸"
        elif link is not None and link.paused:
            icon = "⚠️"
        else:
            icon = "🟢"
        rows.append(
            [
                btn(
                    f"{icon} {t.cut(campaign.name, 32)} · {t.times_short(campaign.times)}",
                    Nav(to="camp", id=campaign.id, f=chat.id),
                )
            ]
        )
    rows.append(
        [
            btn("➕ Новая рассылка", ChatAct(a="new", id=chat.id), BLUE),
            btn("📝 Из черновика", ChatAct(a="fromdraft", id=chat.id)),
        ]
    )
    if chat.status == "active" and paused_links:
        rows.append([btn(f"▶️ Возобновить публикации сюда ({paused_links})", ChatAct(a="unpause", id=chat.id), GREEN)])
    stopped = sum(1 for c in campaigns if not c.is_active)
    if chat.status == "active" and stopped:
        rows.append([btn(f"▶️ Запустить остановленные ({stopped})", ChatAct(a="resume", id=chat.id))])
    if chat.type != "channel":
        rows.append([btn(protection_label(chat, app.settings.sub_channels), Guard(a="chat", id=chat.id), BLUE)])
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
    text = f"🗑 Удалить «<b>{t.esc(chat.title)}</b>» из бота?\n\n"
    if count:
        text += (
            f"Чат будет убран из рассылок: {count}. Сами рассылки, их посты и черновики останутся "
            "и продолжат публиковать в другие чаты.\n\n"
        )
    text += (
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
            "Выберите черновик. Этот чат добавится в рассылку, созданную из черновика (если её ещё нет — "
            "она появится остановленной: проверьте и запустите). Посты, расписание и опции рассылки "
            "возьмутся из черновика."
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


def campaign_icon(campaign: Campaign, targets: Sequence[Target]) -> str:
    if not campaign.is_active:
        return "⏸"
    if not any(target.deliverable for target in targets) or any(target_problem(target) for target in targets):
        return "⚠️"
    return "🟢"


async def campaigns_list(app: App, page: int = 0) -> Screen:
    campaigns = await app.repo.list_campaigns()
    pages, page = _pages(len(campaigns), page)
    shown = campaigns[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    targets = await app.repo.targets_of(c.id for c in shown)
    lines = ["<b>📬 Рассылки</b>", ""]
    if campaigns:
        lines.append(
            "Рассылка публикует свои посты по своему расписанию во все отмеченные в ней чаты.\n\n"
            "🟢 работает · ⏸ остановлена · ⚠️ требует внимания"
        )
    else:
        lines.append(
            "Рассылок пока нет. Рассылка публикует посты по расписанию во все отмеченные в ней чаты — "
            "создайте первую: отметьте чаты, добавьте посты и задайте время."
        )
    rows: list[list[InlineKeyboardButton] | None] = []
    for campaign in shown:
        items = targets.get(campaign.id, [])
        label = (
            f"{campaign_icon(campaign, items)} {t.cut(campaign.name, 26)} · {_chats_word(len(items))} · "
            f"{t.times_short(campaign.times)}"
        )
        rows.append([btn(label, Nav(to="camp", id=campaign.id, f=-1))])
    rows.append(_pager(page, pages, lambda p: Nav(to="camps", page=p)))
    rows.append([btn("➕ Новая рассылка", CampAct(a="new", id=0), GREEN)])
    rows.append(back("main", text="« Меню"))
    return "\n".join(lines), markup(*rows)


def _status_line(campaign: Campaign, targets: Sequence[Target], paused_all: bool) -> str:
    if not campaign.is_active:
        return "⏸ остановлена"
    if paused_all:
        return "⏸ все рассылки на паузе (см. настройки)"
    if not any(target.deliverable for target in targets):
        return "⚠️ запущена, но публиковать некуда — проверьте «💬 Чаты»"
    if campaign.next_run_ts is None:
        return "⚠️ включена, но ближайших публикаций нет — проверьте время, дни и период"
    return "🟢 работает"


def _uses_premium(posts: list[Post]) -> bool:
    return any(
        (custom_emoji_count(p.kind, p.payload) and not (p.send_mode == "forward" and p.forward_from))
        or has_icons(p.buttons)
        for p in posts
    )


def target_lines(targets: Sequence[Target]) -> list[str]:
    """Что не так с чатами рассылки: бота нет, нет прав, пауза после ошибок, свежие ошибки."""
    lines = []
    for target in targets:
        title, link = t.esc(target.chat.title, 30), target.link
        problem = target_problem(target)
        if link.paused:
            reason = f": <i>{t.esc(link.last_error, 150)}</i>" if link.last_error else ""
            lines.append(f"⏸ «{title}» — пауза после ошибок{reason}")
        elif problem:
            lines.append(f"⚠️ «{title}» — {problem}")
        elif link.fail_count and link.last_error:
            lines.append(f"⚠️ «{title}» — ошибок подряд: {link.fail_count}, <i>{t.esc(link.last_error, 150)}</i>")
        elif link.last_error:
            lines.append(f"ℹ️ «{title}» — {t.esc(link.last_error, 150)}")
    if len(lines) > MAX_PROBLEM_LINES:
        lines = [*lines[:MAX_PROBLEM_LINES], f"…и ещё {len(lines) - MAX_PROBLEM_LINES}"]
    return lines


def campaign_warnings(campaign: Campaign, targets: Sequence[Target], posts: list[Post], tz: Any, now: int) -> list[str]:
    warnings = []
    if any(target.chat.type == "channel" for target in targets) and _uses_premium(posts):
        warnings.append(
            "💎 В постах есть премиум-эмодзи. В каналах бот показывает их, только если ему куплен юзернейм "
            "на Fragment — иначе они станут обычными. Альтернатива: переслать пост из своего канала "
            "и включить у него режим «↪️ Пересылкой»."
        )
    if campaign.delete_prev and su.max_gap_hours(campaign.times, campaign.weekdays) > 48:
        warnings.append("🗑 Между публикациями больше 48 часов — Telegram не даст удалить прошлый пост.")
    no_pin = [target.chat.title for target in targets if target.chat.status == "active" and not target.chat.can_pin]
    if campaign.pin and no_pin:
        warnings.append(f"📌 Нет права закреплять сообщения в: {t.names_label(no_pin, 5)}")
    if campaign.end_date and su.schedule_ended(spec_of(campaign), tz, now):
        warnings.append("📆 Период публикаций уже закончился.")
    return warnings


async def campaign_view(
    app: App, campaign_id: int, note: str | None = None, *, user_id: int | None = None
) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    posts = await app.repo.list_posts(campaign.id)
    targets = [] if campaign.is_draft else await app.repo.campaign_targets(campaign.id)
    tz, now = app.settings.tz, _now(app)
    lines: list[str] = []

    linked = await app.repo.linked_campaigns(campaign.id) if campaign.is_draft else []
    if campaign.is_draft:
        lines.append(f"<b>📝 Черновик «{t.esc(campaign.name)}»</b>")
        if linked:
            linked_targets = await app.repo.targets_of(c.id for c in linked)
            parts = [f"«{t.esc(c.name, 30)}» ({_chats_word(len(linked_targets[c.id]))})" for c in linked[:3]]
            more = f" и ещё {len(linked) - 3}" if len(linked) > 3 else ""
            lines.append("📤 Применён: " + ", ".join(parts) + more)
        else:
            lines.append("Ещё не применён — «📤 Применить к чатам» создаст из него рассылку.")
    else:
        lines.append(f"<b>📬 Рассылка «{t.esc(campaign.name)}»</b>")
        if targets:
            lines.append(f"💬 Чаты ({len(targets)}): {t.names_label([x.chat.title for x in targets])}")
        else:
            lines.append("💬 Чаты: <b>не выбраны</b>")
        lines.append(f"Статус: {_status_line(campaign, targets, app.settings.paused_all)}")
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
    lines.append("⚙️ " + t.options_label(campaign.silent, campaign.protect, campaign.pin, campaign.delete_prev))

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
        sent = sum(x.link.sent_count or 0 for x in targets)
        last = max((x.link.last_sent_ts for x in targets if x.link.last_sent_ts), default=None)
        if sent or targets:
            stats = f"📊 Опубликовано: {sent}"
            if last:
                stats += f" · последний {t.fmt_ts(last, tz, now)}"
            lines.append(stats)
        if campaign.last_error:
            lines.append(f"⚠️ <i>{t.esc(campaign.last_error, 300)}</i>")
        problems = target_lines(targets)
        if problems:
            lines += ["", *problems]
            if any(x.link.paused for x in targets):
                lines.append("Возобновить публикации в чат — в «💬 Чаты».")

    missing = []
    if not campaign.is_draft and not targets:
        missing.append("отметьте чаты")
    if not posts:
        missing.append("добавьте посты")
    if not campaign.times:
        missing.append("задайте время")
    if not campaign.weekdays:
        missing.append("выберите дни")
    if missing and not campaign.is_active:
        lines += ["", "👉 Чтобы запустить: " + ", ".join(missing) + "."]
    warnings = campaign_warnings(campaign, targets, posts, tz, now)
    if warnings:
        lines += ["", *warnings]
    if note:
        lines += ["", note]

    cid = campaign.id
    rows: list[list[InlineKeyboardButton] | None] = []
    if campaign.is_draft:
        rows += [
            [
                btn(f"📝 Посты ({len(posts)})", Nav(to="posts", id=cid)),
                btn("⏰ Расписание", Nav(to="sched", id=cid)),
            ],
            [btn("⚙️ Опции", Nav(to="opts", id=cid)), btn("👁 Предпросмотр", CampAct(a="preview", id=cid))],
            [btn("📤 Применить к чатам", CampAct(a="apply", id=cid), GREEN)],
        ]
        if linked:
            rows.append([btn(f"🔄 Обновить рассылки ({len(linked)})", CampAct(a="sync", id=cid))])
        rows += [
            [
                btn("✏️ Переименовать", CampAct(a="rename", id=cid)),
                btn("🗑 Удалить", CampAct(a="del", id=cid), RED),
            ],
            back("drafts", text="« Черновики"),
        ]
        return "\n".join(lines), markup(*rows)

    toggle = (
        btn("⏸ Остановить", CampAct(a="off", id=cid), RED)
        if campaign.is_active
        else btn("▶️ Запустить", CampAct(a="on", id=cid), GREEN)
    )
    origin = app.origin(user_id, cid)
    if origin and await app.repo.get_chat(origin) is not None:
        back_row = back("chat", origin, "« К чату")
    else:
        back_row = back("camps", text="« Рассылки")
    rows += [
        [toggle],
        [btn(f"💬 Чаты ({len(targets)})", Nav(to="tgt", id=cid), BLUE)],
        [
            btn(f"📝 Посты ({len(posts)})", Nav(to="posts", id=cid)),
            btn("⏰ Расписание", Nav(to="sched", id=cid)),
        ],
        [btn("⚙️ Опции", Nav(to="opts", id=cid)), btn("👁 Предпросмотр", CampAct(a="preview", id=cid))],
        [btn("🚀 Отправить сейчас", CampAct(a="send", id=cid)), btn("💾 В черновики", CampAct(a="todraft", id=cid))],
        [
            btn("✏️ Переименовать", CampAct(a="rename", id=cid)),
            btn("🗑 Удалить", CampAct(a="del", id=cid), RED),
        ],
        back_row,
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
    else:
        count = len(await app.repo.campaign_targets(campaign.id))
        if count:
            text += f"\n\nПубликации прекратятся во всех её чатах ({count})."
    keyboard = markup(
        [btn("🗑 Да, удалить", CampAct(a="del_ok", id=campaign.id), RED)],
        back("camp", campaign.id, "✖️ Отмена"),
    )
    return text, keyboard


async def send_now_confirm(app: App, campaign_id: int) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None or campaign.is_draft:
        return None
    targets = await app.repo.campaign_targets(campaign.id)
    ready = [x.chat.title for x in targets if x.deliverable]
    posts = await app.repo.list_posts(campaign.id)
    text = f"🚀 Опубликовать следующий пост рассылки «<b>{t.esc(campaign.name)}</b>» прямо сейчас?\n\n"
    if ready:
        text += f"Чаты ({len(ready)}): {t.names_label(ready, 10)}\n"
        if len(targets) > len(ready):
            text += f"Пропущены — сейчас недоступны: {len(targets) - len(ready)}\n"
    else:
        text += "⚠️ Сейчас нет чатов, куда бот может публиковать.\n"
    text += "Расписание не изменится."
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
        "Черновик — заготовка: посты, расписание и опции. «📤 Применить к чатам» публикует его в нужных чатах, "
        "а если потом изменить черновик, «🔄 Обновить рассылки» перенесёт изменения в эту рассылку "
        "и во все, созданные из него."
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
    targets = await app.repo.targets_of(c.id for c in linked)
    items = []
    for campaign in linked[:20]:
        names = [x.chat.title for x in targets[campaign.id]]
        items.append(f"• «{t.esc(campaign.name, 40)}» — {t.names_label(names) if names else 'чаты не выбраны'}")
    text = (
        f"🔄 Обновить рассылки из черновика «<b>{t.esc(draft.name)}</b>»?\n\n"
        f"Посты, расписание и опции будут заменены в {len(linked)} "
        f"{t.plural(len(linked), 'рассылке', 'рассылках', 'рассылках')}:\n"
        + "\n".join(items)
        + ("\n…" if len(linked) > 20 else "")
        + "\n\nЧаты и статус (работает/остановлена) у рассылок останутся прежними."
    )
    keyboard = markup(
        [btn("🔄 Обновить", CampAct(a="sync_ok", id=draft.id), GREEN)],
        back("camp", draft.id, "✖️ Отмена"),
    )
    return text, keyboard


# -------------------------------------------------------------------- чаты рассылки


async def targets_view(app: App, campaign_id: int, page: int = 0, note: str | None = None) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None or campaign.is_draft:
        return None
    targets = await app.repo.campaign_targets(campaign.id)
    by_chat = {x.chat.id: x for x in targets}
    # Отметить можно работающий чат; уже отмеченные показываем всегда, чтобы их можно было снять
    chats = [c for c in await app.repo.list_chats() if c.status == "active" or c.id in by_chat]
    pages, page = _pages(len(chats), page, TARGETS_PAGE_SIZE)
    shown = chats[page * TARGETS_PAGE_SIZE : (page + 1) * TARGETS_PAGE_SIZE]

    lines = [f"<b>💬 Чаты рассылки «{t.esc(campaign.name)}»</b>", ""]
    if chats:
        lines.append(
            "Отметьте чаты, в которые публиковать. Посты, расписание и опции общие для всех отмеченных чатов. "
            "Изменения сохраняются сразу."
        )
        lines += ["", f"Выбрано: <b>{len(targets)}</b> из {len(chats)}"]
    else:
        lines.append(
            "Чатов пока нет. Добавьте бота администратором в группу или канал — чат появится здесь "
            "(кнопки «➕ Добавить…» внизу экрана или раздел «💬 Мои чаты»)."
        )
    problems = target_lines(targets)
    if problems:
        lines += ["", *problems]
    if campaign.is_active and not any(x.deliverable for x in targets):
        lines += ["", "⚠️ Рассылка запущена, но публиковать некуда — отметьте чат, где бот может публиковать."]
    if any(x.chat.is_forum for x in targets):
        lines += ["", "🧵 В форумах можно выбрать тему — у каждого чата она своя."]
    if note:
        lines += ["", note]

    cid = campaign.id
    rows: list[list[InlineKeyboardButton] | None] = []
    for chat in shown:
        target = by_chat.get(chat.id)
        mark = "☑️" if target else "⬜"
        marks = ""
        if chat.status == "left":
            marks += " 🚫"
        elif chat.status == "pending":
            marks += " ⏳"
        elif not chat.can_post:
            marks += " ⚠️"
        if target is not None and target.link.paused:
            marks += " ⏸"
        if target is not None and target.link.thread_id:
            marks += f" 🧵{target.link.thread_id}"
        type_icon = t.CHAT_ICONS.get(chat.type, "💬")
        rows.append(
            [
                btn(
                    f"{mark} {type_icon} {t.cut(chat.title, 34)}{marks}",
                    TargetAct(a="off" if target else "on", id=cid, v=chat.id, p=page),
                )
            ]
        )
    rows.append(_pager(page, pages, lambda p: TargetAct(a="pg", id=cid, v=p, p=p)))
    if chats:
        rows.append(
            [
                btn("☑️ Все", TargetAct(a="all", id=cid, p=page)),
                btn("⬜ Снять все", TargetAct(a="none", id=cid, p=page)),
            ]
        )
    for chat in shown:
        target = by_chat.get(chat.id)
        if target is None:
            continue
        if chat.is_forum:
            topic = f"#{target.link.thread_id}" if target.link.thread_id else "General"
            rows.append(
                [btn(f"🧵 Тема в «{t.cut(chat.title, 22)}»: {topic}", TargetAct(a="thr", id=cid, v=chat.id, p=page))]
            )
        if target.link.paused:
            rows.append(
                [
                    btn(
                        f"▶️ Возобновить в «{t.cut(chat.title, 24)}»",
                        TargetAct(a="resume", id=cid, v=chat.id, p=page),
                        GREEN,
                    )
                ]
            )
    rows.append(back("camp", cid, "« К рассылке"))
    return "\n".join(lines), markup(*rows)


# --------------------------------------------------------------------------- посты


async def posts_view(app: App, campaign_id: int, page: int = 0) -> Screen | None:
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None:
        return None
    posts = await app.repo.list_posts(campaign.id)
    pages, page = _pages(len(posts), page, POSTS_PAGE_SIZE)
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
            lines.append(f"{index}. {_post_line(post)}")
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
        target = "rot_rnd" if campaign.rotation == "sequential" else "rot_seq"
        rows.append([btn(label, CampAct(a=target, id=campaign.id))])
    rows.append(back("camp", campaign.id))
    return "\n".join(lines), markup(*rows)


def _post_line(post: Post, snippet_limit: int = 50) -> str:
    snippet = post_text(post.kind, post.payload)
    line = post_title(post.kind, post.payload)
    if snippet:
        line += f" — <i>{t.esc(t.cut(snippet, snippet_limit))}</i>"
    if post.buttons:
        line += f" · 🔘{count_buttons(post.buttons)}"
    if post.send_mode == "forward" and post.forward_from:
        line += " · ↪️"
    return line


def _owner_label(campaign: Campaign, limit: int = 30) -> str:
    return ("📝 " if campaign.is_draft else "📬 ") + t.cut(campaign.name, limit)


async def post_view(app: App, post_id: int, note: str | None = None, *, origin: int = 0) -> Screen | None:
    """origin — страница «Моих постов» + 1, если пост открыт оттуда (0 — из постов рассылки)."""
    post = await app.repo.get_post(post_id)
    if post is None:
        return None
    posts = await app.repo.list_posts(post.campaign_id)
    campaign = await app.repo.get_campaign(post.campaign_id)
    index = [p.id for p in posts].index(post.id) + 1
    owner = t.esc(_owner_label(campaign, 60)) if campaign else ""
    lines = [
        f"<b>Пост #{index} из {len(posts)}</b> · {owner}",
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

    pid, f = post.id, origin
    rows: list[list[InlineKeyboardButton] | None] = []
    first = [btn("👁 Показать", PostAct(a="show", id=pid, f=f))]
    if supports_buttons(post.kind) and not forward:
        first.append(btn("🔘 Кнопки", PostAct(a="btn", id=pid, f=f)))
    rows.append(first)
    rows.append([btn("🔄 Заменить содержимое", PostAct(a="replace", id=pid, f=f))])
    if post.forward_from:
        rows.append(
            [
                btn(
                    "📋 Сделать копией" if forward else "↪️ Публиковать пересылкой",
                    PostAct(a="copy" if forward else "fwd", id=pid, f=f),
                )
            ]
        )
    if len(posts) > 1:
        rows.append([btn("⬆️ Выше", PostAct(a="up", id=pid, f=f)), btn("⬇️ Ниже", PostAct(a="down", id=pid, f=f))])
    if origin:
        rows.append([btn("✨ Новая рассылка с этим постом", LibAct(a="new", id=pid, f=f), GREEN)])
    rows.append([btn("📋 Копировать в другую рассылку", LibAct(a="to", id=pid, f=f))])
    if origin and campaign is not None:
        rows.append([btn(f"{_owner_label(campaign, 40)} — открыть", Nav(to="camp", id=campaign.id, f=-1))])
    rows.append([btn("🗑 Удалить пост", PostAct(a="del", id=pid, f=f), RED)])
    if origin:
        rows.append([btn("« Мои посты", Nav(to="lib", page=origin - 1))])
    else:
        rows.append([btn("« К постам", Nav(to="posts", id=post.campaign_id, page=(index - 1) // POSTS_PAGE_SIZE))])
    return "\n".join(lines), markup(*rows)


async def post_delete_confirm(app: App, post_id: int, origin: int = 0) -> Screen | None:
    post = await app.repo.get_post(post_id)
    if post is None:
        return None
    text = f"🗑 Удалить пост «{post_title(post.kind, post.payload)}»?"
    keyboard = markup(
        [btn("🗑 Да, удалить", PostAct(a="del_ok", id=post.id, f=origin), RED)],
        [btn("✖️ Отмена", Nav(to="post", id=post.id, f=origin))],
    )
    return text, keyboard


# ---------------------------------------------------------------------- мои посты


async def library_view(app: App, page: int = 0, note: str | None = None) -> Screen:
    total = await app.repo.count_posts_total()
    pages, page = _pages(total, page, POSTS_PAGE_SIZE)
    items = await app.repo.list_all_posts(page * POSTS_PAGE_SIZE, POSTS_PAGE_SIZE)
    lines = ["<b>🗂 Мои посты</b>", ""]
    if not total:
        lines.append(
            "Постов пока нет. Нажмите «➕ Новый пост» и пришлите сообщение: текст, фото, видео, альбом "
            "или пересланный пост. Для поста сразу появится рассылка — останется отметить чаты и задать время."
        )
    else:
        lines.append(
            "Все посты из рассылок и черновиков, новые — сверху. Откройте пост, чтобы посмотреть, изменить, "
            "скопировать в другую рассылку или сделать из него новую."
        )
        lines += ["", f"Всего постов: <b>{total}</b>"]
        for number, (post, campaign) in enumerate(items, start=page * POSTS_PAGE_SIZE + 1):
            lines.append(f"{number}. {_post_line(post, 40)} · {t.esc(_owner_label(campaign))}")
    if note:
        lines += ["", note]
    rows: list[list[InlineKeyboardButton] | None] = []
    for number, (post, _campaign) in enumerate(items, start=page * POSTS_PAGE_SIZE + 1):
        snippet = t.cut(post_text(post.kind, post.payload), 24)
        label = f"{number}. {post_title(post.kind, post.payload)} {snippet}".strip()
        rows.append([btn(label, Nav(to="post", id=post.id, f=page + 1))])
    rows.append(_pager(page, pages, lambda p: Nav(to="lib", page=p)))
    rows.append([btn("➕ Новый пост", LibAct(a="add", id=0), GREEN)])
    rows.append(back("main", text="« Меню"))
    return "\n".join(lines), markup(*rows)


async def copy_post_view(app: App, post_id: int, page: int = 0, origin: int = 0) -> Screen | None:
    post = await app.repo.get_post(post_id)
    if post is None:
        return None
    candidates = [
        c for c in [*await app.repo.list_campaigns(), *await app.repo.list_drafts()] if c.id != post.campaign_id
    ]
    pages, page = _pages(len(candidates), page)
    lines = [
        "<b>📋 Копировать пост</b>",
        "",
        f"{_post_line(post)}",
        "",
    ]
    if candidates:
        lines.append("Выберите рассылку или черновик — копия поста встанет в конец его списка постов.")
    else:
        lines.append("Других рассылок и черновиков пока нет — можно сделать новую рассылку с этим постом.")
    rows: list[list[InlineKeyboardButton] | None] = [
        [btn(_owner_label(c, 40), LibAct(a="cp", id=post.id, v=c.id, f=origin))]
        for c in candidates[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    rows.append(_pager(page, pages, lambda p: LibAct(a="to", id=post.id, v=p, f=origin)))
    rows.append([btn("✨ Новая рассылка с этим постом", LibAct(a="new", id=post.id, f=origin), GREEN)])
    rows.append([btn("« К посту", Nav(to="post", id=post.id, f=origin))])
    return "\n".join(lines), markup(*rows)


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
        btn(
            ("✅" if day in selected else "▫️") + t.WEEKDAYS_SHORT[day],
            SchedAct(a="wd", id=cid, v=day, w=0 if day in selected else 1),
        )
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
                SchedAct(a="jit", id=cid, v=_next_jitter(campaign.jitter_min)),
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
    targets = [] if campaign.is_draft else await app.repo.campaign_targets(campaign.id)
    lines = [
        f"<b>⚙️ Опции · «{t.esc(campaign.name)}»</b>",
        "",
        "🔕 <b>Без звука</b> — участники не получат уведомление о посте.",
        "🔒 <b>Защита</b> — пост нельзя переслать и сохранить.",
        "📌 <b>Закреплять</b> — новый пост закрепляется, прошлый открепляется.",
        "🗑 <b>Удалять прошлый</b> — после публикации бот удаляет предыдущий пост этой рассылки "
        "(Telegram разрешает удалять только сообщения моложе 48 часов).",
        "",
        "Опции общие для всех чатов рассылки. Тема форума у каждого чата своя — она задаётся в «💬 Чаты».",
    ]
    no_pin = [x.chat.title for x in targets if x.chat.status == "active" and not x.chat.can_pin]
    if campaign.pin and no_pin:
        lines += ["", f"⚠️ Нет права закреплять сообщения в: {t.names_label(no_pin, 5)}"]
    if campaign.delete_prev and su.max_gap_hours(campaign.times, campaign.weekdays) > 48:
        lines += ["", "⚠️ Между публикациями больше 48 часов — удалить прошлый пост не получится."]
    if note:
        lines += ["", note]

    def mark(value: bool) -> str:
        return "✅" if value else "❌"

    cid = campaign.id
    rows: list[list[InlineKeyboardButton] | None] = [
        [btn(f"🔕 Без звука: {mark(campaign.silent)}", OptAct(a="silent", id=cid, v=int(not campaign.silent)))],
        [btn(f"🔒 Защита: {mark(campaign.protect)}", OptAct(a="protect", id=cid, v=int(not campaign.protect)))],
        [btn(f"📌 Закреплять: {mark(campaign.pin)}", OptAct(a="pin", id=cid, v=int(not campaign.pin)))],
        [
            btn(
                f"🗑 Удалять прошлый: {mark(campaign.delete_prev)}",
                OptAct(a="delprev", id=cid, v=int(not campaign.delete_prev)),
            )
        ],
        back("camp", cid),
    ]
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
        "в пару нажатий, а после правок — обновить созданные из него рассылки.",
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
    """Выбор чатов, к которым применить черновик."""
    source = await app.repo.get_campaign(int(data["src"]))
    if source is None or not source.is_draft:
        return None
    chats = await app.repo.list_chats(statuses=("active",))
    selected = {int(x) for x in data.get("sel", [])} & {c.id for c in chats}
    linked = await app.repo.linked_campaigns(source.id)
    linked_targets = await app.repo.targets_of(c.id for c in linked)
    already = {x.chat.id for items in linked_targets.values() for x in items}
    pages, page = _pages(len(chats), int(data.get("page", 0)))

    lines = [f"<b>📤 Применить черновик «{t.esc(source.name)}»</b>", ""]
    if chats:
        if len(linked) == 1:
            lines.append(
                f"Из черновика уже есть рассылка «{t.esc(linked[0].name)}». Отмеченные чаты добавятся к ней, "
                "а её посты, расписание и опции обновятся по черновику."
            )
        elif linked:
            lines.append(
                f"Из черновика уже есть рассылки ({len(linked)}). Новые чаты добавятся в первую из них, "
                f"«{t.esc(linked[0].name)}». Для чатов с 🔄 второй рассылки не будет — их рассылка просто "
                "обновится по черновику."
            )
        else:
            lines.append(
                "Отметьте чаты и нажмите кнопку внизу. Из черновика получится рассылка с его постами, "
                "расписанием и опциями — она будет публиковать во все отмеченные чаты."
            )
        if already:
            lines.append("🔄 — чат уже есть в рассылке из этого черновика.")
        lines += ["", f"Выбрано: <b>{len(selected)}</b> из {len(chats)}"]
    else:
        lines.append("Нет подходящих чатов. Добавьте бота администратором в чаты — они появятся здесь.")

    rows: list[list[InlineKeyboardButton] | None] = []
    for chat in chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        mark = "☑️" if chat.id in selected else "⬜"
        suffix = " 🔄" if chat.id in already else ""
        rows.append([btn(f"{mark} {t.cut(chat.title, 38)}{suffix}", PickAct(a="t", s=source.id, v=chat.id))])
    rows.append(_pager(page, pages, lambda p: PickAct(a="pg", s=source.id, v=p)))
    if chats:
        rows.append(
            [btn("☑️ Выбрать все", PickAct(a="all", s=source.id)), btn("⬜ Снять все", PickAct(a="none", s=source.id))]
        )
        rows.append([btn("🚀 Применить и запустить", PickAct(a="go", s=source.id, v=1), GREEN)])
        rows.append([btn("💾 Применить без запуска", PickAct(a="go", s=source.id, v=0))])
    rows.append(back("camp", source.id, "✖️ Отмена"))
    return "\n".join(lines), markup(*rows)


async def apply_summary(app: App, draft_id: int, applied: DraftApplied, warnings: list[str]) -> Screen:
    tz, now = app.settings.tz, _now(app)
    lines = ["<b>✅ Готово</b>"]
    targets = await app.repo.targets_of(applied.touched)
    campaigns = [c for c in [await app.repo.get_campaign(cid) for cid in applied.touched] if c is not None]
    for campaign in campaigns[:10]:
        created = applied.created and campaign.id == applied.campaign.id
        names = [x.chat.title for x in targets.get(campaign.id, [])]
        if campaign.is_active and campaign.next_run_ts:
            status = f"🟢 работает, ближайшая {t.fmt_ts(campaign.next_run_ts, tz, now)}"
        elif campaign.is_active:
            status = "🟢 запущена"
        else:
            status = "⏸ остановлена — проверьте и нажмите «▶️ Запустить»"
        lines += [
            "",
            ("Создана рассылка" if created else "Обновлена рассылка") + f" «<b>{t.esc(campaign.name)}</b>».",
            f"💬 Чаты ({len(names)}): {t.names_label(names, 10) if names else 'не выбраны'}",
            f"Статус: {status}",
        ]
    if len(campaigns) > 10:
        lines.append(f"\n…и ещё рассылок: {len(campaigns) - 10}")
    if warnings:
        lines += ["", *(w if len(w) < 1500 else w[:1500] + "…" for w in warnings)]
    rows = [
        [
            btn(
                f"📬 {t.cut(c.name, 30)} · {_chats_word(len(targets.get(c.id, [])))}",
                Nav(to="camp", id=c.id, f=-1),
                BLUE if i == 0 else None,
            )
        ]
        for i, c in enumerate(campaigns[:5])
    ]
    rows.append(back("camp", draft_id, "« К черновику"))
    return "\n".join(lines), markup(*rows)


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
        [btn("🛡 Антиспам", Guard(a="spam_set")), btn("🔒 Обязательная подписка", Guard(a="sub"))],
        [
            btn("▶️ Возобновить все рассылки", SetAct(a="pause", v=0), GREEN)
            if paused
            else btn("⏸ Поставить все на паузу", SetAct(a="pause", v=1), RED)
        ],
        [
            btn(
                f"🔔 Уведомления: {'вкл' if app.settings.notify_errors else 'выкл'}",
                SetAct(a="notify", v=int(not app.settings.notify_errors)),
            )
        ],
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
    items = [c for c in await app.repo.active_campaigns() if c.next_run_ts][:10]
    targets = await app.repo.targets_of(c.id for c in items)
    lines = ["<b>📅 Ближайшие публикации</b>", ""]
    if app.settings.paused_all:
        lines.append("⏸ Все рассылки на паузе — публикаций не будет, пока не возобновите их в настройках.")
    elif not items:
        lines.append("Запланированных публикаций нет. Запустите любую рассылку в «📬 Рассылки».")
    else:
        for campaign in items:
            names = [x.chat.title for x in targets[campaign.id] if x.deliverable]
            where = t.names_label(names, 2, 24) if names else "⚠️ нет доступных чатов"
            lines.append(f"• <b>{t.fmt_ts(campaign.next_run_ts, tz, now)}</b> — «{t.esc(campaign.name, 30)}» → {where}")
    rows: list[list[InlineKeyboardButton] | None] = [
        [btn(f"{t.fmt_ts(c.next_run_ts, tz, now)} · {t.cut(c.name, 28)}", Nav(to="camp", id=c.id, f=-1))] for c in items
    ]
    rows.append([btn("🔄 Обновить", Nav(to="upcoming")), btn("« Меню", Nav(to="main"))])
    return "\n".join(lines), markup(*rows)


def help_view() -> Screen:
    text = (
        "<b>❓ Как пользоваться</b>\n\n"
        "<b>1. Добавьте бота в чаты.</b> Кнопки «➕ Добавить группу/канал» внизу откроют выбор чата — "
        "Telegram сам сделает бота администратором. Можно и вручную: настройки чата → Администраторы. "
        "Для канала нужно право «Публикация сообщений», для закрепления — «Закрепление» "
        "(в канале — «Редактирование»).\n\n"
        "<b>2. Создайте рассылку.</b> «📬 Рассылки» → «➕ Новая рассылка». В «💬 Чаты» отметьте галочками, "
        "куда публиковать, в «📝 Посты» пришлите боту сообщения (или перешлите их из канала), задайте время "
        "и нажмите «▶️ Запустить». Одна рассылка публикует одни и те же посты по одному расписанию "
        "во все отмеченные чаты.\n\n"
        "<b>3. Мои посты.</b> «🗂 Мои посты» — все посты из рассылок и черновиков. Пост можно открыть, "
        "изменить, скопировать в другую рассылку или сделать из него новую рассылку.\n\n"
        "<b>4. Защита чатов.</b> Во всех группах бот молча удаляет спам: ссылки, пересылки, @ботов и @каналы, "
        "рекламу от имени каналов, стоп-слова. «⚙️ Настройки → 🔒 Обязательная подписка» — писать смогут только "
        "подписчики выбранных каналов. Правила чата — «🛡 Защита» на экране чата. Боту нужно право "
        "«Удаление сообщений», а в каналах для подписки он должен быть админом.\n\n"
        "<b>5. Черновики.</b> «💾 В черновики» сохраняет посты, расписание и опции. «📤 Применить к чатам» "
        "запускает черновик в нужных чатах. Изменили черновик — «🔄 Обновить рассылки».\n\n"
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
        "Команды: /start — меню, /campaigns — рассылки, /posts — мои посты, /chats — чаты, "
        "/drafts — черновики, /settings — настройки, /cancel — отменить ввод."
    )
    return text, markup(back("main", text="« Меню"))


# --------------------------------------------------------------------------- навигация


async def resolve(app: App, nav: Nav, user_id: int | None = None) -> Screen | None:
    match nav.to:
        case "main":
            return await main_menu(app)
        case "chats":
            return await chats_list(app, nav.page)
        case "chat":
            return await chat_view(app, nav.id)
        case "camps":
            return await campaigns_list(app, nav.page)
        case "camp":
            return await campaign_view(app, nav.id, user_id=user_id)
        case "tgt":
            return await targets_view(app, nav.id, nav.page)
        case "posts":
            return await posts_view(app, nav.id, nav.page)
        case "post":
            return await post_view(app, nav.id, origin=nav.f)
        case "lib":
            return await library_view(app, nav.page)
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
