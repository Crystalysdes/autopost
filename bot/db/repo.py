"""Доступ к данным. Каждый метод — отдельная короткая транзакция.

Объекты, которые возвращают методы, «отсоединены» от сессии (expire_on_commit=False):
их можно читать, но изменения нужно сохранять через методы репозитория.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import Database
from bot.db.models import ALL_WEEKDAYS, Campaign, Chat, Post, SendLog, Setting, now_ts

# Что переносится при копировании рассылки / применении черновика.
# Тема форума (thread_id), статус и счётчики принадлежат конкретному чату и не копируются.
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
POST_FIELDS = ("position", "kind", "payload", "buttons", "forward_from", "send_mode")

NAME_LIMIT = 64


@dataclass
class ChatChange:
    """Результат обновления статуса бота в чате."""

    chat: Chat | None
    prev_status: str | None
    status: str | None
    lost_post_right: bool = False
    paused: int = 0

    @property
    def is_new(self) -> bool:
        return self.prev_status is None and self.chat is not None


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
    chat: Chat
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


def _clip_name(name: str) -> str:
    name = " ".join(name.split())
    return name[:NAME_LIMIT] or "Без названия"


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ settings

    async def load_settings(self) -> dict[str, str]:
        async with self.db.session() as s:
            rows = (await s.scalars(select(Setting))).all()
            return {row.key: row.value for row in rows}

    async def save_setting(self, key: str, value: str) -> None:
        async with self.db.session() as s:
            await s.merge(Setting(key=key, value=value))
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
        # SQLite не умеет сортировать кириллицу без учёта регистра — сортируем в Python
        chats.sort(key=lambda c: (c.status != "active", c.title.casefold(), c.id))
        return chats

    async def campaign_stats_by_chat(self) -> dict[int, tuple[int, int]]:
        """chat_id -> (всего рассылок, активных)."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(
                    Campaign.chat_id,
                    func.count(Campaign.id),
                    func.sum(case((Campaign.is_active.is_(True), 1), else_=0)),
                )
                .where(Campaign.chat_id.is_not(None))
                .group_by(Campaign.chat_id)
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
    ) -> ChatChange:
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
            chat.status = status
            if prev in (None, "left") and in_chat:
                chat.added_by = actor_id
            chat.updated_ts = now_ts()

            paused = 0
            if status == "left" and prev != "left":
                await s.flush()
                paused = await self._pause_chat_campaigns(s, chat.id)
            await s.commit()
            return ChatChange(
                chat=chat,
                prev_status=prev,
                status=status,
                lost_post_right=bool(prev == status == "active" and old_can_post and not can_post),
                paused=paused,
            )

    async def set_chat_status(self, chat_id: int, status: str) -> Chat | None:
        async with self.db.session() as s:
            chat = await s.get(Chat, chat_id)
            if chat is None:
                return None
            chat.status = status
            chat.updated_ts = now_ts()
            if status == "left":
                await self._pause_chat_campaigns(s, chat_id)
            await s.commit()
            return chat

    async def delete_chat(self, chat_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(delete(Chat).where(Chat.id == chat_id))
            await s.commit()

    async def migrate_chat(self, old_tg_id: int, new_tg_id: int) -> Chat | None:
        """Группа превратилась в супергруппу: у чата новый id. Идемпотентно.

        Если запись для нового id уже успела появиться (апдейт пришёл раньше сервисного
        сообщения о миграции), рассылки переносятся в неё, а старая запись удаляется.
        """
        async with self.db.session() as s:
            old = await s.scalar(select(Chat).where(Chat.tg_id == old_tg_id))
            new = await s.scalar(select(Chat).where(Chat.tg_id == new_tg_id))
            if old is None:
                return new
            if new is None:
                old.tg_id = new_tg_id
                old.type = "supergroup"
                old.updated_ts = now_ts()
                await s.commit()
                return old
            await s.execute(update(Campaign).where(Campaign.chat_id == old.id).values(chat_id=new.id))
            await s.execute(update(SendLog).where(SendLog.chat_id == old.id).values(chat_id=new.id))
            rank = {"active": 2, "pending": 1, "left": 0}
            if rank.get(old.status, 0) > rank.get(new.status, 0):
                new.status = old.status
            new.type = "supergroup"
            new.updated_ts = now_ts()
            await s.delete(old)
            await s.commit()
            return new

    async def _pause_chat_campaigns(self, s: AsyncSession, chat_id: int) -> int:
        result = await s.execute(
            update(Campaign)
            .where(Campaign.chat_id == chat_id, Campaign.is_active.is_(True))
            .values(is_active=False, next_slot_ts=None, next_run_ts=None)
        )
        return int(result.rowcount or 0)

    async def pause_chat_campaigns(self, chat_id: int) -> int:
        async with self.db.session() as s:
            count = await self._pause_chat_campaigns(s, chat_id)
            await s.commit()
            return count

    # ----------------------------------------------------------------- campaigns

    async def create_campaign(self, chat_id: int | None, name: str) -> Campaign:
        async with self.db.session() as s:
            campaign = Campaign(
                chat_id=chat_id,
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
            s.add(campaign)
            await s.commit()
            return campaign

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        async with self.db.session() as s:
            return await s.get(Campaign, campaign_id)

    async def list_campaigns(self, chat_id: int) -> list[Campaign]:
        async with self.db.session() as s:
            rows = await s.scalars(select(Campaign).where(Campaign.chat_id == chat_id).order_by(Campaign.id))
            return list(rows.all())

    async def list_drafts(self) -> list[Campaign]:
        async with self.db.session() as s:
            rows = await s.scalars(select(Campaign).where(Campaign.chat_id.is_(None)).order_by(Campaign.id))
            return list(rows.all())

    async def count_campaigns(self, chat_id: int) -> int:
        async with self.db.session() as s:
            return int(await s.scalar(select(func.count(Campaign.id)).where(Campaign.chat_id == chat_id)) or 0)

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
        async with self.db.session() as s:
            rows = await s.scalars(
                select(Campaign)
                .where(Campaign.source_draft_id == draft_id, Campaign.chat_id.is_not(None))
                .order_by(Campaign.id)
            )
            return list(rows.all())

    async def linked_counts(self) -> dict[int, int]:
        async with self.db.session() as s:
            rows = await s.execute(
                select(Campaign.source_draft_id, func.count(Campaign.id))
                .where(Campaign.source_draft_id.is_not(None), Campaign.chat_id.is_not(None))
                .group_by(Campaign.source_draft_id)
            )
            return {int(draft_id): int(count) for draft_id, count in rows}

    async def active_campaigns_with_chats(self) -> list[tuple[Campaign, Chat]]:
        async with self.db.session() as s:
            rows = await s.execute(
                select(Campaign, Chat)
                .join(Chat, Chat.id == Campaign.chat_id)
                .where(Campaign.is_active.is_(True), Chat.status == "active")
                .order_by(Campaign.next_run_ts)
            )
            return [(c, chat) for c, chat in rows.all()]

    async def campaign_totals(self) -> tuple[int, int]:
        """(всего рассылок в чатах, активных)."""
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(
                        func.count(Campaign.id),
                        func.sum(case((Campaign.is_active.is_(True), 1), else_=0)),
                    ).where(Campaign.chat_id.is_not(None))
                )
            ).one()
            return int(row[0] or 0), int(row[1] or 0)

    async def _copy_template(self, s: AsyncSession, src: Campaign, dst: Campaign, posts: Sequence[Post]) -> None:
        for field in TEMPLATE_FIELDS:
            setattr(dst, field, copy.deepcopy(getattr(src, field)))
        await s.execute(delete(Post).where(Post.campaign_id == dst.id))
        for post in posts:
            s.add(Post(campaign_id=dst.id, **{f: copy.deepcopy(getattr(post, f)) for f in POST_FIELDS}))
        dst.last_post_id = None
        dst.updated_ts = now_ts()

    async def _posts_of(self, s: AsyncSession, campaign_id: int) -> list[Post]:
        rows = await s.scalars(select(Post).where(Post.campaign_id == campaign_id).order_by(Post.position, Post.id))
        return list(rows.all())

    def _blank(self, chat_id: int | None, name: str) -> Campaign:
        return Campaign(
            chat_id=chat_id,
            name=_clip_name(name),
            is_active=False,
            last_message_ids=[],
            sent_count=0,
            fail_count=0,
        )

    async def save_as_draft(self, campaign_id: int, name: str | None = None) -> Campaign | None:
        """Копирует рассылку в новый черновик и связывает исходную рассылку с ним."""
        async with self.db.session() as s:
            src = await s.get(Campaign, campaign_id)
            if src is None:
                return None
            posts = await self._posts_of(s, src.id)
            draft = self._blank(None, name or src.name)
            s.add(draft)
            await s.flush()
            await self._copy_template(s, src, draft, posts)
            if src.chat_id is not None:
                src.source_draft_id = draft.id
            await s.commit()
            return draft

    async def apply_template(
        self,
        src_id: int,
        chat_ids: Sequence[int],
        *,
        activate: bool | None,
        link: bool,
    ) -> list[tuple[Campaign, bool]]:
        """Копирует рассылку/черновик в чаты.

        link=True (черновик): upsert по (чат, source_draft_id) — повторное применение
        обновляет уже созданную рассылку, а не плодит дубли.
        activate=None — не менять статус (используется при «Обновить во всех»).
        Возвращает [(рассылка, создана_ли_новая)].
        """
        results: list[tuple[Campaign, bool]] = []
        async with self.db.session() as s:
            src = await s.get(Campaign, src_id)
            if src is None:
                return results
            posts = await self._posts_of(s, src.id)
            for chat_id in chat_ids:
                dst: Campaign | None = None
                if link:
                    dst = await s.scalar(
                        select(Campaign)
                        .where(Campaign.chat_id == chat_id, Campaign.source_draft_id == src.id)
                        .order_by(Campaign.id)
                        .limit(1)
                    )
                created = dst is None
                if dst is None:
                    dst = self._blank(chat_id, src.name)
                    dst.source_draft_id = src.id if link else None
                    s.add(dst)
                    await s.flush()
                await self._copy_template(s, src, dst, posts)
                if activate is not None:
                    dst.is_active = activate
                    if not activate:
                        dst.next_slot_ts = dst.next_run_ts = None
                dst.fail_count = 0
                results.append((dst, created))
            await s.commit()
        return results

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
            max_pos = await s.scalar(select(func.max(Post.position)).where(Post.campaign_id == campaign_id))
            post = Post(
                campaign_id=campaign_id,
                position=(max_pos + 1) if max_pos is not None else 0,
                kind=kind,
                payload=payload,
                buttons=buttons,
                forward_from=forward_from,
                send_mode="copy",
            )
            s.add(post)
            await s.flush()
            number = int(await s.scalar(select(func.count(Post.id)).where(Post.campaign_id == campaign_id)) or 0)
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

    # ----------------------------------------------------------------- scheduler

    async def due_runs(self, now: int) -> list[tuple[int, int]]:
        """Рассылки, которым пора публиковать: [(id, next_run_ts)]."""
        async with self.db.session() as s:
            rows = await s.execute(
                select(Campaign.id, Campaign.next_run_ts)
                .join(Chat, Chat.id == Campaign.chat_id)
                .where(
                    Campaign.is_active.is_(True),
                    Campaign.next_run_ts.is_not(None),
                    Campaign.next_run_ts <= now,
                    Chat.status == "active",
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
        """
        async with self.db.session() as s:
            campaign = await s.get(Campaign, campaign_id)
            if (
                campaign is None
                or not campaign.is_active
                or campaign.chat_id is None
                or campaign.next_run_ts != expected_run_ts
            ):
                return None
            chat = await s.get(Chat, campaign.chat_id)
            if chat is None or chat.status != "active":
                return None
            posts = await self._posts_of(s, campaign.id)
            decision = decide(campaign, posts)
            values: dict[str, Any] = {
                "next_slot_ts": decision.next_slot_ts,
                "next_run_ts": decision.next_run_ts,
                "updated_ts": now,
            }
            if decision.post is not None:
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
            if decision.skipped:
                s.add(
                    SendLog(
                        campaign_id=campaign.id,
                        chat_id=chat.id,
                        status="skipped",
                        error="Слот пропущен: бот был недоступен дольше допустимого",
                        ts=now,
                    )
                )
            await s.commit()
            return Claim(campaign=campaign, chat=chat, decision=decision)

    async def mark_post_used(self, campaign_id: int, post_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(update(Campaign).where(Campaign.id == campaign_id).values(last_post_id=post_id))
            await s.commit()

    async def record_sent(
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
            campaign = await s.get(Campaign, campaign_id)
            if campaign is not None:
                campaign.last_message_ids = list(message_ids)
                campaign.last_sent_ts = now
                campaign.sent_count = (campaign.sent_count or 0) + 1
                campaign.fail_count = 0
                campaign.last_error = warning
            s.add(
                SendLog(
                    campaign_id=campaign_id,
                    chat_id=chat_id,
                    post_id=post_id,
                    status="sent",
                    manual=manual,
                    error=warning,
                    message_ids=list(message_ids),
                    ts=now,
                )
            )
            await s.commit()

    async def record_failed(
        self,
        campaign_id: int,
        chat_id: int | None,
        post_id: int | None,
        error: str,
        *,
        manual: bool,
        now: int,
        max_fails: int,
    ) -> tuple[int, bool]:
        """Возвращает (ошибок подряд, поставлена ли рассылка на автопаузу)."""
        async with self.db.session() as s:
            campaign = await s.get(Campaign, campaign_id)
            fails, paused = 0, False
            if campaign is not None:
                fails = (campaign.fail_count or 0) + 1
                campaign.fail_count = fails
                campaign.last_error = error[:1000]
                if not manual and fails >= max_fails and campaign.is_active:
                    campaign.is_active = False
                    campaign.next_slot_ts = campaign.next_run_ts = None
                    paused = True
            s.add(
                SendLog(
                    campaign_id=campaign_id,
                    chat_id=chat_id,
                    post_id=post_id,
                    status="failed",
                    manual=manual,
                    error=error[:1000],
                    ts=now,
                )
            )
            await s.commit()
            return fails, paused

    async def reschedule(
        self,
        planner: Callable[[Campaign], tuple[int | None, int | None]],
        campaign_ids: Iterable[int] | None = None,
        *,
        only_missing: bool = False,
    ) -> list[Campaign]:
        """Пересчитывает next_run. Возвращает рассылки, которые пришлось выключить
        (например, период уже закончился)."""
        finished: list[Campaign] = []
        async with self.db.session() as s:
            query = select(Campaign).where(Campaign.chat_id.is_not(None))
            if campaign_ids is not None:
                ids = list(campaign_ids)
                if not ids:
                    return finished
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
                    campaign.is_active = False
                    finished.append(campaign)
            await s.commit()
        return finished

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
