"""Небольшие клавиатуры и конструкторы кнопок."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    ChatAdministratorRights,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
)

from bot.ui.callbacks import CampAct, ChatAct, Guard, Nav, SubCheck

GREEN, RED, BLUE = "success", "danger", "primary"

ADD_GROUP_TEXT = "➕ Добавить группу"
ADD_CHANNEL_TEXT = "➕ Добавить канал"
REQUEST_GROUP, REQUEST_CHANNEL = 1, 2


def btn(text: str, data: CallbackData | str, style: str | None = None) -> InlineKeyboardButton:
    payload = data.pack() if isinstance(data, CallbackData) else data
    if style:
        return InlineKeyboardButton(text=text, callback_data=payload, style=style)
    return InlineKeyboardButton(text=text, callback_data=payload)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def markup(*rows: Sequence[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows if row])


def back(to: str, item_id: int = 0, text: str = "« Назад") -> list[InlineKeyboardButton]:
    return [btn(text, Nav(to=to, id=item_id))]


def open_campaign(campaign_id: int) -> InlineKeyboardMarkup:
    return markup([btn("📬 Открыть рассылку", Nav(to="camp", id=campaign_id))])


def open_chat(chat_id: int) -> InlineKeyboardMarkup:
    return markup([btn("💬 Открыть чат", Nav(to="chat", id=chat_id))])


def chat_added(chat_id: int) -> InlineKeyboardMarkup:
    return markup(
        [btn("⚙️ Открыть чат", Nav(to="chat", id=chat_id), BLUE)],
        [
            btn("➕ Новая рассылка", ChatAct(a="new", id=chat_id)),
            btn("📝 Из черновика", ChatAct(a="fromdraft", id=chat_id)),
        ],
    )


def chat_pending(chat_id: int) -> InlineKeyboardMarkup:
    return markup(
        [
            btn("✅ Принять", ChatAct(a="accept", id=chat_id), GREEN),
            btn("🚪 Выйти", ChatAct(a="leave", id=chat_id), RED),
        ]
    )


def chat_back(chat_id: int, stopped: int) -> InlineKeyboardMarkup:
    start = [btn(f"▶️ Запустить остановленные ({stopped})", ChatAct(a="resume", id=chat_id), GREEN)]
    return markup(start if stopped else None, [btn("⚙️ Открыть чат", Nav(to="chat", id=chat_id))])


def sub_notice(channels: Sequence[tuple[str, str | None]], user_id: int) -> InlineKeyboardMarkup:
    """Под подсказкой неподписанному: ссылки на каналы и проверка подписки."""
    rows = [[url_btn(f"📢 {title[:40]}", url)] for title, url in channels[:5] if url]
    rows.append([btn("✅ Я подписался", SubCheck(u=user_id), GREEN)])
    return markup(*rows)


def open_sub_settings() -> InlineKeyboardMarkup:
    return markup([btn("🔒 Настройки подписки", Guard(a="sub"))])


def campaign_problem(campaign_id: int) -> InlineKeyboardMarkup:
    return markup(
        [btn("💬 Чаты рассылки", Nav(to="tgt", id=campaign_id), BLUE)],
        [btn("📬 Открыть рассылку", Nav(to="camp", id=campaign_id))],
    )


def done_adding(campaign_id: int, action: str = "done") -> InlineKeyboardMarkup:
    """action: done — после «Готово» список постов, fin — экран рассылки (пост из «Моих постов»)."""
    return markup([btn("✅ Готово", CampAct(a=action, id=campaign_id), GREEN)])


def _rights(**enabled: bool) -> ChatAdministratorRights:
    base = {
        "is_anonymous": False,
        "can_manage_chat": False,
        "can_delete_messages": False,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_send_welcome_messages": False,
    }
    base.update(enabled)
    return ChatAdministratorRights(**base)


def add_chat_reply() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: нативный выбор чата. Telegram сам добавит бота админом с нужными правами."""
    group_rights = _rights(can_delete_messages=True, can_pin_messages=True)
    # Пригласительные ссылки — чтобы в подсказке об обязательной подписке была кнопка на закрытый канал
    channel_rights = _rights(
        can_post_messages=True, can_edit_messages=True, can_delete_messages=True, can_invite_users=True
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=ADD_GROUP_TEXT,
                    request_chat=KeyboardButtonRequestChat(
                        request_id=REQUEST_GROUP,
                        chat_is_channel=False,
                        user_administrator_rights=group_rights,
                        bot_administrator_rights=group_rights,
                        request_title=True,
                        request_username=True,
                    ),
                ),
                KeyboardButton(
                    text=ADD_CHANNEL_TEXT,
                    request_chat=KeyboardButtonRequestChat(
                        request_id=REQUEST_CHANNEL,
                        chat_is_channel=True,
                        user_administrator_rights=channel_rights,
                        bot_administrator_rights=channel_rights,
                        request_title=True,
                        request_username=True,
                    ),
                ),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Меню — /start",
    )


def add_group_link(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?startgroup=autopost&admin=delete_messages+pin_messages"


def add_channel_link(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?startchannel&admin=post_messages+edit_messages+delete_messages+invite_users"
