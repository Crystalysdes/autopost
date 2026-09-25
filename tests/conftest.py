"""Общие фикстуры: временная БД и «фейковый Telegram», который записывает запросы бота."""

from __future__ import annotations

import itertools
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    CreateChatInviteLink,
    EditMessageText,
    ForwardMessages,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    GetMe,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendLivePhoto,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendSticker,
    SendVideo,
    SendVideoNote,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import (
    AcceptedGiftTypes,
    Chat,
    ChatFullInfo,
    ChatInviteLink,
    ChatMemberAdministrator,
    ChatMemberLeft,
    Message,
    MessageId,
    User,
)

from bot.app import App, AppSettings
from bot.config import Config
from bot.db.base import Database
from bot.db.repo import Repo

OWNER_ID = 1000
SECOND_ADMIN_ID = 1001
BOT_ID = 4242
STRANGER_ID = 2000

_SEND_WITH_CAPTION = (SendPhoto, SendVideo, SendAnimation, SendDocument, SendAudio, SendVoice, SendLivePhoto)


class RecordingSession(BaseSession):
    """Вместо HTTP-запросов записывает вызовы и возвращает правдоподобные ответы.

    Для конкретного метода можно заранее положить ответ или исключение:
    session.queue(SendMessage, TelegramForbiddenError(...)).
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._queued: dict[type, list[Any]] = defaultdict(list)
        self._ids = itertools.count(100)
        self.handlers: dict[type, Callable[[TelegramMethod[Any]], Any]] = {}

    def queue(self, method_type: type, result: Any) -> None:
        self._queued[method_type].append(result)

    def calls(self, method_type: type) -> list[Any]:
        return [r for r in self.requests if isinstance(r, method_type)]

    def clear(self) -> None:
        self.requests.clear()

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover - не используется
        raise NotImplementedError
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.requests.append(method)
        queued = self._queued.get(type(method))
        if queued:
            result = queued.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        handler = self.handlers.get(type(method))
        if handler is not None:
            return handler(method)
        return self._default(method)

    # -------------------------------------------------------------- ответы по умолчанию

    def _chat(self, chat_id: Any) -> Chat:
        chat_id = int(chat_id)
        return Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")

    def _message(self, chat_id: Any, **fields: Any) -> Message:
        return Message(message_id=next(self._ids), date=datetime.now(), chat=self._chat(chat_id), **fields)

    def _default(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Autopost", username="autopost_test_bot")
        if isinstance(method, SendMessage):
            return self._message(method.chat_id, text=method.text, entities=method.entities)
        if isinstance(method, _SEND_WITH_CAPTION):
            return self._message(
                method.chat_id,
                caption=getattr(method, "caption", None),
                caption_entities=getattr(method, "caption_entities", None),
            )
        if isinstance(method, (SendSticker, SendVideoNote)):
            return self._message(method.chat_id)
        if isinstance(method, SendMediaGroup):
            return [
                self._message(method.chat_id, caption=m.caption, caption_entities=m.caption_entities)
                for m in method.media
            ]
        if isinstance(method, ForwardMessages):
            return [MessageId(message_id=next(self._ids)) for _ in method.message_ids]
        if isinstance(method, EditMessageText):
            return self._message(method.chat_id, text=method.text)
        if isinstance(method, GetChat):
            if isinstance(method.chat_id, str):  # «@ник» человека: getChat их не находит
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            return chat_info(int(method.chat_id))
        if isinstance(method, GetChatMember):
            return admin_member()
        if isinstance(method, GetChatAdministrators):
            return []
        if isinstance(method, CreateChatInviteLink):  # каждый вызов — новая ссылка, как в Telegram
            return ChatInviteLink(
                invite_link=f"https://t.me/+invite{method.chat_id}_{next(self._ids)}",
                creator=User(id=BOT_ID, is_bot=True, first_name="Autopost"),
                creates_join_request=False,
                is_primary=False,
                is_revoked=False,
                name=method.name,
                member_limit=method.member_limit,
            )
        return True


def chat_info(tg_id: int, title: str = "Тестовый чат", chat_type: str = "supergroup", **extra: Any) -> ChatFullInfo:
    return ChatFullInfo(
        id=tg_id,
        type=chat_type,
        title=title,
        accent_color_id=0,
        max_reaction_count=11,
        accepted_gift_types=AcceptedGiftTypes(
            unlimited_gifts=False,
            limited_gifts=False,
            unique_gifts=False,
            premium_subscription=False,
            gifts_from_channels=False,
        ),
        **extra,
    )


def admin_member(**rights: bool) -> ChatMemberAdministrator:
    base = {
        "can_be_edited": False,
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": True,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_send_welcome_messages": False,
        "can_pin_messages": True,
        "can_post_messages": True,
        "can_edit_messages": True,
    }
    base.update(rights)
    return ChatMemberAdministrator(user=User(id=BOT_ID, is_bot=True, first_name="Autopost"), **base)


def left_member() -> ChatMemberLeft:
    return ChatMemberLeft(user=User(id=BOT_ID, is_bot=True, first_name="Autopost"))


class FakeClock:
    def __init__(self, start: float | None = None) -> None:
        self.value = float(start if start is not None else time.time())

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
async def db(tmp_path) -> AsyncGenerator[Database, None]:
    database = Database(tmp_path / "test.db")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def repo(db: Database) -> Repo:
    return Repo(db)


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
async def bot(session: RecordingSession) -> AsyncGenerator[Bot, None]:
    instance = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    yield instance


@pytest.fixture
async def app(tmp_path, db: Database, repo: Repo, bot: Bot) -> App:
    config = Config(
        _env_file=None,
        bot_token="42:TEST",
        admin_ids=[OWNER_ID, SECOND_ADMIN_ID],
        timezone="Europe/Moscow",
        db_path=tmp_path / "test.db",
    )
    settings = AppSettings(config.timezone)
    return App(
        config=config,
        db=db,
        repo=repo,
        bot=bot,
        settings=settings,
        bot_id=BOT_ID,
        bot_username="autopost_test_bot",
    )
