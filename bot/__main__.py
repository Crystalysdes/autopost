"""Запуск: python -m bot"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import IO

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import User
from aiogram.utils.token import TokenValidationError
from pydantic import ValidationError

from bot.app import App, AppSettings
from bot.config import Config
from bot.db.base import Database
from bot.db.repo import Repo
from bot.handlers.common import setup_commands
from bot.services.chats import refresh_all
from bot.services.scheduler import Scheduler
from bot.setup import build_dispatcher

logger = logging.getLogger("bot")


def acquire_instance_lock(db_path: Path) -> IO[str]:
    """Второй экземпляр на той же базе привёл бы к двойным публикациям — не даём его запустить."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(db_path.with_name(db_path.name + ".lock"), "w")  # noqa: SIM115 - держим открытым всё время работы
    try:
        import fcntl
    except ImportError:  # Windows: блокировка недоступна
        return handle
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Бот уже запущен с этой базой данных (занят файл блокировки). Остановите второй экземпляр.")
    return handle


async def _get_me_with_retries(bot: Bot, attempts: int = 8) -> User:
    """После перезагрузки сервера сеть может подняться не сразу — пробуем несколько раз."""
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            return await bot.get_me()
        except TelegramNetworkError as error:
            if attempt == attempts:
                raise
            logger.warning("Нет связи с Telegram (%s), повтор через %.0f с", error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    raise AssertionError("unreachable")


async def run(config: Config) -> None:
    try:
        bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except TokenValidationError:
        sys.exit("BOT_TOKEN в .env записан неправильно. Скопируйте токен из @BotFather целиком.")
    try:
        me = await _get_me_with_retries(bot)
    except TelegramUnauthorizedError:
        await bot.session.close()
        sys.exit("Telegram не принял BOT_TOKEN — проверьте токен в .env (его выдаёт @BotFather).")
    except TelegramNetworkError as error:
        await bot.session.close()
        sys.exit(f"Нет связи с Telegram: {error}. Проверьте интернет на сервере.")

    db = Database(config.db_path)
    await db.init()
    repo = Repo(db)

    settings = AppSettings(config.timezone)
    await settings.load(repo)
    app = App(
        config=config,
        db=db,
        repo=repo,
        bot=bot,
        settings=settings,
        bot_id=me.id,
        bot_username=me.username or "",
    )
    app.scheduler = Scheduler(app)
    dp = build_dispatcher(app)
    app.commands_ready = await setup_commands(bot, config.admin_ids)

    await app.scheduler.reschedule(only_missing=True)
    scheduler_task = asyncio.create_task(app.scheduler.run())
    refresh_task = asyncio.create_task(refresh_all(app))
    admins = ", ".join(map(str, config.admin_ids))
    logger.info("Бот @%s запущен. Админы: %s. Часовой пояс: %s", me.username, admins, settings.timezone)
    try:
        # Апдейты, накопившиеся за время простоя, не сбрасываем: среди них могут быть
        # добавления бота в новые чаты.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        app.scheduler.stop()
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(scheduler_task, refresh_task, return_exceptions=True)
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        config = Config()
    except ValidationError as error:
        problems = []
        for item in error.errors():
            field = str(item["loc"][0]).upper() if item["loc"] else ""
            problems.append(f"{field}: {item['msg']}" if field else item["msg"].removeprefix("Value error, "))
        sys.exit("Проверьте настройки в .env (см. .env.example):\n  " + "\n  ".join(problems))

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # aiogram пишет строку на каждый апдейт — в активных группах это шум
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    lock = acquire_instance_lock(config.db_path)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()


if __name__ == "__main__":
    main()
