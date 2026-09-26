"""Экраны защиты чатов: антиспам, обязательная подписка и автоприём заявок."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton

from bot.app import App
from bot.db.models import Chat, now_ts
from bot.services import spam
from bot.services.moderation import GROUP_TYPES, auto_approve_on, required_channels
from bot.ui import texts as t
from bot.ui.callbacks import Guard, Nav
from bot.ui.keyboards import GREEN, RED, btn, markup
from bot.ui.render import Screen

MAX_CHANNELS = 40
MAX_JOIN_CHATS = 40  # галочек на экране автоприёма; остальные чаты — на их экранах
LOG_LIMIT = 10
# Порядок переключателей на экране (в callback — имя правила)
UI_RULES = ("links", "bots", "forwards", "channels", "words", "contacts", "names", "service")
SUB_MODES = {0: None, 1: "own", 2: "off"}


def _now(app: App) -> int:
    return app.scheduler.now() if app.scheduler else now_ts()


def _mark(value: bool) -> str:
    return "✅" if value else "❌"


async def _counts_line(app: App, chat_id: int | None = None) -> str:
    now = _now(app)
    spam_today, sub_today = await app.repo.moderation_counts(t.day_start_ts(app.settings.tz, now), chat_id)
    spam_week, sub_week = await app.repo.moderation_counts(now - 7 * 24 * 3600, chat_id)
    return (
        f"📊 Удалено сегодня: спам {spam_today} · без подписки {sub_today}\n"
        f"За 7 дней: спам {spam_week} · без подписки {sub_week}"
    )


async def active_channels(app: App) -> list[Chat]:
    return [c for c in await app.repo.list_chats(statuses=("active",)) if c.type == "channel"]


async def active_groups(app: App) -> list[Chat]:
    return [c for c in await app.repo.list_chats(statuses=("active",)) if c.type in GROUP_TYPES]


def channel_problem(channel: Chat) -> str | None:
    """Почему подписку на канал не получится проверить или почему в подсказке не будет личной ссылки."""
    if channel.status != "active":
        return "бота нет в канале — подписка на него не проверяется"
    if not channel.is_admin:
        return "бот не админ канала — подписку не проверить"
    if channel.can_invite is False:
        if not channel.username and not channel.invite_link:
            return f"закрытый канал, а у бота нет права {t.INVITE_RIGHT} — в подсказке не будет кнопки"
        return f"у бота нет права {t.INVITE_RIGHT} — в подсказке обычная ссылка вместо личной"
    return None


async def _channels_by_ids(app: App, ids: Sequence[int]) -> list[Chat]:
    channels = [await app.repo.get_chat(channel_id) for channel_id in ids]
    return [c for c in channels if c is not None and c.type == "channel"]


def _names(channels: Sequence[Chat]) -> str:
    return t.names_label([c.title for c in channels], 5, 30)


# -------------------------------------------------------------------- защита чата


def protection_label(chat: Chat, common: Sequence[int]) -> str:
    """Подпись кнопки на экране чата."""
    rules = spam.SpamRules.of(chat.spam_filter)
    sub = "🔒" if required_channels(chat, common) else "—"
    return f"🛡 Защита: антиспам {_mark(rules.on)} · подписка {sub}"


async def protection_view(app: App, chat_id: int, note: str | None = None) -> Screen | None:
    chat = await app.repo.get_chat(chat_id)
    if chat is None or chat.type not in GROUP_TYPES:
        return None
    rules = spam.SpamRules.of(chat.spam_filter)
    channels = await _channels_by_ids(app, required_channels(chat, app.settings.sub_channels))
    lines = [f"<b>🛡 Защита «{t.esc(chat.title)}»</b>", ""]
    if rules.on:
        lines.append("Антиспам: ✅ включён — бот молча удаляет спам участников. Отметьте, что считать спамом:")
    else:
        lines.append("Антиспам: ⏸ выключен.")
    if chat.sub_mode == "off":
        sub_line = "Подписка: выключена — писать можно без подписки."
    elif channels:
        whose = "свои каналы" if chat.sub_mode == "own" else "общие каналы"
        sub_line = f"Подписка: 🔒 {whose} — {_names(channels)}. Без подписки сообщения удаляются."
    elif chat.sub_mode == "own":
        sub_line = "Подписка: свои каналы не выбраны — пока писать можно без подписки."
    else:
        sub_line = "Подписка: общие каналы не выбраны (⚙️ Настройки → 🔒 Обязательная подписка)."
    lines += [sub_line, "Админы чата и бота не проверяются.", "", await _counts_line(app, chat.id)]
    if chat.can_delete is False:
        lines += ["", "⚠️ У бота нет права «Удаление сообщений» — защита не работает. Выдайте его в чате."]
    problems = [f"⚠️ «{t.esc(c.title, 30)}»: {p}" for c in channels if (p := channel_problem(c))]
    if problems:
        lines += ["", *problems]
    if note:
        lines += ["", note]

    cid = chat.id
    rows: list[list[InlineKeyboardButton] | None] = [
        [
            btn("🛡 Антиспам: ✅ включён", Guard(a="spam", id=cid, v=0))
            if rules.on
            else btn("🛡 Антиспам: ⏸ выключен", Guard(a="spam", id=cid, v=1), GREEN)
        ]
    ]
    if rules.on:
        toggles = [
            btn(
                f"{_mark(getattr(rules, rule))} {spam.RULE_TITLES[rule]}",
                Guard(a="r_" + rule, id=cid, v=int(not getattr(rules, rule))),
            )
            for rule in UI_RULES
        ]
        rows += [toggles[i : i + 2] for i in range(0, len(toggles), 2)]
    mode = {None: 0, "own": 1, "off": 2}.get(chat.sub_mode, 0)
    titles = ("🔒 Общие каналы", "🔒 Свои каналы", "Без подписки")
    rows.append(
        [btn(("• " if i == mode else "") + title, Guard(a="sm", id=cid, v=i)) for i, title in enumerate(titles)]
    )
    if chat.sub_mode == "own":
        rows.append([btn(f"📢 Каналы этого чата ({len(chat.sub_channels or [])})", Guard(a="ch", id=cid))])
    rows.append([btn("🗒 Последние удалённые", Guard(a="log", id=cid))])
    rows.append([btn("« К чату", Nav(to="chat", id=cid))])
    return "\n".join(lines), markup(*rows)


async def log_view(app: App, chat_id: int) -> Screen | None:
    chat = await app.repo.get_chat(chat_id)
    if chat is None:
        return None
    tz, now = app.settings.tz, _now(app)
    entries = await app.repo.last_moderation(chat.id, LOG_LIMIT)
    lines = [f"<b>🗒 Удалено защитой · «{t.esc(chat.title)}»</b>", ""]
    if not entries:
        lines.append("Пока ничего не удалено.")
    for entry in entries:
        when = t.fmt_ts(entry.ts, tz, now)
        reason = spam.REASON_TITLES.get(entry.reason, entry.reason)
        who = t.esc(entry.user_name or "—", 30)
        line = f"• {when} · {who} · {reason}"
        if entry.snippet:
            line += f"\n  <i>{t.esc(entry.snippet, 100)}</i>"
        lines.append(line)
    lines += ["", "Журнал хранится две недели. Если бот ошибся — ослабьте правило на экране защиты чата."]
    return "\n".join(lines), markup([btn("« Защита чата", Guard(a="chat", id=chat.id))])


# --------------------------------------------------------------- выбор каналов


async def channels_view(app: App, chat_id: int, note: str | None = None) -> Screen | None:
    """Галочки каналов для подписки: chat_id=0 — общие каналы, иначе свои каналы чата."""
    chat = await app.repo.get_chat(chat_id) if chat_id else None
    if chat_id and chat is None:
        return None
    selected = set(chat.sub_channels or []) if chat else set(app.settings.sub_channels)
    channels = await active_channels(app)
    known = {c.id for c in channels}
    stale = await _channels_by_ids(app, [cid for cid in selected if cid not in known])
    if chat:
        lines = [f"<b>📢 Каналы для подписки · «{t.esc(chat.title)}»</b>", ""]
        lines.append("Участники этого чата смогут писать, только подписавшись на все отмеченные каналы.")
    else:
        lines = ["<b>📢 Общие каналы для подписки</b>", ""]
        lines.append(
            "Участники групп смогут писать, только подписавшись на все отмеченные каналы. Действует во всех "
            "группах, кроме тех, где выбраны свои каналы или подписка выключена."
        )
    lines.append("Бот должен быть администратором канала — иначе проверить подписку нельзя.")
    if not channels:
        lines += ["", "Каналов пока нет. Добавьте бота администратором в канал — он появится здесь."]
    problems = [
        f"⚠️ «{t.esc(c.title, 30)}»: {p}" for c in [*channels, *stale] if c.id in selected and (p := channel_problem(c))
    ]
    if problems:
        lines += ["", *problems]
    if note:
        lines += ["", note]
    rows: list[list[InlineKeyboardButton] | None] = []
    for channel in [*channels, *stale][:MAX_CHANNELS]:
        on = channel.id in selected
        rows.append(
            [
                btn(
                    f"{'☑️' if on else '⬜'} 📢 {t.cut(channel.title, 36)}",
                    Guard(a="coff" if on else "con", id=chat_id, v=channel.id),
                )
            ]
        )
    rows.append([btn("« Защита чата", Guard(a="chat", id=chat_id))] if chat else [btn("« Подписка", Guard(a="sub"))])
    return "\n".join(lines), markup(*rows)


# ---------------------------------------------------------------- общие настройки


async def spam_settings_view(app: App, note: str | None = None) -> Screen:
    groups = await active_groups(app)
    enabled = sum(1 for g in groups if spam.SpamRules.of(g.spam_filter).on)
    words = app.settings.spam_words
    allow = app.settings.spam_allow
    lines = [
        "<b>🛡 Антиспам</b>",
        "",
        "Бот молча удаляет спам участников групп: ссылки, пересылки, @ботов и @каналы, сообщения через ботов "
        "и от имени каналов, стоп-слова, контакты, рекламу в имени, сообщения о входе и выходе. Админы чатов "
        "и бота не проверяются. Что считать спамом в конкретном чате — на экране чата («🛡 Защита»).",
        "",
        f"Включён в группах: <b>{enabled}</b> из {len(groups)}",
        f"🚫 Стоп-слова: {len(words) if words is not None else len(spam.DEFAULT_STOP_WORDS)}"
        + (" (свой список)" if words is not None else " (стандартный список)"),
        "✅ Разрешённые ссылки: " + (t.esc(", ".join(allow[:10]), 300) if allow else "нет"),
        "",
        await _counts_line(app),
    ]
    if note:
        lines += ["", note]
    rows = [
        [
            btn("✅ Включить во всех группах", Guard(a="spam_all", v=1), GREEN),
            btn("⏸ Выключить во всех", Guard(a="spam_all", v=0), RED),
        ],
        [btn("🚫 Стоп-слова", Guard(a="words")), btn("✅ Разрешённые ссылки", Guard(a="allow"))],
        [btn("« Настройки", Nav(to="settings"))],
    ]
    return "\n".join(lines), markup(*rows)


async def sub_settings_view(app: App, note: str | None = None) -> Screen:
    groups = await active_groups(app)
    common = await _channels_by_ids(app, app.settings.sub_channels)
    own = sum(1 for g in groups if g.sub_mode == "own")
    off = sum(1 for g in groups if g.sub_mode == "off")
    lines = [
        "<b>🔒 Обязательная подписка</b>",
        "",
        "Участники групп смогут писать, только подписавшись на выбранные каналы. Сообщение неподписанного бот "
        "сразу удаляет и на 5 минут показывает подсказку: каждому человеку — новые личные ссылки-приглашения "
        "на каналы и кнопка «✅ Проверить подписку». Подписался и нажал — подсказка исчезает, можно писать. "
        "Админы чатов и бота не проверяются.",
        "",
        f"Для личных ссылок боту нужно право {t.INVITE_RIGHT} в каждом канале.",
        "",
        "Общие каналы: "
        + (_names(common) if common else "<b>не выбраны</b> — подписка нужна только в чатах со своими каналами"),
        f"Групп: на общих каналах — {len(groups) - own - off}, со своими — {own}, без подписки — {off}",
        "",
        await _counts_line(app),
    ]
    problems = [f"⚠️ «{t.esc(c.title, 30)}»: {p}" for c in common if (p := channel_problem(c))]
    if problems:
        lines += ["", *problems]
    if note:
        lines += ["", note]
    rows = [[btn(f"📢 Общие каналы ({len(common)})", Guard(a="ch", id=0), GREEN)]]
    if own or off:
        rows.append([btn("↩️ Все группы — на общие каналы", Guard(a="sub_reset"))])
    rows.append([btn("« Настройки", Nav(to="settings"))])
    return "\n".join(lines), markup(*rows)


async def joins_view(app: App, note: str | None = None) -> Screen:
    """Автоприём заявок на вступление: галочки чатов и «во всех»."""
    chats = await app.repo.list_chats(statuses=("active",))
    default = app.settings.join_auto
    enabled = [c for c in chats if auto_approve_on(c, default)]
    no_right = [c for c in enabled if c.can_invite is False]
    now = _now(app)
    today = await app.repo.join_counts(t.day_start_ts(app.settings.tz, now))
    week = await app.repo.join_counts(now - 7 * 24 * 3600)
    lines = [
        "<b>🚪 Автоприём заявок</b>",
        "",
        "Бот сам принимает заявки на вступление в отмеченные группы и каналы. Заявки бывают, где включено "
        "«Одобрение новых участников» или раздаются ссылки с заявками, — в остальных чатах люди вступают сразу.",
        "",
        f"Telegram присылает боту заявки, только если у него есть право приглашать: в группе — "
        f"{t.GROUP_INVITE_RIGHT}, в канале — {t.INVITE_RIGHT}.",
        "",
        f"Включён: <b>{len(enabled)}</b> из {len(chats)}",
        "Новые чаты: " + ("✅ автоприём включается сам" if default else "⏸ без автоприёма"),
        f"📊 Принято сегодня: {today} · за 7 дней: {week}",
    ]
    if no_right:
        names = t.names_label([c.title for c in no_right], 5, 30)
        lines += ["", f"⚠️ Нет права приглашать — заявки не приходят боту: {names}"]
    if len(chats) > MAX_JOIN_CHATS:
        lines += ["", f"Показаны первые {MAX_JOIN_CHATS} — остальные включаются на экране чата."]
    if note:
        lines += ["", note]
    rows: list[list[InlineKeyboardButton] | None] = []
    for chat in chats[:MAX_JOIN_CHATS]:
        on = auto_approve_on(chat, default)
        icon = t.CHAT_ICONS.get(chat.type, "💬")
        warn = " ⚠️" if on and chat.can_invite is False else ""
        rows.append(
            [
                btn(
                    f"{'☑️' if on else '⬜'} {icon} {t.cut(chat.title, 34)}{warn}",
                    Guard(a="jn", id=chat.id, v=int(not on)),
                )
            ]
        )
    rows.append(
        [
            btn("✅ Включить во всех", Guard(a="jall", v=1), GREEN),
            btn("⏸ Выключить во всех", Guard(a="jall", v=0), RED),
        ]
    )
    rows.append([btn("« Настройки", Nav(to="settings"))])
    return "\n".join(lines), markup(*rows)


def words_prompt_text(app: App) -> str:
    words = app.settings.spam_words
    current = list(words) if words is not None else list(spam.DEFAULT_STOP_WORDS)
    shown = "\n".join(f"<code>{t.esc(word)}</code>" for word in current[:100])
    more = f"\n…и ещё {len(current) - 100}" if len(current) > 100 else ""
    return (
        "<b>🚫 Стоп-слова</b>\n\n"
        "Сообщение со стоп-словом удаляется. Слово срабатывает и как начало слова: «заработок» ловит "
        "«заработка», а короткие слова (до 3 букв) — только целиком. Латинские буквы-двойники "
        "(«зapaбoтoк») и невидимые символы бот распознаёт сам.\n\n"
        "Чтобы заменить список, пришлите новый — по одному слову или фразе в строке.\n\n"
        f"<b>Сейчас ({len(current)}):</b>\n{shown}{more}"
    )


def allow_prompt_text(app: App) -> str:
    allow = app.settings.spam_allow
    shown = "\n".join(f"<code>{t.esc(item)}</code>" for item in allow) if allow else "пусто"
    return (
        "<b>✅ Разрешённые ссылки</b>\n\n"
        "Ссылки из этого списка антиспам не удаляет. Пришлите список — по одному в строке: домен "
        "(<code>mysite.ru</code> — вместе с поддоменами), канал (<code>@mychannel</code> или "
        "<code>t.me/mychannel</code>). Юзернейм самого чата разрешён всегда.\n\n"
        f"<b>Сейчас:</b>\n{shown}"
    )
