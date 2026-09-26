"""Общий контейнер зависимостей, доступный во всех обработчиках как ``app``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from bot.config import Config
from bot.db.base import Database
from bot.db.repo import Repo
from bot.services import trusted

if TYPE_CHECKING:
    from bot.services.moderation import Moderator
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
        # Защита групп (списки хранятся в settings как JSON)
        self.spam_words: list[str] | None = None  # None — стандартный список из bot/services/spam.py
        self.spam_allow: list[str] = []  # разрешённые ссылки: домены и @имена
        self.sub_channels: list[int] = []  # общие каналы обязательной подписки (id записей чатов)
        # Автоприём заявок в чатах, где он не выбран отдельно (в том числе в новых)
        self.join_auto = True
        # Скрытые админы во всех группах (см. bot/services/trusted.py)
        self.trusted: list[dict[str, Any]] = []

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
        elif key == "join_auto":
            self.join_auto = value == "1"
        elif key in ("spam_words", "spam_allow", "sub_channels", "trusted"):
            self._apply_list(key, value)

    def _apply_list(self, key: str, value: str) -> None:
        try:
            data = json.loads(value)
        except ValueError:
            data = None
        if not isinstance(data, list):
            logger.warning("Настройка %s в базе повреждена — использую значение по умолчанию", key)
            return
        if key == "spam_words":
            self.spam_words = [str(item) for item in data]
        elif key == "spam_allow":
            self.spam_allow = [str(item) for item in data]
        elif key == "trusted":
            self.trusted = trusted.clean_list(data)
        else:
            self.sub_channels = [int(item) for item in data if isinstance(item, int)]

    async def load(self, repo: Repo) -> None:
        for key, value in (await repo.load_settings()).items():
            self._apply(key, value)

    async def set(self, repo: Repo, key: str, value: str | bool) -> None:
        text = ("1" if value else "0") if isinstance(value, bool) else value
        await repo.save_setting(key, text)
        self._apply(key, text)

    async def set_list(self, repo: Repo, key: str, value: list[Any]) -> None:
        await self.set(repo, key, json.dumps(value, ensure_ascii=False))

    async def reset_spam_words(self, repo: Repo) -> None:
        """Вернуть стандартные стоп-слова (и получать их обновления вместе с ботом)."""
        await repo.delete_setting("spam_words")
        self.spam_words = None


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
    moderator: Moderator | None = field(default=None, repr=False)
    # Админы, которым уже установлено меню команд
    commands_ready: set[int] = field(default_factory=set)
    # Из какого чата админ открыл рассылку: туда ведёт «« Назад» (после перезапуска — к списку рассылок)
    origins: dict[tuple[int, int], int] = field(default_factory=dict)
    # Люди, выбранные кнопкой «🕶 Добавить скрытого админа», пока админ не решил, куда их добавить
    picked: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def admin_ids(self) -> tuple[int, ...]:
        return tuple(self.config.admin_ids)

    def origin(self, user_id: int | None, campaign_id: int) -> int:
        return self.origins.get((user_id or 0, campaign_id), 0)

    def remember_origin(self, user_id: int | None, campaign_id: int, chat_id: int) -> None:
        """chat_id > 0 — рассылку открыли из этого чата, иначе — из списков."""
        key = (user_id or 0, campaign_id)
        if chat_id > 0:
            self.origins[key] = chat_id
        else:
            self.origins.pop(key, None)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.admin_ids

    async def notify(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Сообщение всем админам. Ошибки (например, админ ещё не открыл бота) только логируются."""
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
            except TelegramAPIError as error:
                logger.warning("Не удалось отправить уведомление админу %s: %s", admin_id, error)
