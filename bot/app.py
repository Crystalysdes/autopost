"""Общий контейнер зависимостей, доступный во всех обработчиках как ``app``."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from bot.config import Config
from bot.db.base import Database
from bot.db.repo import Repo

if TYPE_CHECKING:
    from bot.services.scheduler import Scheduler

logger = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class AppSettings:
    """Настройки, которые меняются из бота. Хранятся в таблице settings, читаются из памяти."""

    def __init__(self, default_timezone: str) -> None:
        self.timezone = default_timezone
        self.paused_all = False
        self.notify_errors = True
        self._tz = ZoneInfo(default_timezone)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def _apply(self, key: str, value: str) -> None:
        if key == "timezone":
            try:
                self._tz = ZoneInfo(value)
                self.timezone = value
            except (ValueError, KeyError):
                logger.warning("В базе неизвестный часовой пояс %r — оставляю %s", value, self.timezone)
        elif key == "paused_all":
            self.paused_all = value == "1"
        elif key == "notify_errors":
            self.notify_errors = value == "1"

    async def load(self, repo: Repo) -> None:
        for key, value in (await repo.load_settings()).items():
            self._apply(key, value)

    async def set(self, repo: Repo, key: str, value: str | bool) -> None:
        text = ("1" if value else "0") if isinstance(value, bool) else value
        await repo.save_setting(key, text)
        self._apply(key, text)


@dataclass
class App:
    config: Config
    db: Database
    repo: Repo
    bot: Bot
    settings: AppSettings
    bot_id: int = 0
    bot_username: str = ""
    scheduler: Scheduler | None = field(default=None, repr=False)
    # Админы, которым уже установлено меню команд
    commands_ready: set[int] = field(default_factory=set)

    @property
    def admin_ids(self) -> tuple[int, ...]:
        return tuple(self.config.admin_ids)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.admin_ids

    async def notify(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Сообщение всем админам. Ошибки (например, админ ещё не открыл бота) только логируются."""
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
            except TelegramAPIError as error:
                logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, error)
