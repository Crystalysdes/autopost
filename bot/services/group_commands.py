"""Команды админов в группах: /ban, /mute, /del, /delban, /delmute, /unban, /unmute.

Команда пишется ответом на сообщение нарушителя, срок — после неё: «/mute 1day», «/ban 2h». Без срока —
навсегда. Сообщение с командой бот сразу удаляет, а об успехе ничего не пишет: в чате не остаётся следов.
Замученному Telegram сам показывает, до какого времени он не может писать. Если команда не сработала
(нет ответа, непонятный срок, у бота нет прав), бот на несколько секунд показывает подсказку.

Пользоваться могут админы бота, скрытые админы, анонимный админ, создатель чата и админы чата с нужным
правом (банить — для бана и мута, удалять сообщения — для удаления). Остальных бот молча игнорирует.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandObject
from aiogram.types import Chat, ChatPermissions, Message, User

from bot.app import App
from bot.services import chats as chat_service
from bot.services.errors import humanize
from bot.services.moderation import ChatInfo, Moderator
from bot.services.rights import enum_text

logger = logging.getLogger(__name__)

COMMANDS = ("ban", "mute", "del", "delban", "delmute", "unban", "unmute")
MAX_SECONDS = 366 * 24 * 3600  # срок дольше Telegram считает вечным
DURATION_HELP = "Примеры: 30min, 2h, 1day, 1week, 1month (или 30мин, 2ч, 1д, 1нед, 1мес)."

_MINUTE, _HOUR, _DAY = 60, 3600, 24 * 3600
_UNITS = {
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes", "м", "мин", "минута", "минуты", "минут"), _MINUTE),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours", "ч", "час", "часа", "часов"), _HOUR),
    **dict.fromkeys(
        ("d", "day", "days", "д", "дн", "день", "дня", "дней", "сут", "сутки", "суток"),
        _DAY,
    ),
    **dict.fromkeys(("w", "week", "weeks", "н", "нед", "неделя", "недели", "недель"), 7 * _DAY),
    **dict.fromkeys(
        ("mo", "mon", "mont", "month", "months", "мес", "месяц", "месяца", "месяцев"),
        30 * _DAY,
    ),
    **dict.fromkeys(("y", "year", "years", "г", "год", "года", "лет"), 365 * _DAY),
}
_DURATION = re.compile(r"^(\d{1,4})\s*([a-zа-яё]+)", re.IGNORECASE)

# Замученный не может отправлять ничего; всё True — так Telegram снимает ограничения
_MUTED = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)
_FREE = ChatPermissions(**dict.fromkeys(ChatPermissions.model_fields, True))


@dataclass(frozen=True)
class Spec:
    delete: bool = False  # удалить сообщение, на которое ответили
    action: str | None = None  # ban, mute, unban, unmute


SPECS = {
    "del": Spec(delete=True),
    "ban": Spec(action="ban"),
    "mute": Spec(action="mute"),
    "delban": Spec(delete=True, action="ban"),
    "delmute": Spec(delete=True, action="mute"),
    "unban": Spec(action="unban"),
    "unmute": Spec(action="unmute"),
}


def parse_duration(args: str | None) -> tuple[int | None, str | None]:
    """Срок из текста после команды: (секунды или None — навсегда, непонятый кусок или None).
    «1day», «1 day», «30мин»… Текст, который начинается не с цифры, — это причина, срок «навсегда»."""
    text = (args or "").strip()
    if not text:
        return None, None
    match = _DURATION.match(text)
    if match is None:
        return (None, text.split()[0]) if text[0].isdigit() else (None, None)
    unit = _UNITS.get(match.group(2).lower().replace("ё", "е"))
    seconds = int(match.group(1)) * unit if unit else 0
    if seconds <= 0:
        return None, match.group(0)
    return (None if seconds > MAX_SECONDS else seconds), None


# ---------------------------------------------------------------------------- обработка


async def handle(app: App, message: Message, command: CommandObject) -> None:
    moderator = app.moderator
    if moderator is None:
        return
    info = await moderator.chat_info(message.chat.id)
    if info is None:  # чат не подключён к боту
        return
    await moderator.delete_message(info, message.message_id)  # команду — сразу, у всех
    name = command.command.lower()
    spec = SPECS[name]
    if not await _allowed(app, moderator, info, message, spec):
        return

    reply = message.reply_to_message
    if reply is not None and reply.forum_topic_created is not None:
        reply = None  # в форуме сообщения темы «отвечают» на её начало — это не ответ нарушителю
    target_id = _id_argument(command.args) if reply is None and spec.action in ("unban", "unmute") else None
    if reply is None and target_id is None:
        extra = f" Или с ID: <code>/{name} 123456789</code>." if spec.action in ("unban", "unmute") else ""
        await moderator.hint(info, message, f"↩️ Отправьте команду ответом на сообщение нарушителя.{extra}")
        return

    seconds = None
    if spec.action in ("ban", "mute"):
        seconds, wrong = parse_duration(command.args)
        if wrong:
            text = f"⏳ Не понял срок «{html.escape(wrong[:30])}». {DURATION_HELP}"
            await moderator.hint(info, message, text)
            return

    if reply is not None and spec.action and not await _can_touch(app, moderator, info, message, reply):
        return
    if spec.delete and reply is not None and not await moderator.delete_message(info, reply.message_id):
        await moderator.hint(
            info, message, "⚠️ Не получилось удалить сообщение — проверьте у бота право «Удаление сообщений»."
        )
        return
    if spec.action is None:
        return

    sender = reply.sender_chat if reply is not None else None
    if sender is not None and spec.action in ("mute", "unmute"):
        await moderator.hint(info, message, "📢 Канал нельзя замутить — только забанить: /ban.")
        return
    until = int(moderator.wall()) + seconds if seconds else None
    target: Chat | User | int = sender or (reply.from_user if reply is not None else None) or target_id or 0
    if await _apply(app, moderator, info, message, spec.action, target, until):
        who = message.from_user.full_name if message.from_user else "анонимный админ"
        logger.info("Команда /%s %s в «%s»: %s → %s", name, command.args or "", info.title, who, _name(target))


def _id_argument(args: str | None) -> int | None:
    token = (args or "").strip().split(maxsplit=1)[0] if args and args.strip() else ""
    return int(token) if re.fullmatch(r"-?\d{3,20}", token) else None


def _name(target: Chat | User | int) -> str:
    if isinstance(target, int):
        return str(target)
    return getattr(target, "full_name", None) or getattr(target, "title", None) or str(target.id)


async def _allowed(app: App, moderator: Moderator, info: ChatInfo, message: Message, spec: Spec) -> bool:
    """Может ли автор команды это сделать. Посторонних — молча игнорируем."""
    if message.sender_chat is not None:
        return message.sender_chat.id == message.chat.id  # анонимный админ группы
    user = message.from_user
    if user is None:
        return False
    if user.id in app.admin_ids or info.is_trusted(user.id, user.username):
        return True
    admins = await moderator.chat_admins(info)
    if admins is not None and user.id not in admins:  # может, его только что назначили
        admins = await moderator.chat_admins(info, fresh=True)
    member = admins.get(user.id) if admins else None
    if member is None:
        return False
    if enum_text(member.status) == "creator":
        return True
    can_restrict = bool(getattr(member, "can_restrict_members", False))
    can_delete = bool(getattr(member, "can_delete_messages", False))
    return (spec.action is None or can_restrict) and (not spec.delete or can_delete)


async def _can_touch(app: App, moderator: Moderator, info: ChatInfo, message: Message, reply: Message) -> bool:
    """Банить и мутить нельзя админов (бота и чата), самого бота и посты от имени этой группы или
    привязанного канала. Подсказка — автору команды."""
    sender = reply.sender_chat
    if sender is not None:
        if sender.id == reply.chat.id or reply.is_automatic_forward:
            text = "🛡 Это сообщение от имени группы или её канала — его автора не трогаю."
            await moderator.hint(info, message, text)
            return False
        return True
    user = reply.from_user
    if user is None:
        return False
    if user.id == app.bot_id:
        await moderator.hint(info, message, "🤖 Себя бот не банит и не мутит.")
        return False
    admins: dict[int, Any] | None = await moderator.chat_admins(info)
    if user.id in app.admin_ids or (admins is not None and user.id in admins):
        await moderator.hint(info, message, "🛡 Это админ — его нельзя забанить или замутить.")
        return False
    return True


async def _apply(
    app: App,
    moderator: Moderator,
    info: ChatInfo,
    message: Message,
    action: str,
    target: Chat | User | int,
    until: int | None,
) -> bool:
    bot = app.bot
    target_id = target if isinstance(target, int) else target.id
    is_channel = isinstance(target, Chat)
    try:
        if action == "ban" and is_channel:
            await bot.ban_chat_sender_chat(info.tg_id, target_id)
        elif action == "unban" and is_channel:
            await bot.unban_chat_sender_chat(info.tg_id, target_id)
        elif action == "ban":
            await bot.ban_chat_member(info.tg_id, target_id, until_date=until)
        elif action == "unban":
            await bot.unban_chat_member(info.tg_id, target_id, only_if_banned=True)
        elif action == "mute":
            await bot.restrict_chat_member(
                info.tg_id, target_id, _MUTED, use_independent_chat_permissions=True, until_date=until
            )
        else:
            await bot.restrict_chat_member(info.tg_id, target_id, _FREE, use_independent_chat_permissions=True)
    except TelegramBadRequest as error:
        text = error.message.lower()
        if "administrator" in text or "owner" in text or "creator" in text:
            await moderator.hint(info, message, "🛡 Это админ — его нельзя забанить или замутить.")
        elif "right" in text or "admin_required" in text:
            await moderator.hint(
                info,
                message,
                "⚠️ У бота нет права «Блокировка пользователей» в этом чате — выдайте его в настройках администраторов.",
            )
            chat = await app.repo.get_chat(info.id)
            if chat is not None:
                await chat_service.refresh_chat(app, chat)
                moderator.forget(info.tg_id)
        else:
            await moderator.hint(info, message, f"⚠️ Telegram не дал это сделать: {html.escape(humanize(error))}")
        return False
    except TelegramForbiddenError:
        return False  # бота уже нет в чате
    except TelegramAPIError as error:
        await moderator.hint(info, message, f"⚠️ Не получилось: {html.escape(humanize(error))}")
        return False
    return True
