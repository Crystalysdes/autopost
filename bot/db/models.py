"""Модели базы данных.

Время везде хранится как UTC epoch-секунды (INTEGER): SQLite не хранит часовые пояса.
Рассылки и черновики живут в одной таблице (поле ``kind``). Одна рассылка публикуется
в несколько чатов: связь и состояние отправки по каждому чату — в таблице ``campaign_chats``.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]


def now_ts() -> int:
    return int(time.time())


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    type: Mapped[str] = mapped_column(String(16), default="supergroup")
    is_forum: Mapped[bool] = mapped_column(default=False)
    # active — работает; pending — бота добавил не админ, ждёт подтверждения; left — бота нет в чате
    status: Mapped[str] = mapped_column(String(16), default="active")
    is_admin: Mapped[bool] = mapped_column(default=False)
    can_post: Mapped[bool] = mapped_column(default=False)
    can_pin: Mapped[bool] = mapped_column(default=False)
    can_delete: Mapped[bool | None] = mapped_column(default=None)  # удалять чужие сообщения (для защиты)
    can_invite: Mapped[bool | None] = mapped_column(default=None)  # создавать ссылки-приглашения
    can_restrict: Mapped[bool | None] = mapped_column(default=None)  # банить и мутить (команды в группах)
    invite_link: Mapped[str | None] = mapped_column(String(128), default=None)  # закрытый канал: ссылка
    # Защита группы. Антиспам: None — включён со всеми правилами, иначе {"on": bool, "<правило>": bool}.
    spam_filter: Mapped[dict[str, bool] | None] = mapped_column(JSON, default=None)
    # Обязательная подписка: None — общие каналы из настроек, "own" — свои (sub_channels), "off" — выключена
    sub_mode: Mapped[str | None] = mapped_column(String(8), default=None)
    sub_channels: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    # Автоприём заявок на вступление: None — как в общих настройках, True/False — выбрано для этого чата
    auto_approve: Mapped[bool | None] = mapped_column(default=None)
    # Скрытые админы только этого чата: [{"id", "username", "name"}] (см. bot/services/trusted.py)
    trusted: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    added_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_ts: Mapped[int] = mapped_column(default=now_ts)
    updated_ts: Mapped[int] = mapped_column(default=now_ts, onupdate=now_ts)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # "campaign" — рассылка, "draft" — черновик. NULL бывает только в базах до миграции v2.
    kind: Mapped[str | None] = mapped_column(String(8), default="campaign")
    # Устарело: до v2 рассылка принадлежала одному чату. Теперь всегда NULL — чаты в campaign_chats.
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True, default=None)
    source_draft_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True, default=None
    )
    name: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=False)

    # Расписание
    times: Mapped[list[str]] = mapped_column(JSON, default=list)
    weekdays: Mapped[list[int]] = mapped_column(JSON, default=lambda: list(ALL_WEEKDAYS))
    jitter_min: Mapped[int] = mapped_column(default=0)
    start_date: Mapped[str | None] = mapped_column(String(10), default=None)
    end_date: Mapped[str | None] = mapped_column(String(10), default=None)

    # Опции
    rotation: Mapped[str] = mapped_column(String(12), default="sequential")
    silent: Mapped[bool] = mapped_column(default=False)
    protect: Mapped[bool] = mapped_column(default=False)
    pin: Mapped[bool] = mapped_column(default=False)
    delete_prev: Mapped[bool] = mapped_column(default=False)
    thread_id: Mapped[int | None] = mapped_column(default=None)  # устарело: тема теперь у каждого чата своя

    # Состояние
    next_slot_ts: Mapped[int | None] = mapped_column(default=None)
    # Базовый слот последнего запуска: от него считается следующий при пересчёте расписания
    last_slot_ts: Mapped[int | None] = mapped_column(default=None)
    next_run_ts: Mapped[int | None] = mapped_column(default=None, index=True)
    last_post_id: Mapped[int | None] = mapped_column(default=None)
    # Проблемы уровня рассылки (например, «нет постов»); ошибки отправки — у каждого чата свои
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Устарело (до v2 — состояние единственного чата), перенесено в campaign_chats
    last_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    last_sent_ts: Mapped[int | None] = mapped_column(default=None)
    sent_count: Mapped[int] = mapped_column(default=0)
    fail_count: Mapped[int] = mapped_column(default=0)

    created_ts: Mapped[int] = mapped_column(default=now_ts)
    updated_ts: Mapped[int] = mapped_column(default=now_ts, onupdate=now_ts)

    @property
    def is_draft(self) -> bool:
        return self.kind == "draft"


class CampaignChat(Base):
    """Чат, в который публикуется рассылка, и состояние отправки именно в него."""

    __tablename__ = "campaign_chats"
    __table_args__ = (UniqueConstraint("campaign_id", "chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[int | None] = mapped_column(default=None)  # тема форума в этом чате
    last_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    last_sent_ts: Mapped[int | None] = mapped_column(default=None)
    sent_count: Mapped[int] = mapped_column(default=0)
    fail_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Автопауза после серии ошибок в этом чате (остальные чаты рассылки работают)
    paused: Mapped[bool] = mapped_column(default=False)
    created_ts: Mapped[int] = mapped_column(default=now_ts)


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(default=0)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    buttons: Mapped[list[Any] | None] = mapped_column(JSON, default=None)
    # Откуда пересылать: {"chat_id": ..., "message_ids": [...]} — сообщение в чате админа с ботом
    forward_from: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    send_mode: Mapped[str] = mapped_column(String(8), default="copy")
    created_ts: Mapped[int] = mapped_column(default=now_ts)


class SendLog(Base):
    __tablename__ = "send_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True, default=None
    )
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="SET NULL"), default=None)
    post_id: Mapped[int | None] = mapped_column(default=None)
    # sent | failed | skipped
    status: Mapped[str] = mapped_column(String(8))
    manual: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    message_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None)
    ts: Mapped[int] = mapped_column(default=now_ts, index=True)


class ModerationLog(Base):
    """Сообщение, удалённое защитой группы. Хранится две недели — для статистики и проверки ошибок."""

    __tablename__ = "moderation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    user_name: Mapped[str] = mapped_column(String(128), default="")
    # links | bots | forwards | channels | words | contacts | names | service — антиспам; sub — нет подписки
    reason: Mapped[str] = mapped_column(String(16))
    snippet: Mapped[str | None] = mapped_column(Text, default=None)
    ts: Mapped[int] = mapped_column(default=now_ts, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
