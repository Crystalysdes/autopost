"""Сборка диспетчера: middleware и роутеры в нужном порядке."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.callback_answer import CallbackAnswerMiddleware

from bot.app import App
from bot.handlers import (
    campaigns,
    chat_member,
    chats,
    common,
    drafts,
    fallback,
    group_events,
    options,
    posts,
    schedule,
    settings,
)
from bot.middlewares.access import AdminOnlyMiddleware
from bot.middlewares.album import AlbumMiddleware


def build_dispatcher(app: App, *, album_latency: float = 0.8) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["app"] = app

    access = AdminOnlyMiddleware(app.admin_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.message.outer_middleware(AlbumMiddleware(latency=album_latency))
    dp.callback_query.middleware(CallbackAnswerMiddleware())

    # Порядок важен: команды и навигация — первыми (они сбрасывают ввод), fallback — последним
    dp.include_routers(
        common.router,
        chat_member.router,
        group_events.router,
        chats.router,
        campaigns.router,
        posts.router,
        schedule.router,
        options.router,
        drafts.router,
        settings.router,
        fallback.router,
    )
    return dp
