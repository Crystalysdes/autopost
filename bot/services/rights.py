"""Права бота в чате. В группах и каналах они устроены по-разному."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BotRights:
    in_chat: bool
    is_admin: bool
    can_post: bool
    can_pin: bool
    can_delete: bool = False  # удалять сообщения участников — нужно защите группы
    can_invite: bool = False  # создавать ссылки-приглашения — для подписки на закрытый канал
    can_restrict: bool = False  # банить и ограничивать участников — команды /ban и /mute в группах


def enum_text(value: Any) -> str:
    """Значение enum aiogram как строка. str() не подходит: в Python 3.11+ он даёт «ChatType.CHANNEL»."""
    return str(getattr(value, "value", value) or "")


def rights_from_member(chat_type: str, member: Any) -> BotRights:
    """member — ChatMember* из getChatMember или my_chat_member.new_chat_member."""
    status = enum_text(getattr(member, "status", ""))
    if status in ("administrator", "creator"):
        can_delete = bool(getattr(member, "can_delete_messages", False))
        can_invite = bool(getattr(member, "can_invite_users", False))
        can_restrict = bool(getattr(member, "can_restrict_members", False))
        if chat_type == "channel":
            # В канале закрепление требует права редактировать сообщения
            return BotRights(
                in_chat=True,
                is_admin=True,
                can_post=bool(getattr(member, "can_post_messages", False)),
                can_pin=bool(getattr(member, "can_edit_messages", False)),
                can_delete=can_delete,
                can_invite=can_invite,
                can_restrict=can_restrict,
            )
        return BotRights(
            in_chat=True,
            is_admin=True,
            can_post=True,
            can_pin=bool(getattr(member, "can_pin_messages", False)),
            can_delete=can_delete,
            can_invite=can_invite,
            can_restrict=can_restrict,
        )
    if status == "member":
        return BotRights(in_chat=True, is_admin=False, can_post=chat_type != "channel", can_pin=False)
    if status == "restricted":
        in_chat = bool(getattr(member, "is_member", False))
        return BotRights(
            in_chat=in_chat,
            is_admin=False,
            can_post=in_chat and bool(getattr(member, "can_send_messages", False)),
            can_pin=in_chat and bool(getattr(member, "can_pin_messages", False)),
        )
    return BotRights(in_chat=False, is_admin=False, can_post=False, can_pin=False)
