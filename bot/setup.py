"""Сборка диспетчера: middleware и роутеры в нужном порядке."""

from __future__ import annotations

from aiogram import Dispatcher
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
    library,
    moderation,
    options,
    posts,
    protection,
    schedule,
    settings,
    targets,
)
from bot.middlewares.access import AdminOnlyMiddleware
from bot.middlewares.album import AlbumMiddleware
from bot.middlewares.taps import DoubleTapMiddleware, ResetInputMiddleware
from bot.storage import LeanMemoryStorage


def build_dispatcher(app: App, *, album_latency: float = 0.8, double_tap_window: float = 1.5) -> Dispatcher:
    dp = Dispatcher(storage=LeanMemoryStorage())
    dp["app"] = app

    access = AdminOnlyMiddleware(app.admin_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.callback_query.outer_middleware(DoubleTapMiddleware(window=double_tap_window))
    dp.callback_query.outer_middleware(ResetInputMiddleware())
    dp.message.outer_middleware(AlbumMiddleware(latency=album_latency))
    dp.callback_query.middleware(CallbackAnswerMiddleware())

    # Порядок важен: команды и навигация — первыми (они сбрасывают ввод), fallback — последним
    dp.include_routers(
        common.router,
        chat_member.router,
        group_events.router,
        moderation.router,
        chats.router,
        campaigns.router,
        posts.router,
        schedule.router,
        options.router,
        targets.router,
        library.router,
        protection.router,
        drafts.router,
        settings.router,
        fallback.router,
    )
    return dp
