"""Доступ к данным. Каждый метод — отдельная короткая транзакция.

Объекты, которые возвращают методы, «отсоединены» от сессии (expire_on_commit=False):
их можно читать, но изменения нужно сохранять через методы репозитория.

Рассылка (kind="campaign") публикует одни и те же посты по одному расписанию в несколько чатов.
Отмеченные чаты и состояние отправки в каждый из них — CampaignChat («цель»).
Черновик (kind="draft") — заготовка рассылки без чатов.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import Database
from bot.db.models import ALL_WEEKDAYS, Campaign, CampaignChat, Chat, ModerationLog, Post, SendLog, Setting, now_ts

CAMPAIGN, DRAFT = "campaign", "draft"

# Что переносится из черновика в рассылку и обратно. Чаты, статус и счётчики у каждой рассылки свои.
TEMPLATE_FIELDS = (
    "times",
    "weekdays",
    "jitter_min",
    "start_date",
    "end_date",
    "rotation",
    "silent",
    "protect",
    "pin",
    "delete_prev",
)
POST_FIELDS = ("kind", "payload", "buttons", "forward_from", "send_mode")

NAME_LIMIT = 64
GROUP_TYPES = ("group", "supergroup")
MODERATION_KEEP_SECONDS = 14 * 24 * 3600  # журнал удалённого защитой хранится две недели
JOIN_REASON = "join"  # в том же журнале — заявки на вступление, которые бот принял сам


@dataclass
class ChatChange:
    """Результат обновления статуса бота в чате."""

    chat: Chat | None
    prev_status: str | None
    status: str | None
    lost_post_right: bool = False

    @property
    def is_new(self) -> bool:
        return self.prev_status is None and self.chat is not None


@dataclass
class Target:
    """Чат, отмеченный в рассылке: связь с состоянием отправки и сам чат."""

    link: CampaignChat
    chat: Chat

    @property
    def deliverable(self) -> bool:
        """Можно ли сейчас публиковать в этот чат. Остальные чаты просто пропускаются
        и сами возвращаются в работу, когда проблема исчезнет (кроме паузы после ошибок)."""
        return self.chat.status == "active" and self.chat.can_post and not self.link.paused


@dataclass
class DraftApplied:
    """Итог применения черновика (см. Repo.apply_draft)."""

    campaign: Campaign  # что показать: новая или основная рассылка, либо та, где уже был выбранный чат
    created: bool
    touched: list[int]  # рассылки, получившие содержимое черновика — их нужно перепланировать


@dataclass
class ClaimDecision:
    """Что планировщик решил сделать со слотом (см. Repo.claim_run)."""

    next_slot_ts: int | None
    next_run_ts: int | None
    post: Post | None
    skipped: bool
    finished: bool


@dataclass
class Claim:
    campaign: Campaign
    targets: list[Target]  # куда публиковать этот слот (только доступные чаты)
    decision: ClaimDecision


def next_chat_status(prev: str | None, in_chat: bool, actor_id: int | None, admin_ids: Collection[int]) -> str:
    """Статус чата после изменения прав бота.

    Админ бота всегда активирует чат. Добавление кем-то другим — «ждёт подтверждения».
    Уже принятый чат остаётся принятым, если кто-то просто поменял боту права.
    """
    if not in_chat:
        return "left"
    if actor_id is not None and actor_id in admin_ids:
        return "active"
    if prev in ("active", "pending"):
        return prev
    return "pending"


def chat_sort_key(chat: Chat) -> tuple[bool, str, int]:
    # SQLite не умеет сортировать кириллицу без учёта регистра — сортируем в Python
    return chat.status != "active", chat.title.casefold(), chat.id


def _clip_name(name: str) -> str:
    name = " ".join(name.split())
    return name[:NAME_LIMIT] or "Без названия"


def _blank(name: str, kind: str) -> Campaign:
    return Campaign(
        kind=kind,
        name=_clip_name(name),
        is_active=False,
        times=[],
        weekdays=list(ALL_WEEKDAYS),
        jitter_min=0,
        rotation="sequential",
        silent=False,
        protect=False,
        pin=False,
        delete_prev=False,
        last_message_ids=[],
        sent_count=0,
        fail_count=0,
    )


def _copy_post(post: Post, campaign_id: int, position: int) -> Post:
    return Post(campaign_id=campaign_id, position=position, **{f: copy.deepcopy(getattr(post, f)) for f in POST_FIELDS})


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._pruned_ts = 0  # когда последний раз чистили журнал защиты

    # ------------------------------------------------------------------ settings

    async def load_settings(self) -> dict[str, str]:
        async with self.db.session() as s:
            rows = (await s.scalars(select(Setting))).all()
            return {row.key: row.value for row in rows}

    async def save_setting(self, key: str, value: str) -> None:
        async with self.db.session() as s:
            await s.merge(Setting(key=key, value=value))
            await s.commit()

    async def delete_setting(self, key: str) -> None:
        async with self.db.session() as s:
            await s.execute(delete(Setting).where(Setting.key == key))
            await s.commit()

    # --------------------------------------------------------------------- chats

    async def get_chat(self, chat_id: int) -> Chat | None:
        async with self.db.session() as s:
            return await s.get(Chat, chat_id)

    async def get_chat_by_tg(self, tg_id: int) -> Chat | None:
        async with self.db.session() as s:
            return await s.scalar(select(Chat).where(Chat.tg_id == tg_id))

    async def list_chats(self, statuses: Iterable[str] | None = None) -> list[Chat]:
        async with self.db.session() as s:
            query = select(Chat)
            if statuses is not None:
                query = query.where(Chat.status.in_(list(statuses)))
            chats = list((await s.scalars(query)).all())
        chats.sort(key=chat_sort_key)
        return chats

    async def campaign_stats_by_chat(self) -> dict[int, tuple[int, int]]:
        """chat_id -> (рассылок, где отмечен чат; из них публикуют в него сейчас)."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(
                    CampaignChat.chat_id,
                    func.count(CampaignChat.id),
                    func.sum(case((Campaign.is_active.is_(True) & CampaignChat.paused.is_(False), 1), else_=0)),
                )
                .join(Campaign, Campaign.id == CampaignChat.campaign_id)
                .group_by(CampaignChat.chat_id)
            )
            return {chat_id: (int(total), int(active or 0)) for chat_id, total, active in rows}

    async def upsert_chat_membership(
        self,
        *,
        tg_id: int,
        title: str,
        username: str | None,
        chat_type: str,
        is_forum: bool,
        in_chat: bool,
        is_admin: bool,
        can_post: bool,
        can_pin: bool,
        actor_id: int | None,
        admin_ids: Collection[int],
        can_delete: bool = False,
        can_invite: bool = False,
        can_restrict: bool = False,
    ) -> ChatChange:
        """Бот вышел из чата — рассылки не трогаем: этот чат просто пропускается при публикации
        и возвращается в работу, когда бота добавят снова."""
        async with self.db.session() as s:
            chat = await s.scalar(select(Chat).where(Chat.tg_id == tg_id))
            prev = chat.status if chat else None
            if chat is None:
                if not in_chat:
                    return ChatChange(chat=None, prev_status=None, status=None)
                chat = Chat(tg_id=tg_id)
                s.add(chat)
            old_can_post = chat.can_post if prev is not None else False
            status = next_chat_status(prev, in_chat, actor_id, admin_ids)

            chat.title = title or chat.title or str(tg_id)
            chat.username = username
            chat.type = chat_type
            chat.is_forum = is_forum
            chat.is_admin = is_admin
            chat.can_post = can_post
            chat.can_pin = can_pin
            chat.can_delete = can_delete
            chat.can_invite = can_invite
            chat.can_restrict = can_restrict
            chat.status = status
            if prev in (None, "left") and in_chat:
                chat.added_by = actor_id
            chat.updated_ts = now_ts()
            await s.commit()
            return ChatChange(
                chat=chat,
                prev_status=prev,
                status=status,
                lost_post_right=bool(prev == status == "active" and old_can_post and not can_post),
            )

    async def set_chat_status(self, chat_id: int, status: str) -> Chat | None:
        async with self.db.session() as s:
            chat = await s.get(Chat, chat_id)
            if chat is None:
                return None
            chat.status = status
            chat.updated_ts = now_ts()
            await s.commit()
            return chat

    async def delete_chat(self, chat_id: int) -> None:
        """Чат пропадает из рассылок (ON DELETE CASCADE по campaign_chats), сами рассылки остаются."""
        async with self.db.session() as s:
            await s.execute(delete(Chat).where(Chat.id == chat_id))
            await s.commit()

    async def migrate_chat(self, old_tg_id: int, new_tg_id: int) -> Chat | None:
        """Группа превратилась в супергруппу: у чата новый id. Идемпотентно.

        Если запись для нового id уже успела появиться (апдейт пришёл раньше сервисного
        сообщения о миграции), отметки в рассылках переносятся в неё, а старая запись удаляется.
        """
        async with self.db.session() as s:
            old = await s.scalar(select(Chat).where(Chat.tg_id == old_tg_id))
            new = await s.scalar(select(Chat).where(Chat.tg_id == new_tg_id))
            if old is None:
                return new
            # id прошлых сообщений относятся к старой группе — в супергруппе это были бы чужие сообщения
            await s.execute(update(CampaignChat).where(CampaignChat.chat_id == old.id).values(last_message_ids=[]))
            if new is None:
                old.tg_id = new_tg_id
                old.type = "supergroup"
                old.updated_ts = now_ts()
                await s.commit()
                return old
            taken = list(await s.scalars(select(CampaignChat.campaign_id).where(CampaignChat.chat_id == new.id)))
            if taken:  # рассылка уже отмечена в новом чате — вторая отметка не нужна
                await s.execute(
                    delete(CampaignChat).where(CampaignChat.chat_id == old.id, CampaignChat.campaign_id.in_(taken))
                )
            await s.execute(update(CampaignChat).where(CampaignChat.chat_id == old.id).values(chat_id=new.id))
            await s.execute(update(SendLog).where(SendLog.chat_id == old.id).values(chat_id=new.id))
            rank = {"active": 2, "pending": 1, "left": 0}
            if rank.get(old.status, 0) > rank.get(new.status, 0):
                new.status = old.status
            new.type = "supergroup"
            new.updated_ts = now_ts()
            await s.delete(old)
            await s.commit()
            return new

    # ------------------------------------------------------------ защита групп

    async def update_chat(self, chat_id: int, **values: Any) -> Chat | None:
        async with self.db.session() as s:
            chat = await s.get(Chat, chat_id)
            if chat is None:
                return None
            for key, value in values.items():
                setattr(chat, key, value)
            await s.commit()
            return chat

    async def set_spam_everywhere(self, on: bool) -> int:
        """Включает или выключает антиспам во всех группах; выбранные правила не меняются."""
        async with self.db.session() as s:
            chats = list((await s.scalars(select(Chat).where(Chat.type.in_(GROUP_TYPES)))).all())
            for chat in chats:
                chat.spam_filter = {**(chat.spam_filter or {}), "on": on}
            await s.commit()
            return len(chats)

    async def reset_sub_modes(self) -> int:
        """Все группы переходят на общие каналы подписки (в том числе те, где подписка была выключена)."""
        async with self.db.session() as s:
            result = await s.execute(
                update(Chat).where(Chat.type.in_(GROUP_TYPES), Chat.sub_mode.is_not(None)).values(sub_mode=None)
            )
            await s.commit()
            return int(result.rowcount or 0)

    async def reset_auto_approve(self) -> int:
        """Все чаты и каналы переходят на общую настройку автоприёма заявок."""
        async with self.db.session() as s:
            result = await s.execute(update(Chat).where(Chat.auto_approve.is_not(None)).values(auto_approve=None))
            await s.commit()
            return int(result.rowcount or 0)

    async def log_moderation(
        self,
        chat_id: int,
        *,
        user_id: int | None,
        user_name: str,
        reason: str,
        snippet: str | None,
        now: int,
    ) -> None:
        async with self.db.session() as s:
            if await s.get(Chat, chat_id) is None:  # чат удалили из бота, пока шла проверка
                return
            s.add(
                ModerationLog(
                    chat_id=chat_id,
                    user_id=user_id,
                    user_name=user_name[:128],
                    reason=reason,
                    snippet=snippet[:100] if snippet else None,
                    ts=now,
                )
            )
            if now - self._pruned_ts >= 3600:
                self._pruned_ts = now
                await s.execute(delete(ModerationLog).where(ModerationLog.ts < now - MODERATION_KEEP_SECONDS))
            await s.commit()

    async def moderation_counts(self, since_ts: int, chat_id: int | None = None) -> tuple[int, int]:
        """(удалено спама, удалено сообщений без подписки) начиная с since_ts."""
        async with self.db.session() as s:
            query = select(ModerationLog.reason, func.count(ModerationLog.id)).where(ModerationLog.ts >= since_ts)
            if chat_id is not None:
                query = query.where(ModerationLog.chat_id == chat_id)
            counts = {reason: int(count) for reason, count in await s.execute(query.group_by(ModerationLog.reason))}
        counts.pop(JOIN_REASON, 0)  # принятые заявки — не удаления
        sub = counts.pop("sub", 0)
        return sum(counts.values()), sub

    async def join_counts(self, since_ts: int, chat_id: int | None = None) -> int:
        """Сколько заявок на вступление бот принял начиная с since_ts."""
        async with self.db.session() as s:
            query = select(func.count(ModerationLog.id)).where(
                ModerationLog.reason == JOIN_REASON, ModerationLog.ts >= since_ts
            )
            if chat_id is not None:
                query = query.where(ModerationLog.chat_id == chat_id)
            return int(await s.scalar(query) or 0)

    async def last_moderation(self, chat_id: int, limit: int = 10) -> list[ModerationLog]:
        """Последние удалённые сообщения (принятые заявки сюда не входят)."""
        async with self.db.session() as s:
            rows = await s.scalars(
                select(ModerationLog)
                .where(ModerationLog.chat_id == chat_id, ModerationLog.reason != JOIN_REASON)
                .order_by(ModerationLog.ts.desc(), ModerationLog.id.desc())
                .limit(limit)
            )
            return list(rows.all())

    # ----------------------------------------------------------------- campaigns

    async def create_campaign(self, name: str, *, kind: str = CAMPAIGN, chat_ids: Iterable[int] = ()) -> Campaign:
        async with self.db.session() as s:
            campaign = _blank(name, kind)
            s.add(campaign)
            await s.flush()
            for chat_id in dict.fromkeys(chat_ids):
                s.add(CampaignChat(campaign_id=campaign.id, chat_id=chat_id))
            await s.commit()
            return campaign

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        async with self.db.session() as s:
            return await s.get(Campaign, campaign_id)

    async def list_campaigns(self, chat_id: int | None = None) -> list[Campaign]:
        """Все рассылки или только те, где отмечен чат."""
        async with self.db.session() as s:
            query = select(Campaign).where(Campaign.kind == CAMPAIGN)
            if chat_id is not None:
                query = query.join(CampaignChat, CampaignChat.campaign_id == Campaign.id).where(
                    CampaignChat.chat_id == chat_id
                )
            return list((await s.scalars(query.order_by(Campaign.id))).all())

    async def list_drafts(self) -> list[Campaign]:
        async with self.db.session() as s:
            rows = await s.scalars(select(Campaign).where(Campaign.kind == DRAFT).order_by(Campaign.id))
            return list(rows.all())

    async def count_campaigns(self, chat_id: int | None = None) -> int:
        async with self.db.session() as s:
            if chat_id is not None:
                query = select(func.count(CampaignChat.id)).where(CampaignChat.chat_id == chat_id)
            else:
                query = select(func.count(Campaign.id)).where(Campaign.kind == CAMPAIGN)
            return int(await s.scalar(query) or 0)

    async def update_campaign(self, campaign_id: int, **values: Any) -> Campaign | None:
        async with self.db.session() as s:
            campaign = await s.get(Campaign, campaign_id)
            if campaign is None:
                return None
            for key, value in values.items():
                if key == "name":
                    value = _clip_name(value)
                setattr(campaign, key, value)
            campaign.updated_ts = now_ts()
            await s.commit()
            return campaign

    async def delete_campaign(self, campaign_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(delete(Campaign).where(Campaign.id == campaign_id))
            await s.commit()

    async def linked_campaigns(self, draft_id: int) -> list[Campaign]:
        """Рассылки, созданные из черновика или сохранённые в него."""
        async with self.db.session() as s:
            rows = await s.scalars(
                select(Campaign)
                .where(Campaign.source_draft_id == draft_id, Campaign.kind == CAMPAIGN)
                .order_by(Campaign.id)
            )
            return list(rows.all())

    async def linked_counts(self) -> dict[int, int]:
        """draft_id -> в скольких чатах публикуются рассылки из этого черновика."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(Campaign.source_draft_id, func.count(func.distinct(CampaignChat.chat_id)))
                .join(CampaignChat, CampaignChat.campaign_id == Campaign.id)
                .where(Campaign.source_draft_id.is_not(None), Campaign.kind == CAMPAIGN)
                .group_by(Campaign.source_draft_id)
            )
            return {int(draft_id): int(count) for draft_id, count in rows}

    async def active_campaigns(self) -> list[Campaign]:
        """Включённые рассылки, ближайшие публикации — первыми."""
        async with self.db.session() as s:
            rows = await s.scalars(
                select(Campaign)
                .where(Campaign.kind == CAMPAIGN, Campaign.is_active.is_(True))
                .order_by(Campaign.next_run_ts.is_(None), Campaign.next_run_ts, Campaign.id)
            )
            return list(rows.all())

    async def campaign_totals(self) -> tuple[int, int]:
        """(всего рассылок, включённых)."""
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(
                        func.count(Campaign.id),
                        func.sum(case((Campaign.is_active.is_(True), 1), else_=0)),
                    ).where(Campaign.kind == CAMPAIGN)
                )
            ).one()
            return int(row[0] or 0), int(row[1] or 0)

    # ------------------------------------------------------------ чаты рассылки

    async def _targets(self, s: AsyncSession, campaign_ids: Collection[int]) -> dict[int, list[Target]]:
        result: dict[int, list[Target]] = {cid: [] for cid in campaign_ids}
        if not result:
            return result
        rows = await s.execute(
            select(CampaignChat, Chat)
            .join(Chat, Chat.id == CampaignChat.chat_id)
            .where(CampaignChat.campaign_id.in_(list(result)))
        )
        for link, chat in rows.all():
            result[link.campaign_id].append(Target(link=link, chat=chat))
        for targets in result.values():
            targets.sort(key=lambda target: chat_sort_key(target.chat))
        return result

    async def campaign_targets(self, campaign_id: int) -> list[Target]:
        async with self.db.session() as s:
            return (await self._targets(s, [campaign_id]))[campaign_id]

    async def targets_of(self, campaign_ids: Iterable[int]) -> dict[int, list[Target]]:
        async with self.db.session() as s:
            return await self._targets(s, list(dict.fromkeys(campaign_ids)))

    async def chat_links(self, chat_id: int) -> dict[int, CampaignChat]:
        """campaign_id -> отметка этого чата в рассылке."""
        async with self.db.session() as s:
            rows = await s.scalars(select(CampaignChat).where(CampaignChat.chat_id == chat_id))
            return {link.campaign_id: link for link in rows.all()}

    async def set_target(self, campaign_id: int, chat_id: int, on: bool) -> bool:
        """Отмечает чат в рассылке или снимает отметку. Возвращает, изменилось ли что-то."""
        async with self.db.session() as s:
            link = await s.scalar(
                select(CampaignChat).where(CampaignChat.campaign_id == campaign_id, CampaignChat.chat_id == chat_id)
            )
            if on and link is None:
                if await s.get(Campaign, campaign_id) is None or await s.get(Chat, chat_id) is None:
                    return False
                s.add(CampaignChat(campaign_id=campaign_id, chat_id=chat_id))
            elif not on and link is not None:
                await s.delete(link)
            else:
                return False
            await s.commit()
            return True

    async def add_targets(self, campaign_id: int, chat_ids: Iterable[int]) -> int:
        """Отмечает чаты в дополнение к уже отмеченным. Возвращает, сколько добавлено."""
        async with self.db.session() as s:
            if await s.get(Campaign, campaign_id) is None:
                return 0
            added = await self._add_targets(s, campaign_id, chat_ids)
            await s.commit()
            return added

    async def _add_targets(self, s: AsyncSession, campaign_id: int, chat_ids: Iterable[int]) -> int:
        existing = set(await s.scalars(select(CampaignChat.chat_id).where(CampaignChat.campaign_id == campaign_id)))
        added = 0
        for chat_id in dict.fromkeys(chat_ids):
            if chat_id not in existing:
                s.add(CampaignChat(campaign_id=campaign_id, chat_id=chat_id))
                added += 1
        return added

    async def clear_targets(self, campaign_id: int) -> int:
        async with self.db.session() as s:
            result = await s.execute(delete(CampaignChat).where(CampaignChat.campaign_id == campaign_id))
            await s.commit()
            return int(result.rowcount or 0)

    async def update_target(self, campaign_id: int, chat_id: int, **values: Any) -> CampaignChat | None:
        async with self.db.session() as s:
            link = await s.scalar(
                select(CampaignChat).where(CampaignChat.campaign_id == campaign_id, CampaignChat.chat_id == chat_id)
            )
            if link is None:
                return None
            for key, value in values.items():
                setattr(link, key, value)
            await s.commit()
            return link

    async def resume_target(self, campaign_id: int, chat_id: int) -> CampaignChat | None:
        """Снимает паузу, поставленную после серии ошибок."""
        return await self.update_target(campaign_id, chat_id, paused=False, fail_count=0, last_error=None)

    async def _reset_targets(self, s: AsyncSession, campaign_id: int) -> None:
        await s.execute(
            update(CampaignChat)
            .where(CampaignChat.campaign_id == campaign_id)
            .values(paused=False, fail_count=0, last_error=None)
        )

    async def reset_targets(self, campaign_id: int) -> None:
        """Запуск рассылки — с чистого листа: пауза после ошибок снимается, счётчики ошибок обнуляются."""
        async with self.db.session() as s:
            await self._reset_targets(s, campaign_id)
            await s.commit()

    async def resume_chat_targets(self, chat_id: int) -> int:
        """Снимает паузу после ошибок во всех рассылках этого чата."""
        async with self.db.session() as s:
            result = await s.execute(
                update(CampaignChat)
                .where(CampaignChat.chat_id == chat_id, CampaignChat.paused.is_(True))
                .values(paused=False, fail_count=0, last_error=None)
            )
            await s.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------ черновики

    async def _copy_template(self, s: AsyncSession, src: Campaign, dst: Campaign, posts: Sequence[Post]) -> None:
        for field in TEMPLATE_FIELDS:
            setattr(dst, field, copy.deepcopy(getattr(src, field)))
        await s.execute(delete(Post).where(Post.campaign_id == dst.id))
        for position, post in enumerate(posts):
            s.add(_copy_post(post, dst.id, position))
        dst.last_post_id = None
        dst.updated_ts = now_ts()

    async def _posts_of(self, s: AsyncSession, campaign_id: int) -> list[Post]:
        rows = await s.scalars(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.position, Post.id))
        return list(rows.all())

    async def save_as_draft(self, campaign_id: int, name: str | None = None) -> Campaign | None:
        """Копирует рассылку в новый черновик и связывает исходную рассылку с ним."""
        async with self.db.session() as s:
            src = await s.get(Campaign, campaign_id)
            if src is None:
                return None
            posts = await self._posts_of(s, src.id)
            draft = _blank(name or src.name, DRAFT)
            s.add(draft)
            await s.flush()
            await self._copy_template(s, src, draft, posts)
            if src.kind == CAMPAIGN:
                src.source_draft_id = draft.id
            await s.commit()
            return draft

    async def apply_draft(self, draft_id: int, chat_ids: Iterable[int], *, activate: bool) -> DraftApplied | None:
        """Черновик → связанная с ним рассылка. При первом применении она создаётся, при повторном —
        получает посты, расписание и опции черновика, а новые отмеченные чаты добавляются к ней.

        До версии 2 черновик, применённый к нескольким чатам, давал по рассылке на чат. Чат, который уже
        есть в любой из них, второй раз не добавляется (иначе посты шли бы в него дважды) — его рассылка
        просто получает содержимое черновика.

        activate=True запускает затронутые рассылки; иначе новая создаётся остановленной,
        а у существующих статус не меняется. None — черновика нет.
        """
        chat_ids = list(dict.fromkeys(chat_ids))
        async with self.db.session() as s:
            draft = await s.get(Campaign, draft_id)
            if draft is None or draft.kind != DRAFT:
                return None
            posts = await self._posts_of(s, draft.id)
            linked = list(
                (
                    await s.scalars(
                        select(Campaign)
                        .where(Campaign.source_draft_id == draft.id, Campaign.kind == CAMPAIGN)
                        .order_by(Campaign.id)
                    )
                ).all()
            )
            by_id = {c.id: c for c in linked}
            owner: dict[int, Campaign] = {}  # чат -> рассылка из этого черновика, где он уже отмечен
            if linked:
                rows = await s.execute(
                    select(CampaignChat.chat_id, CampaignChat.campaign_id)
                    .where(CampaignChat.campaign_id.in_(list(by_id)))
                    .order_by(CampaignChat.campaign_id)
                )
                for chat_id, campaign_id in rows.all():
                    owner.setdefault(chat_id, by_id[campaign_id])
            created = not linked
            if created:
                primary = _blank(draft.name, CAMPAIGN)
                primary.source_draft_id = draft.id
                s.add(primary)
                await s.flush()
            else:
                primary = linked[0]
            new_chats = [chat_id for chat_id in chat_ids if chat_id not in owner]
            touched: dict[int, Campaign] = {}
            if created or new_chats:
                touched[primary.id] = primary
            for chat_id in chat_ids:
                if chat_id in owner:
                    touched.setdefault(owner[chat_id].id, owner[chat_id])
            for campaign in touched.values():
                await self._copy_template(s, draft, campaign, posts)
            await self._add_targets(s, primary.id, new_chats)
            if activate:
                for campaign in touched.values():
                    if not campaign.is_active:
                        await self._reset_targets(s, campaign.id)
                    campaign.is_active = True
                    campaign.last_error = None
            await s.commit()
            shown = owner[chat_ids[0]] if len(chat_ids) == 1 and chat_ids[0] in owner else primary
            return DraftApplied(campaign=shown, created=created, touched=list(touched))

    async def sync_draft(self, draft_id: int) -> list[Campaign]:
        """«Обновить рассылки»: посты, расписание и опции черновика переносятся во все связанные
        рассылки. Чаты и статус рассылок не меняются."""
        async with self.db.session() as s:
            draft = await s.get(Campaign, draft_id)
            if draft is None or draft.kind != DRAFT:
                return []
            posts = await self._posts_of(s, draft.id)
            linked = list(
                (
                    await s.scalars(
                        select(Campaign)
                        .where(Campaign.source_draft_id == draft.id, Campaign.kind == CAMPAIGN)
                        .order_by(Campaign.id)
                    )
                ).all()
            )
            for dst in linked:
                await self._copy_template(s, draft, dst, posts)
            await s.commit()
            return linked

    # --------------------------------------------------------------------- posts

    async def list_posts(self, campaign_id: int) -> list[Post]:
        async with self.db.session() as s:
            return await self._posts_of(s, campaign_id)

    async def get_post(self, post_id: int) -> Post | None:
        async with self.db.session() as s:
            return await s.get(Post, post_id)

    async def post_counts(self, campaign_ids: Iterable[int]) -> dict[int, int]:
        ids = list(campaign_ids)
        if not ids:
            return {}
        async with self.db.session() as s:
            rows = await s.execute(
                select(Post.campaign_id, func.count(Post.id))
                .where(Post.campaign_id.in_(ids))
                .group_by(Post.campaign_id)
            )
            return {int(cid): int(count) for cid, count in rows}

    async def _append(self, s: AsyncSession, post: Post) -> int:
        """Ставит пост в конец рассылки. Возвращает его номер."""
        max_pos = await s.scalar(select(func.max(Post.position)).where(Post.campaign_id == post.campaign_id))
        post.position = (max_pos + 1) if max_pos is not None else 0
        s.add(post)
        await s.flush()
        return int(await s.scalar(select(func.count(Post.id)).where(Post.campaign_id == post.campaign_id)) or 0)

    async def add_post(
        self,
        campaign_id: int,
        *,
        kind: str,
        payload: dict[str, Any],
        buttons: list[Any] | None = None,
        forward_from: dict[str, Any] | None = None,
    ) -> tuple[Post, int]:
        """Добавляет пост в конец. Возвращает (пост, его номер в рассылке)."""
        async with self.db.session() as s:
            post = Post(
                campaign_id=campaign_id,
                kind=kind,
                payload=payload,
                buttons=buttons,
                forward_from=forward_from,
                send_mode="copy",
            )
            number = await self._append(s, post)
            await s.commit()
            return post, number

    async def update_post(self, post_id: int, **values: Any) -> Post | None:
        async with self.db.session() as s:
            post = await s.get(Post, post_id)
            if post is None:
                return None
            for key, value in values.items():
                setattr(post, key, value)
            await s.commit()
            return post

    async def delete_post(self, post_id: int) -> int | None:
        """Удаляет пост, возвращает id рассылки."""
        async with self.db.session() as s:
            post = await s.get(Post, post_id)
            if post is None:
                return None
            campaign_id = post.campaign_id
            await s.delete(post)
            await s.flush()
            for index, item in enumerate(await self._posts_of(s, campaign_id)):
                item.position = index
            await s.commit()
            return campaign_id

    async def move_post(self, post_id: int, delta: int) -> bool:
        async with self.db.session() as s:
            post = await s.get(Post, post_id)
            if post is None:
                return False
            posts = await self._posts_of(s, post.campaign_id)
            index = next(i for i, p in enumerate(posts) if p.id == post_id)
            target = index + delta
            if not 0 <= target < len(posts):
                return False
            posts[index], posts[target] = posts[target], posts[index]
            for position, item in enumerate(posts):
                item.position = position
            await s.commit()
            return True

    # ------------------------------------------------------------- «Мои посты»

    async def list_all_posts(self, offset: int = 0, limit: int = 10) -> list[tuple[Post, Campaign]]:
        """Посты всех рассылок и черновиков, новые — первыми."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(Post, Campaign)
                .join(Campaign, Campaign.id == Post.campaign_id)
                .order_by(Post.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [(post, campaign) for post, campaign in rows.all()]

    async def count_posts_total(self) -> int:
        async with self.db.session() as s:
            return int(await s.scalar(select(func.count(Post.id))) or 0)

    async def copy_post(self, post_id: int, campaign_id: int) -> tuple[Post, int] | None:
        """Копия поста в конец другой рассылки или черновика. Возвращает (копия, её номер)."""
        async with self.db.session() as s:
            src = await s.get(Post, post_id)
            if src is None or await s.get(Campaign, campaign_id) is None:
                return None
            post = _copy_post(src, campaign_id, 0)
            number = await self._append(s, post)
            await s.commit()
            return post, number

    async def campaign_from_post(self, post_id: int, name: str) -> Campaign | None:
        """Новая остановленная рассылка без чатов с копией поста."""
        async with self.db.session() as s:
            src = await s.get(Post, post_id)
            if src is None:
                return None
            campaign = _blank(name, CAMPAIGN)
            s.add(campaign)
            await s.flush()
            s.add(_copy_post(src, campaign.id, 0))
            await s.commit()
            return campaign

    # ----------------------------------------------------------------- scheduler

    async def due_runs(self, now: int) -> list[tuple[int, int]]:
        """Рассылки, которым пора публиковать: [(id, next_run_ts)]."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(Campaign.id, Campaign.next_run_ts)
                .where(
                    Campaign.kind == CAMPAIGN,
                    Campaign.is_active.is_(True),
                    Campaign.next_run_ts.is_not(None),
                    Campaign.next_run_ts <= now,
                )
                .order_by(Campaign.next_run_ts)
            )
            return [(int(cid), int(run)) for cid, run in rows.all()]

    async def claim_run(
        self,
        campaign_id: int,
        expected_run_ts: int,
        decide: Callable[[Campaign, list[Post]], ClaimDecision],
        now: int,
    ) -> Claim | None:
        """Атомарно «занимает» слот: сдвигает расписание вперёд ДО отправки.

        UPDATE ... WHERE next_run_ts = :expected гарантирует, что один слот не будет
        отправлен дважды — ни параллельной задачей, ни после падения процесса.
        Если публиковать некуда (все чаты недоступны), слот всё равно проходит — без отправки
        и без смены очереди постов.
        """
        async with self.db.session() as s:
            campaign = await s.get(Campaign, campaign_id)
            if (
                campaign is None
                or campaign.kind != CAMPAIGN
                or not campaign.is_active
                or campaign.next_run_ts != expected_run_ts
            ):
                return None
            posts = await self._posts_of(s, campaign.id)
            targets = [t for t in (await self._targets(s, [campaign.id]))[campaign.id] if t.deliverable]
            decision = decide(campaign, posts)
            values: dict[str, Any] = {
                "next_slot_ts": decision.next_slot_ts,
                "next_run_ts": decision.next_run_ts,
                "last_slot_ts": campaign.next_slot_ts,
                "updated_ts": now,
            }
            if decision.post is not None and targets:
                values["last_post_id"] = decision.post.id
            if decision.finished:
                values["is_active"] = False
            result = await s.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id, Campaign.next_run_ts == expected_run_ts)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await s.rollback()
                return None
            skip_reason = None
            if decision.skipped:
                skip_reason = "Слот пропущен: бот был недоступен дольше допустимого"
            elif decision.post is not None and not targets:
                skip_reason = "Слот пропущен: нет чатов, куда бот может публиковать"
            if skip_reason:
                s.add(SendLog(campaign_id=campaign.id, status="skipped", error=skip_reason, ts=now))
            await s.commit()
            return Claim(campaign=campaign, targets=targets, decision=decision)

    async def mark_post_used(self, campaign_id: int, post_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(update(Campaign).where(Campaign.id == campaign_id).values(last_post_id=post_id))
            await s.commit()

    async def _link(self, s: AsyncSession, campaign_id: int, chat_id: int) -> CampaignChat | None:
        return await s.scalar(
            select(CampaignChat).where(CampaignChat.campaign_id == campaign_id, CampaignChat.chat_id == chat_id)
        )

    async def _log_chat(self, s: AsyncSession, chat_id: int) -> int | None:
        # Чат могли удалить из бота прямо во время отправки — запись в журнал без него
        return chat_id if await s.get(Chat, chat_id) is not None else None

    async def record_target_sent(
        self,
        campaign_id: int,
        chat_id: int,
        post_id: int | None,
        message_ids: list[int],
        *,
        manual: bool,
        now: int,
        warning: str | None = None,
    ) -> None:
        async with self.db.session() as s:
            link = await self._link(s, campaign_id, chat_id)
            log_chat = await self._log_chat(s, chat_id)
            if link is not None:  # чат могли снять с рассылки, пока шла отправка
                link.last_message_ids = list(message_ids)
                link.last_sent_ts = now
                link.sent_count = (link.sent_count or 0) + 1
                link.fail_count = 0
                link.last_error = warning
            s.add(
                SendLog(
                    campaign_id=campaign_id,
                    chat_id=log_chat,
                    post_id=post_id,
                    status="sent",
                    manual=manual,
                    error=warning,
                    message_ids=list(message_ids),
                    ts=now,
                )
            )
            await s.commit()

    async def record_target_failed(
        self,
        campaign_id: int,
        chat_id: int,
        post_id: int | None,
        error: str,
        *,
        manual: bool,
        now: int,
        max_fails: int | None,
    ) -> tuple[int, bool]:
        """Возвращает (ошибок подряд в этом чате, поставлен ли чат рассылки на паузу сейчас).
        max_fails=None — не ставить на паузу (например, бота удалили из чата)."""
        async with self.db.session() as s:
            link = await self._link(s, campaign_id, chat_id)
            log_chat = await self._log_chat(s, chat_id)
            fails, paused = 0, False
            if link is not None:
                fails = (link.fail_count or 0) + 1
                link.fail_count = fails
                link.last_error = error[:1000]
                if not manual and max_fails is not None and fails >= max_fails and not link.paused:
                    link.paused = paused = True
            s.add(
                SendLog(
                    campaign_id=campaign_id,
                    chat_id=log_chat,
                    post_id=post_id,
                    status="failed",
                    manual=manual,
                    error=error[:1000],
                    ts=now,
                )
            )
            await s.commit()
            return fails, paused

    async def stop_campaign(self, campaign_id: int, error: str, *, now: int) -> None:
        """Останавливает рассылку из-за её собственной проблемы (например, не осталось постов)."""
        async with self.db.session() as s:
            await s.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(is_active=False, next_slot_ts=None, next_run_ts=None, last_error=error[:1000], updated_ts=now)
            )
            s.add(SendLog(campaign_id=campaign_id, status="failed", error=error[:1000], ts=now))
            await s.commit()

    async def reschedule(
        self,
        planner: Callable[[Campaign], tuple[int | None, int | None]],
        campaign_ids: Iterable[int] | None = None,
        *,
        only_missing: bool = False,
    ) -> list[Campaign]:
        """Пересчитывает next_run. Статус рассылок не меняет: если слотов нет (не выбраны дни,
        период закончился), рассылка остаётся включённой без ближайших запусков — так правка
        расписания не выключает её молча. Возвращает такие рассылки."""
        stalled: list[Campaign] = []
        async with self.db.session() as s:
            query = select(Campaign).where(Campaign.kind == CAMPAIGN)
            if campaign_ids is not None:
                ids = list(campaign_ids)
                if not ids:
                    return stalled
                query = query.where(Campaign.id.in_(ids))
            if only_missing:
                query = query.where(Campaign.is_active.is_(True), Campaign.next_run_ts.is_(None))
            for campaign in (await s.scalars(query)).all():
                if not campaign.is_active:
                    campaign.next_slot_ts = campaign.next_run_ts = None
                    continue
                slot, run = planner(campaign)
                campaign.next_slot_ts, campaign.next_run_ts = slot, run
                if slot is None:
                    stalled.append(campaign)
            await s.commit()
        return stalled

    # ---------------------------------------------------------------------- logs

    async def log_counts_since(self, since_ts: int) -> dict[str, int]:
        async with self.db.session() as s:
            rows = await s.execute(
                select(SendLog.status, func.count(SendLog.id)).where(SendLog.ts >= since_ts).group_by(SendLog.status)
            )
            return {status: int(count) for status, count in rows}

    async def last_errors(self, limit: int = 5) -> list[tuple[SendLog, Chat | None, Campaign | None]]:
        async with self.db.session() as s:
            rows = await s.execute(
                select(SendLog, Chat, Campaign)
                .outerjoin(Chat, Chat.id == SendLog.chat_id)
                .outerjoin(Campaign, Campaign.id == SendLog.campaign_id)
                .where(SendLog.status == "failed")
                .order_by(SendLog.ts.desc(), SendLog.id.desc())
                .limit(limit)
            )
            return [(log, chat, campaign) for log, chat, campaign in rows.all()]
