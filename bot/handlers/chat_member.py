"""Бота добавили в чат, удалили или поменяли ему права."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import ChatMemberUpdated

from bot.app import App
from bot.services import chats as chat_service
from bot.ui import keyboards, texts

router = Router(name="chat_member")


@router.my_chat_member(F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_my_chat_member(event: ChatMemberUpdated, app: App) -> None:
    actor = event.from_user
    change = await chat_service.apply_member(app, chat=event.chat, member=event.new_chat_member, actor_id=actor.id)
    chat = change.chat
    if chat is None:
        return
    if app.moderator is not None:  # права или статус чата поменялись — защита видит это сразу
        app.moderator.forget(chat.tg_id)

    if change.status == "left":
        if change.prev_status in ("active", "pending"):
            reason = "бота удалили из чата или лишили прав администратора"
            await app.notify(texts.chat_lost_text(chat.title, reason), keyboards.open_chat(chat.id))
        return

    if change.prev_status in (None, "left", "pending") and change.status == "active":
        if change.prev_status == "left":
            stopped = sum(1 for c in await app.repo.list_campaigns(chat.id) if not c.is_active)
            await app.notify(texts.chat_back_text(chat.title, stopped), keyboards.chat_back(chat.id, stopped))
        else:
            await app.notify(
                texts.chat_added_text(chat.title, chat.type, chat.can_post, chat.can_pin),
                keyboards.chat_added(chat.id),
            )
        return

    if change.prev_status in (None, "left") and change.status == "pending":
        await app.notify(
            texts.chat_pending_text(chat.title, chat.type, actor.full_name, actor.id),
            keyboards.chat_pending(chat.id),
        )
        return

    if change.lost_post_right:
        await app.notify(texts.lost_post_right_text(chat.title), keyboards.open_chat(chat.id))
