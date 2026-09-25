"""Служебные события из групп. Всё остальное из групп бот игнорирует."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from bot.app import App
from bot.services import chats as chat_service

router = Router(name="group_events")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(F.migrate_to_chat_id)
async def on_migrate_to(message: Message, app: App) -> None:
    await chat_service.migrate(app, message.chat.id, message.migrate_to_chat_id)


@router.message(F.migrate_from_chat_id)
async def on_migrate_from(message: Message, app: App) -> None:
    await chat_service.migrate(app, message.migrate_from_chat_id, message.chat.id)
