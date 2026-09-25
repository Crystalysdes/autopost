"""Защита групп: антиспам и обязательная подписка на каналы.

Сообщения проверяются в памяти. К Telegram бот обращается, только когда без этого не решить
(кто админ чата, кто скрывается за @ником, подписан ли человек), и запоминает ответы на время.

Порядок для каждого сообщения:
1. служебные сообщения: вход/выход удаляются (если включено), остальные не трогаются;
2. сразу пропускаются админы бота, анонимные админы чата и посты привязанного канала;
3. антиспам: если сообщение похоже на спам и автор не админ чата — молча удалить;
4. подписка: если человек не подписан на нужные каналы и не админ чата — удалить и показать подсказку.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message

from bot.app import NO_PREVIEW, App
from bot.db.models import Chat
from bot.services import chats as chat_service
from bot.services import spam
from bot.services.errors import humanize
from bot.services.rights import enum_text
from bot.ui import keyboards, texts

logger = logging.getLogger(__name__)

GROUP_TYPES = ("group", "supergroup")
TELEGRAM_SERVICE_ID = 777000  # служебный аккаунт Telegram (пересылает посты канала в группу обсуждения)
SUBSCRIBED = {"creator", "administrator", "member"}

CHAT_TTL = 60  # настройки чата (правки из бота сбрасывают кэш сразу)
ADMINS_TTL = 600
LINKED_TTL = 3600
MENTION_TTL = 24 * 3600
SUB_YES_TTL = 15 * 60
SUB_NO_TTL = 15  # короткий: человек подписался — через несколько секунд уже может писать
ALBUM_TTL = 120
NOTICE_LIFETIME = 60  # подсказка неподписанному удаляется сама
NOTICE_INTERVAL = 120  # не чаще одной подсказки на человека в чате
BACKLOG_SECONDS = 120  # сообщения старше (пришли, пока бот был выключен) удаляются без подсказки
PROBLEM_INTERVAL = 24 * 3600  # одинаковые предупреждения админам — не чаще раза в сутки
MAX_MENTION_CHECKS = 5  # @ников на одно сообщение, которые проверяем через Telegram
RETRY_AFTER_LIMIT = 30

_MISSING = object()


class TTLCache:
    """Словарь, где у каждой записи своё время жизни; при переполнении старое выбрасывается."""

    def __init__(self, clock: Callable[[], float], limit: int = 20000) -> None:
        self._clock = clock
        self._limit = limit
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        item = self._items.get(key)
        if item is None or item[0] <= self._clock():
            return _MISSING
        return item[1]

    def set(self, key: Any, value: Any, ttl: float) -> None:
        if key not in self._items and len(self._items) >= self._limit:
            now = self._clock()
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            for stale in list(self._items)[: max(0, len(self._items) - self._limit // 2)]:
                del self._items[stale]
        self._items[key] = (self._clock() + ttl, value)

    def pop(self, key: Any) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()


@dataclass(frozen=True)
class ChatInfo:
    """Настройки защиты группы, собранные для быстрой проверки сообщений."""

    id: int
    tg_id: int
    title: str
    rules: spam.SpamRules
    allow: spam.Allowlist
    channels: tuple[int, ...]  # id записей каналов, на которые нужна подписка


@dataclass
class _Album:
    ids: set[int]
    reason: str | None = None


class Moderator:
    def __init__(
        self,
        app: App,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.wall = wall
        self.notice_lifetime: float = NOTICE_LIFETIME
        self._chats = TTLCache(clock)
        self._admins = TTLCache(clock)
        self._linked = TTLCache(clock)
        self._mentions = TTLCache(clock)
        self._subs = TTLCache(clock)
        self._albums = TTLCache(clock)
        self._notices = TTLCache(clock)
        self._problems = TTLCache(clock)
        self._channels = TTLCache(clock)
        self._words: tuple[tuple[str, ...], list[Any]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ настройки

    def forget(self, tg_id: int | None = None) -> None:
        """Настройки защиты поменялись: перечитать их из базы при следующем сообщении."""
        if tg_id is None:
            self._chats.clear()
            self._channels.clear()
        else:
            self._chats.pop(tg_id)

    async def chat_info(self, tg_id: int) -> ChatInfo | None:
        cached = self._chats.get(tg_id)
        if cached is not _MISSING:
            return cached
        chat = await self.app.repo.get_chat_by_tg(tg_id)
        info = None
        if chat is not None and chat.status == "active" and chat.type in GROUP_TYPES:
            info = ChatInfo(
                id=chat.id,
                tg_id=chat.tg_id,
                title=chat.title,
                rules=spam.SpamRules.of(chat.spam_filter),
                allow=spam.build_allowlist(self.app.settings.spam_allow, [chat.username, self.app.bot_username]),
                channels=required_channels(chat, self.app.settings.sub_channels),
            )
        self._chats.set(tg_id, info, CHAT_TTL)
        return info

    def stop_words(self) -> list[Any]:
        words = tuple(self.app.settings.spam_words or spam.DEFAULT_STOP_WORDS)
        if self._words is None or self._words[0] != words:
            self._words = (words, spam.compile_stop_words(words))
        return self._words[1]

    # ------------------------------------------------------------------ проверка

    async def handle(self, message: Message, *, edited: bool = False) -> None:
        info = await self.chat_info(message.chat.id)
        if info is None:
            return
        if spam.is_join_leave(message):
            if not edited and info.rules.enabled("service"):
                await self._delete(info, [message.message_id])
            return
        if not spam.has_user_content(message) or self._always_allowed(message):
            return

        album = self._album(info, message)
        if album is not None and album.reason:  # другая часть этого альбома уже признана спамом
            await self._delete(info, [message.message_id])
            return

        reason = await self._spam_reason(info, message)
        if reason is None and self._needs_subscription(info, message, edited):
            user_id = message.from_user.id if message.from_user else 0
            if await self.missing_channels(user_id, info.channels):
                reason = "sub"
        if reason is None or await self._is_chat_admin(info, message):
            return

        ids = {message.message_id}
        if album is not None:
            if album.reason:  # пока шла проверка, альбом уже удалили по другой части
                await self._delete(info, [message.message_id])
                return
            album.reason = reason
            ids |= album.ids
        if not await self._delete(info, sorted(ids)):
            return
        user = message.from_user
        name = message.sender_chat.title if message.sender_chat else (user.full_name if user else "")
        logger.info("Защита: удалено сообщение в «%s» от %s (%s)", info.title, name, reason)
        await self.app.repo.log_moderation(
            info.id,
            user_id=user.id if user and not message.sender_chat else None,
            user_name=name or "",
            reason=reason,
            snippet=spam.snippet(message),
            now=int(self.wall()),
        )
        if reason == "sub" and user is not None and not self._is_backlog(message):
            await self._notice(info, message)

    def _always_allowed(self, message: Message) -> bool:
        if message.is_automatic_forward:
            return True
        if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
            return True  # анонимный админ пишет от имени группы
        user = message.from_user
        return user is not None and (user.id in self.app.admin_ids or user.id == TELEGRAM_SERVICE_ID)

    @staticmethod
    def _needs_subscription(info: ChatInfo, message: Message, edited: bool) -> bool:
        """Подписку проверяем у людей и только в новых сообщениях (правки старых не трогаем)."""
        return bool(info.channels) and not edited and message.sender_chat is None and message.from_user is not None

    def _is_backlog(self, message: Message) -> bool:
        sent = message.date.timestamp() if isinstance(message.date, datetime) else float(message.date or 0)
        return self.wall() - sent > BACKLOG_SECONDS

    def _album(self, info: ChatInfo, message: Message) -> _Album | None:
        if not message.media_group_id:
            return None
        key = (info.tg_id, message.media_group_id)
        album = self._albums.get(key)
        if album is _MISSING:
            album = _Album(ids=set())
            self._albums.set(key, album, ALBUM_TTL)
        album.ids.add(message.message_id)
        return album

    async def _spam_reason(self, info: ChatInfo, message: Message) -> str | None:
        if not info.rules.on:
            return None
        verdict = spam.detect(message, info.rules, stop_words=self.stop_words(), allow=info.allow)
        if verdict.reason is None and verdict.mentions and await self._any_public_chat(verdict.mentions):
            return "bots"
        return verdict.reason

    async def _any_public_chat(self, names: Sequence[str]) -> bool:
        """Есть ли среди @ников канал или группа. Люди через getChat не находятся — их не трогаем."""
        for name in names[:MAX_MENTION_CHECKS]:
            kind = self._mentions.get(name)
            if kind is _MISSING:
                try:
                    found = await self.app.bot.get_chat(f"@{name}")
                    kind = "chat" if enum_text(found.type) in ("channel", "group", "supergroup") else "user"
                except TelegramBadRequest:
                    kind = "user"  # «chat not found» — это человек или такого ника нет
                except TelegramAPIError as error:
                    logger.info("Не удалось проверить @%s: %s", name, error)
                    continue
                self._mentions.set(name, kind, MENTION_TTL)
            if kind == "chat":
                return True
        return False

    async def _is_chat_admin(self, info: ChatInfo, message: Message) -> bool:
        """Админы чата и привязанный канал не проверяются. Если узнать не вышло — не удаляем."""
        if message.sender_chat is not None:
            return message.sender_chat.id == await self._linked_chat_id(info)
        user = message.from_user
        if user is None:
            return False
        admins = self._admins.get(info.tg_id)
        if admins is _MISSING:
            try:
                members = await self.app.bot.get_chat_administrators(info.tg_id)
                admins = frozenset(member.user.id for member in members)
                self._admins.set(info.tg_id, admins, ADMINS_TTL)
            except TelegramAPIError as error:
                logger.warning("Не удалось получить админов «%s»: %s", info.title, error)
                self._admins.set(info.tg_id, None, 60)
                admins = None
        return admins is None or user.id in admins

    async def _linked_chat_id(self, info: ChatInfo) -> int | None:
        linked = self._linked.get(info.tg_id)
        if linked is _MISSING:
            try:
                linked = (await self.app.bot.get_chat(info.tg_id)).linked_chat_id
            except TelegramAPIError as error:
                logger.info("Не удалось узнать привязанный канал «%s»: %s", info.title, error)
                return None
            self._linked.set(info.tg_id, linked, LINKED_TTL)
        return linked

    # ------------------------------------------------------------------ подписка

    async def channels_of(self, ids: Iterable[int]) -> list[Chat]:
        result = []
        for channel_id in ids:
            channel = self._channels.get(channel_id)
            if channel is _MISSING:
                channel = await self.app.repo.get_chat(channel_id)
                self._channels.set(channel_id, channel, CHAT_TTL)
            if channel is not None and channel.type == "channel":
                result.append(channel)
        return result

    async def missing_channels(self, user_id: int, channel_ids: Iterable[int], *, fresh: bool = False) -> list[Chat]:
        """Каналы, на которые человек не подписан. Канал, который бот не может проверить, пропускается:
        лучше пропустить неподписанного, чем из-за настройки не дать писать всем."""
        missing = []
        for channel in await self.channels_of(channel_ids):
            if channel.status != "active":
                await self._channel_problem(channel, "бота нет в канале")
                continue
            key = (channel.tg_id, user_id)
            subscribed = _MISSING if fresh else self._subs.get(key)
            if subscribed is _MISSING:
                try:
                    member = await self.app.bot.get_chat_member(channel.tg_id, user_id)
                except TelegramBadRequest as error:
                    lowered = error.message.lower()
                    if "user not found" in lowered or "participant_id_invalid" in lowered:
                        subscribed = False
                    else:
                        await self._channel_problem(channel, humanize(error))
                        continue
                except TelegramForbiddenError as error:
                    await self._channel_problem(channel, humanize(error))
                    continue
                except TelegramAPIError as error:  # нет связи и т. п. — не повод беспокоить админов
                    logger.info("Не удалось проверить подписку на «%s»: %s", channel.title, error)
                    continue
                else:
                    status = enum_text(member.status)
                    subscribed = status in SUBSCRIBED or (status == "restricted" and bool(member.is_member))
                self._subs.set(key, subscribed, SUB_YES_TTL if subscribed else SUB_NO_TTL)
            if not subscribed:
                missing.append(channel)
        return missing

    async def channel_link(self, channel: Chat) -> str | None:
        """Ссылка для кнопки «📢 Подписаться»: у публичного канала — t.me/ник, у закрытого —
        ссылка-приглашение (создаётся один раз и хранится в базе)."""
        if channel.username:
            return f"https://t.me/{channel.username}"
        if channel.invite_link:
            return channel.invite_link
        try:
            link = await self.app.bot.create_chat_invite_link(channel.tg_id, name="Подписка для чатов")
        except TelegramAPIError as error:
            logger.info("Не удалось создать ссылку на «%s»: %s", channel.title, error)
            await self._problem(
                (channel.tg_id, "link"), texts.no_invite_link_text(channel.title), keyboards.open_sub_settings()
            )
            return None
        await self.app.repo.update_chat(channel.id, invite_link=link.invite_link)
        self._channels.pop(channel.id)
        return link.invite_link

    async def _notice(self, info: ChatInfo, message: Message) -> None:
        user = message.from_user
        if user is None:
            return
        key = (info.tg_id, user.id)
        if self._notices.get(key) is not _MISSING:
            return
        self._notices.set(key, True, NOTICE_INTERVAL)
        missing = await self.missing_channels(user.id, info.channels)
        if not missing:
            return
        buttons = [(channel.title, await self.channel_link(channel)) for channel in missing]
        name = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
        try:
            sent = await self.app.bot.send_message(
                info.tg_id,
                texts.sub_notice_text(name, [channel.title for channel in missing]),
                reply_markup=keyboards.sub_notice(buttons, user.id),
                message_thread_id=message.message_thread_id if message.is_topic_message else None,
                disable_notification=True,
                link_preview_options=NO_PREVIEW,
            )
        except TelegramAPIError as error:
            logger.info("Не удалось показать подсказку о подписке в «%s»: %s", info.title, error)
            return
        self._later(self._delete_quietly(info.tg_id, sent.message_id, self.notice_lifetime))

    async def _delete_quietly(self, tg_id: int, message_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(TelegramAPIError):
            await self.app.bot.delete_message(tg_id, message_id)

    def _later(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------ удаление

    async def _delete(self, info: ChatInfo, ids: list[int]) -> bool:
        for attempt in (1, 2):
            try:
                if len(ids) == 1:
                    await self.app.bot.delete_message(info.tg_id, ids[0])
                else:
                    await self.app.bot.delete_messages(info.tg_id, ids)
                return True
            except TelegramRetryAfter as error:
                if attempt == 1 and error.retry_after <= RETRY_AFTER_LIMIT:
                    await asyncio.sleep(error.retry_after)
                    continue
                return False
            except TelegramBadRequest as error:
                text = error.message.lower()
                if "not found" in text:  # уже удалено
                    return True
                if "can't be deleted" in text or "rights" in text:
                    await self._no_rights(info)
                else:
                    logger.warning("Не удалось удалить сообщение в «%s»: %s", info.title, error.message)
                return False
            except TelegramForbiddenError:
                return False  # бота уже нет в чате
            except TelegramAPIError as error:
                logger.warning("Не удалось удалить сообщение в «%s»: %s", info.title, error)
                return False
        return False

    async def _no_rights(self, info: ChatInfo) -> None:
        if not await self._problem(
            (info.tg_id, "rights"), texts.no_delete_right_text(info.title), keyboards.open_chat(info.id)
        ):
            return
        chat = await self.app.repo.get_chat(info.id)
        if chat is not None:
            await chat_service.refresh_chat(self.app, chat)
            self.forget(info.tg_id)

    async def _problem(self, key: tuple[Any, ...], text: str, markup: Any = None) -> bool:
        """Предупреждение админам — не чаще раза в сутки на одну проблему. True — отправлено."""
        if self._problems.get(key) is not _MISSING:
            return False
        self._problems.set(key, True, PROBLEM_INTERVAL)
        await self.app.notify(text, markup)
        return True

    async def _channel_problem(self, channel: Chat, reason: str) -> None:
        await self._problem(
            (channel.tg_id, "sub"), texts.sub_check_failed_text(channel.title, reason), keyboards.open_sub_settings()
        )


def required_channels(chat: Chat, common: Sequence[int]) -> tuple[int, ...]:
    """На какие каналы нужна подписка в чате: общие из настроек, свои или никакие."""
    if chat.sub_mode == "off":
        return ()
    if chat.sub_mode == "own":
        return tuple(chat.sub_channels or ())
    return tuple(common)
