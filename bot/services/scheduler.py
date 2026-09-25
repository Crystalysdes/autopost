"""Планировщик публикаций.

Раз в TICK_SECONDS ищем рассылки, которым пора публиковать. Каждая обрабатывается
отдельной задачей под своим замком (он общий с «Отправить сейчас»).

Порядок для слота:
1. claim — одной транзакцией сдвигаем расписание вперёд (UPDATE ... WHERE next_run_ts = старое).
   Только после этого отправляем: даже если процесс упадёт сразу после отправки,
   этот слот повторно не уйдёт.
2. отправка вне транзакции;
3. удаление/открепление прошлого поста, закрепление нового;
4. запись результата.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)

from bot.app import App
from bot.db.models import Campaign, Chat, Post
from bot.db.repo import Claim, ClaimDecision
from bot.services import chats as chat_service
from bot.services.errors import humanize
from bot.services.schedule_utils import ScheduleSpec, effective_jitter, following_slots, plan_next
from bot.services.sender import PostData, PostSendError, SendResult, send_post
from bot.ui import keyboards, texts

logger = logging.getLogger(__name__)

TICK_SECONDS = 10
GRACE_SECONDS = 15 * 60  # насколько можно опоздать со слотом (например, после перезапуска)
MAX_PARALLEL = 5
MAX_FAILS = 5  # после стольких ошибок подряд рассылка встаёт на паузу
RETRY_AFTER_LIMIT = 60  # флуд-контроль: ждём и повторяем, только если пауза не дольше минуты


def spec_of(campaign: Campaign) -> ScheduleSpec:
    return ScheduleSpec(
        times=campaign.times or [],
        weekdays=campaign.weekdays or [],
        jitter_min=campaign.jitter_min or 0,
        start_date=campaign.start_date,
        end_date=campaign.end_date,
    )


def pick_post(posts: Sequence[Post], last_post_id: int | None, rotation: str, rng: random.Random) -> Post | None:
    if not posts:
        return None
    if len(posts) == 1:
        return posts[0]
    if rotation == "random":
        candidates = [p for p in posts if p.id != last_post_id] or list(posts)
        return rng.choice(candidates)
    ids = [p.id for p in posts]
    if last_post_id in ids:
        return posts[(ids.index(last_post_id) + 1) % len(posts)]
    return posts[0]


def predict_runs(
    campaign: Campaign, posts: Sequence[Post], tz: ZoneInfo, count: int = 3
) -> list[tuple[int, bool, int | None]]:
    """Ближайшие запуски: [(время, приблизительно_ли, номер поста или None)]."""
    if not campaign.is_active or campaign.next_run_ts is None or campaign.next_slot_ts is None:
        return []
    spec = spec_of(campaign)
    slots = [campaign.next_run_ts, *following_slots(spec, tz, campaign.next_slot_ts, count - 1)]
    approx = effective_jitter(spec.times, spec.jitter_min) > 0
    numbers: list[int | None]
    if len(posts) == 1:
        numbers = [1] * len(slots)
    elif posts and campaign.rotation == "sequential":
        ids = [p.id for p in posts]
        index = ids.index(campaign.last_post_id) if campaign.last_post_id in ids else -1
        numbers = []
        for _ in slots:
            index = (index + 1) % len(ids)
            numbers.append(index + 1)
    else:
        numbers = [None] * len(slots)
    return [(ts, approx and i > 0, numbers[i]) for i, ts in enumerate(slots)]


@dataclass
class Delivery:
    campaign: Campaign
    chat: Chat
    post: Post
    manual: bool
    finished: bool = False


class Scheduler:
    def __init__(
        self,
        app: App,
        *,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.app = app
        self.clock = clock
        self.rng = rng or random.Random()
        self.tick_seconds = tick_seconds
        self._locks: dict[int, asyncio.Lock] = {}
        self._inflight: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL)
        self._stop = asyncio.Event()

    def now(self) -> int:
        return int(self.clock())

    def lock(self, campaign_id: int) -> asyncio.Lock:
        return self._locks.setdefault(campaign_id, asyncio.Lock())

    # ------------------------------------------------------------------ расчёт

    def plan(self, campaign: Campaign, after_ts: int) -> tuple[int | None, int | None]:
        return plan_next(spec_of(campaign), self.app.settings.tz, after_ts, self.now(), self.rng)

    async def reschedule(self, campaign_ids: Iterable[int] | None = None, *, only_missing: bool = False) -> None:
        """Пересчёт от текущего момента: после правки расписания, запуска, смены пояса и т.п."""
        now = self.now()
        finished = await self.app.repo.reschedule(lambda c: self.plan(c, now), campaign_ids, only_missing=only_missing)
        for campaign in finished:
            logger.info("Рассылка %s выключена: в расписании больше нет слотов", campaign.id)

    # ------------------------------------------------------------------- цикл

    async def run(self) -> None:
        logger.info("Планировщик запущен (проверка каждые %s с)", self.tick_seconds)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Ошибка в цикле планировщика")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
        await self.drain()

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> None:
        if self.app.settings.paused_all:
            return
        for campaign_id, run_ts in await self.app.repo.due_runs(self.now()):
            if campaign_id in self._inflight:
                continue
            self._inflight.add(campaign_id)
            task = asyncio.create_task(self._process(campaign_id, run_ts))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Дождаться всех начатых отправок (для остановки и тестов)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _process(self, campaign_id: int, run_ts: int) -> None:
        try:
            async with self._semaphore, self.lock(campaign_id):
                claim = await self.app.repo.claim_run(
                    campaign_id, run_ts, lambda c, posts: self._decide(c, posts, run_ts), self.now()
                )
                if claim is not None:
                    await self._handle_claim(claim)
        except Exception:
            logger.exception("Ошибка при публикации рассылки %s", campaign_id)
        finally:
            self._inflight.discard(campaign_id)

    def _decide(self, campaign: Campaign, posts: list[Post], run_ts: int) -> ClaimDecision:
        now = self.now()
        overdue = now - run_ts > GRACE_SECONDS
        # Следующий слот считаем от прошлого БАЗОВОГО слота, а не от момента отправки:
        # иначе разброс «+10 мин» съел бы соседний слот через 10 минут.
        base = now if overdue else (campaign.next_slot_ts or now)
        slot, run = self.plan(campaign, base)
        post = None if overdue else pick_post(posts, campaign.last_post_id, campaign.rotation, self.rng)
        return ClaimDecision(next_slot_ts=slot, next_run_ts=run, post=post, skipped=overdue, finished=slot is None)

    async def _handle_claim(self, claim: Claim) -> None:
        campaign, chat, decision = claim.campaign, claim.chat, claim.decision
        if decision.skipped:
            logger.info("Рассылка %s: слот пропущен (бот был недоступен)", campaign.id)
            if decision.finished:
                await self._notify_finished(campaign, chat)
            return
        if decision.post is None:
            await self.app.repo.record_failed(
                campaign.id, chat.id, None, "В рассылке нет постов", manual=False, now=self.now(), max_fails=1
            )
            await self.app.repo.update_campaign(campaign.id, is_active=False, next_slot_ts=None, next_run_ts=None)
            await self.app.notify(texts.no_posts_text(campaign.name, chat.title), keyboards.open_campaign(campaign.id))
            return
        await self._deliver(
            Delivery(campaign=campaign, chat=chat, post=decision.post, manual=False, finished=decision.finished)
        )

    # --------------------------------------------------------------- отправка

    async def send_now(self, campaign_id: int) -> tuple[bool, str]:
        """«Отправить сейчас»: следующий по очереди пост, расписание не меняется."""
        async with self.lock(campaign_id):
            repo = self.app.repo
            campaign = await repo.get_campaign(campaign_id)
            if campaign is None or campaign.chat_id is None:
                return False, "Рассылка не найдена"
            chat = await repo.get_chat(campaign.chat_id)
            if chat is None or chat.status != "active":
                return False, "Чат недоступен: бот не состоит в нём или чат не подтверждён"
            posts = await repo.list_posts(campaign.id)
            post = pick_post(posts, campaign.last_post_id, campaign.rotation, self.rng)
            if post is None:
                return False, "В рассылке нет постов"
            await repo.mark_post_used(campaign.id, post.id)
            number = [p.id for p in posts].index(post.id) + 1
            result = await self._deliver(Delivery(campaign=campaign, chat=chat, post=post, manual=True))
            if result is None:
                fresh = await repo.get_campaign(campaign.id)
                return False, (fresh.last_error if fresh and fresh.last_error else "Не удалось отправить")
            return True, f"Пост #{number} опубликован в «{chat.title}»"

    async def _send(self, tg_id: int, post: PostData, campaign: Campaign) -> SendResult:
        for attempt in (1, 2):
            try:
                return await send_post(
                    self.app.bot,
                    tg_id,
                    post,
                    silent=campaign.silent,
                    protect=campaign.protect,
                    thread_id=campaign.thread_id,
                )
            except TelegramRetryAfter as error:
                if attempt == 1 and error.retry_after <= RETRY_AFTER_LIMIT:
                    await asyncio.sleep(error.retry_after + 1)
                    continue
                raise
        raise AssertionError("unreachable")

    async def _deliver(self, delivery: Delivery) -> SendResult | None:
        campaign, chat = delivery.campaign, delivery.chat
        tg_id = chat.tg_id
        post = PostData.of(delivery.post)
        try:
            try:
                result = await self._send(tg_id, post, campaign)
            except TelegramMigrateToChat as error:
                await chat_service.migrate(self.app, tg_id, error.migrate_to_chat_id)
                tg_id = error.migrate_to_chat_id
                result = await self._send(tg_id, post, campaign)
        except TelegramForbiddenError as error:
            await self._chat_lost(delivery, humanize(error))
            return None
        except TelegramBadRequest as error:
            if "chat not found" in error.message.lower():
                await self._chat_lost(delivery, humanize(error))
            else:
                await self._failed(delivery, humanize(error))
            return None
        except PostSendError as error:
            await self._failed(delivery, str(error))
            return None
        except TelegramAPIError as error:
            # Сюда попадают и таймауты: пост мог уже уйти, поэтому не повторяем — дубль хуже пропуска
            await self._failed(delivery, humanize(error))
            return None
        except Exception as error:
            logger.exception("Непредвиденная ошибка отправки")
            await self._failed(delivery, f"Непредвиденная ошибка: {error}")
            return None

        warning = await self._after_send(tg_id, campaign, result)
        await self.app.repo.record_sent(
            campaign.id,
            chat.id,
            delivery.post.id,
            result.usable_ids,
            manual=delivery.manual,
            now=self.now(),
            warning=warning,
        )
        logger.info("Опубликовано: рассылка %s -> чат %s (%s)", campaign.id, tg_id, result.message_ids)
        if delivery.finished:
            await self._notify_finished(campaign, chat)
        return result

    async def _after_send(self, tg_id: int, campaign: Campaign, result: SendResult) -> str | None:
        bot = self.app.bot
        new_ids = result.usable_ids
        prev_ids = [i for i in (campaign.last_message_ids or []) if i]
        warnings: list[str] = []
        if campaign.delete_prev and prev_ids:
            try:
                await bot.delete_messages(chat_id=tg_id, message_ids=prev_ids)
            except TelegramAPIError as error:
                # Старше 48 часов или уже удалено вручную — это не ошибка рассылки
                logger.info("Не удалось удалить прошлый пост в %s: %s", tg_id, error)
        elif campaign.pin and prev_ids:
            try:
                await bot.unpin_chat_message(chat_id=tg_id, message_id=prev_ids[0])
            except TelegramAPIError as error:
                logger.info("Не удалось открепить прошлый пост в %s: %s", tg_id, error)
        if campaign.pin and new_ids:
            try:
                await bot.pin_chat_message(chat_id=tg_id, message_id=new_ids[0], disable_notification=True)
            except TelegramAPIError as error:
                warnings.append(f"не удалось закрепить: {humanize(error)}")
        if result.icons_dropped:
            warnings.append("иконки на кнопках недоступны боту — отправлено без них")
        return "; ".join(warnings) or None

    async def _failed(self, delivery: Delivery, error_text: str) -> None:
        campaign, chat = delivery.campaign, delivery.chat
        fails, paused = await self.app.repo.record_failed(
            campaign.id,
            chat.id,
            delivery.post.id,
            error_text,
            manual=delivery.manual,
            now=self.now(),
            max_fails=MAX_FAILS,
        )
        logger.warning("Ошибка публикации: рассылка %s -> %s: %s", campaign.id, chat.tg_id, error_text)
        if delivery.manual:
            return
        if paused:
            await self.app.notify(
                texts.auto_paused_text(campaign.name, chat.title, fails, error_text),
                keyboards.open_campaign(campaign.id),
            )
        elif fails == 1 and self.app.settings.notify_errors:
            await self.app.notify(
                texts.send_failed_text(campaign.name, chat.title, error_text),
                keyboards.open_campaign(campaign.id),
            )

    async def _chat_lost(self, delivery: Delivery, reason: str) -> None:
        campaign, chat = delivery.campaign, delivery.chat
        await self.app.repo.record_failed(
            campaign.id,
            chat.id,
            delivery.post.id,
            reason,
            manual=delivery.manual,
            now=self.now(),
            max_fails=MAX_FAILS,
        )
        await self.app.repo.set_chat_status(chat.id, "left")
        await self.app.notify(texts.chat_lost_text(chat.title, reason), keyboards.open_chat(chat.id))

    async def _notify_finished(self, campaign: Campaign, chat: Chat) -> None:
        await self.app.notify(texts.finished_text(campaign.name, chat.title), keyboards.open_campaign(campaign.id))
