"""Предпросмотр постов в чате админа + проверка, отображаются ли премиум-эмодзи."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Sequence

from aiogram.exceptions import TelegramAPIError

from bot.app import App
from bot.db.models import Post
from bot.services.content import custom_emoji_count
from bot.services.errors import humanize
from bot.services.sender import PostData, PostSendError, received_custom_emoji, send_post

PREMIUM_MISSING = (
    "💎 Премиум-эмодзи не отобразились: у владельца бота (аккаунта, который создал его в @BotFather) "
    "нет Telegram Premium. Без него бот показывает обычные эмодзи."
)
PREMIUM_OK = "💎 Премиум-эмодзи отображаются."


async def send_preview(app: App, posts: Sequence[Post | PostData], chat_id: int) -> str:
    """Присылает посты админу (chat_id) так, как они уйдут в чат. Возвращает заметку для экрана."""
    expected = received = 0
    failures: list[str] = []
    for index, post in enumerate(posts, start=1):
        data = post if isinstance(post, PostData) else PostData.of(post)
        try:
            result = await send_post(app.bot, chat_id, data)
        except (TelegramAPIError, PostSendError) as error:
            failures.append(f"пост #{index}: {html.escape(humanize(error))}")
            continue
        if not result.forwarded:
            expected += custom_emoji_count(data.kind, data.payload)
            received += received_custom_emoji(result.messages)
        if len(posts) > 1:
            await asyncio.sleep(0.3)
    notes = []
    if failures:
        notes.append("⚠️ Не удалось показать " + "; ".join(failures))
    if expected:
        notes.append(PREMIUM_MISSING if received < expected else PREMIUM_OK)
    return "\n".join(notes)
