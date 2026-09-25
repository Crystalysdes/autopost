"""Планировщик публикаций.

Раз в TICK_SECONDS ищем рассылки, которым пора публиковать. Каждая обрабатывается
отдельной задачей под своим замком (он общий с «Отправить сейчас»).

Порядок для слота:
1. claim — одной транзакцией сдвигаем расписание вперёд (UPDATE ... WHERE next_run_ts = старое).
   Только после этого отправляем: даже если процесс упадёт сразу после отправки,
   этот слот повторно не уйдёт.
2. пост уходит по очереди в каждый отмеченный чат рассылки (вне транзакции);
3. в каждом чате — удаление/открепление прошлого поста, закрепление нового;
4. запись результата по каждому чату. Ошибка в одном чате не мешает остальным.
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
from bot.db.repo import Claim, ClaimDecision, Target
from bot.services import chats as chat_service
from bot.services.errors import humanize
from bot.services.schedule_utils import ScheduleSpec, effective_jitter, following_slots, plan_next
from bot.services.sender import PostData, PostSendError, SendResult, send_post
from bot.ui import keyboards, texts

logger = logging.getLogger(__name__)

TICK_SECONDS = 10
GRACE_SECONDS = 15 * 60  # насколько можно опоздать со слотом (например, после перезапуска)
MAX_PARALLEL = 5
MAX_FAILS = 5  # после стольких ошибок подряд публикации в чат встают на паузу
RETRY_AFTER_LIMIT = 60  # флуд-контроль: ждём и повторяем, только если пауза не дольше минуты
TARGET_GAP = 0.05  # пауза между чатами одной рассылки — бережём лимиты Telegram


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
class Outcome:
    """Итог публикации в один чат."""

    chat: Chat
    ok: bool
    error: str | None = None
    fails: int = 0  # ошибок подряд в этом чате
    paused: bool = False  # чат рассылки только что поставлен на паузу
    lost: bool = False  # бота нет в чате


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

    def reschedule_base(self, campaign: Campaign, now: int) -> int:
        """С какого момента искать следующий слот при пересчёте (правка расписания, запуск, смена пояса…).

        Слот, который уже ушёл раньше срока из-за разброса, повторно не планируем, а слот,
        чей запуск с разбросом ещё впереди, не теряем.
        """
        if campaign.last_slot_ts is None:
            return now
        jitter = effective_jitter(campaign.times or [], campaign.jitter_min or 0) * 60
        return max(campaign.last_slot_ts, now - jitter)

    async def reschedule(self, campaign_ids: Iterable[int] | None = None, *, only_missing: bool = False) -> None:
        now = self.now()
        stalled = await self.app.repo.reschedule(
            lambda c: self.plan(c, self.reschedule_base(c, now)), campaign_ids, only_missing=only_missing
        )
        for campaign in stalled:
            logger.info("Рассылка %s включена, но ближайших публикаций нет (дни/период)", campaign.id)

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
                if self.app.settings.paused_all:  # пока задача ждала очереди, всё могли поставить на паузу
                    return
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
        if slot is not None and slot <= now:
            # Запуск опоздал (например, бот перезапускался): прошедшие слоты не догоняем пачкой
            slot, run = self.plan(campaign, now)
        post = None if overdue else pick_post(posts, campaign.last_post_id, campaign.rotation, self.rng)
        return ClaimDecision(next_slot_ts=slot, next_run_ts=run, post=post, skipped=overdue, finished=slot is None)

    async def _handle_claim(self, claim: Claim) -> None:
        campaign, decision = claim.campaign, claim.decision
        if decision.skipped:
            logger.info("Рассылка %s: слот пропущен (бот был недоступен)", campaign.id)
        elif decision.post is None:
            await self.app.repo.stop_campaign(campaign.id, "В рассылке нет постов", now=self.now())
            await self.app.notify(texts.no_posts_text(campaign.name), keyboards.open_campaign(campaign.id))
            return
        elif not claim.targets:
            logger.info("Рассылка %s: нет чатов, куда можно публиковать — слот пропущен", campaign.id)
        else:
            outcomes = await self._deliver_all(campaign, claim.targets, decision.post, manual=False)
            await self._report(campaign, outcomes)
        if decision.finished:
            await self.app.notify(texts.finished_text(campaign.name), keyboards.open_campaign(campaign.id))

    # --------------------------------------------------------------- отправка

    async def send_now(self, campaign_id: int) -> tuple[bool, str]:
        """«Отправить сейчас»: следующий по очереди пост во все доступные чаты, расписание не меняется."""
        if self.lock(campaign_id).locked():
            return False, "Пост этой рассылки уже отправляется — подождите пару секунд"
        async with self.lock(campaign_id):
            repo = self.app.repo
            campaign = await repo.get_campaign(campaign_id)
            if campaign is None or campaign.is_draft:
                return False, "Рассылка не найдена"
            targets = [t for t in await repo.campaign_targets(campaign.id) if t.deliverable]
            if not targets:
                return False, "Нет чатов, куда бот может публиковать — отметьте их в «💬 Чаты»"
            posts = await repo.list_posts(campaign.id)
            post = pick_post(posts, campaign.last_post_id, campaign.rotation, self.rng)
            if post is None:
                return False, "В рассылке нет постов"
            await repo.mark_post_used(campaign.id, post.id)
            number = [p.id for p in posts].index(post.id) + 1
            outcomes = await self._deliver_all(campaign, targets, post, manual=True)
            return send_summary(number, outcomes)

    async def _deliver_all(
        self, campaign: Campaign, targets: list[Target], post: Post, *, manual: bool
    ) -> list[Outcome]:
        outcomes = []
        for index, target in enumerate(targets):
            if index:
                await asyncio.sleep(TARGET_GAP)
            outcomes.append(await self._deliver(campaign, target, post, manual=manual))
        return outcomes

    async def _send(self, tg_id: int, post: PostData, campaign: Campaign, thread_id: int | None) -> SendResult:
        for attempt in (1, 2):
            try:
                return await send_post(
                    self.app.bot,
                    tg_id,
                    post,
                    silent=campaign.silent,
                    protect=campaign.protect,
                    thread_id=thread_id,
                )
            except TelegramRetryAfter as error:
                if attempt == 1 and error.retry_after <= RETRY_AFTER_LIMIT:
                    await asyncio.sleep(error.retry_after + 1)
                    continue
                raise
        raise AssertionError("unreachable")

    async def _deliver(self, campaign: Campaign, target: Target, post: Post, *, manual: bool) -> Outcome:
        chat = target.chat
        tg_id = chat.tg_id
        data = PostData.of(post)
        thread_id = target.link.thread_id
        prev_ids = [i for i in (target.link.last_message_ids or []) if i]
        try:
            try:
                result = await self._send(tg_id, data, campaign, thread_id)
            except TelegramMigrateToChat as error:
                await chat_service.migrate(self.app, tg_id, error.migrate_to_chat_id)
                tg_id = error.migrate_to_chat_id
                # Запись чата могла слиться с новой; id старых сообщений в супергруппе чужие
                chat = await self.app.repo.get_chat_by_tg(tg_id) or chat
                prev_ids = []
                result = await self._send(tg_id, data, campaign, thread_id)
        except TelegramForbiddenError as error:
            return await self._chat_lost(campaign, chat, post, humanize(error), manual=manual)
        except TelegramBadRequest as error:
            if "chat not found" in error.message.lower():
                return await self._chat_lost(campaign, chat, post, humanize(error), manual=manual)
            return await self._failed(campaign, chat, post, humanize(error), manual=manual)
        except PostSendError as error:
            return await self._failed(campaign, chat, post, str(error), manual=manual)
        except TelegramAPIError as error:
            # Сюда попадают и таймауты: пост мог уже уйти, поэтому не повторяем — дубль хуже пропуска
            return await self._failed(campaign, chat, post, humanize(error), manual=manual)
        except Exception as error:
            logger.exception("Непредвиденная ошибка отправки")
            return await self._failed(campaign, chat, post, f"Непредвиденная ошибка: {error}", manual=manual)

        warning = await self._after_send(tg_id, campaign, result, prev_ids)
        await self.app.repo.record_target_sent(
            campaign.id, chat.id, post.id, result.usable_ids, manual=manual, now=self.now(), warning=warning
        )
        logger.info("Опубликовано: рассылка %s -> чат %s (%s)", campaign.id, tg_id, result.message_ids)
        return Outcome(chat=chat, ok=True, error=warning)

    async def _after_send(self, tg_id: int, campaign: Campaign, result: SendResult, prev_ids: list[int]) -> str | None:
        bot = self.app.bot
        new_ids = result.usable_ids
        warnings: list[str] = []
        deleted = False
        if campaign.delete_prev and prev_ids:
            try:
                await bot.delete_messages(chat_id=tg_id, message_ids=prev_ids)
                deleted = True
            except TelegramAPIError as error:
                # Старше 48 часов или уже удалено вручную — это не ошибка рассылки
                logger.info("Не удалось удалить прошлый пост в %s: %s", tg_id, error)
        if campaign.pin and prev_ids and not deleted:
            # Прошлый пост остался в чате — снимаем с него закреп, чтобы закрепы не копились
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

    async def _failed(self, campaign: Campaign, chat: Chat, post: Post, error: str, *, manual: bool) -> Outcome:
        fails, paused = await self.app.repo.record_target_failed(
            campaign.id, chat.id, post.id, error, manual=manual, now=self.now(), max_fails=MAX_FAILS
        )
        logger.warning("Ошибка публикации: рассылка %s -> %s: %s", campaign.id, chat.tg_id, error)
        return Outcome(chat=chat, ok=False, error=error, fails=fails, paused=paused)

    async def _chat_lost(self, campaign: Campaign, chat: Chat, post: Post, reason: str, *, manual: bool) -> Outcome:
        """Бота удалили из чата: чат пропускается всеми рассылками, пока бота не вернут."""
        await self.app.repo.record_target_failed(
            campaign.id, chat.id, post.id, reason, manual=manual, now=self.now(), max_fails=None
        )
        await self.app.repo.set_chat_status(chat.id, "left")
        await self.app.notify(texts.chat_lost_text(chat.title, reason), keyboards.open_chat(chat.id))
        return Outcome(chat=chat, ok=False, error=reason, lost=True)

    async def _report(self, campaign: Campaign, outcomes: list[Outcome]) -> None:
        """Одно уведомление на запуск, а не на каждый чат. О повторных ошибках в том же чате
        не напоминаем — только о первой в серии и об автопаузе."""
        paused = [(o.chat.title, o.error or "") for o in outcomes if o.paused]
        if paused:
            await self.app.notify(
                texts.auto_paused_text(campaign.name, paused, MAX_FAILS), keyboards.campaign_problem(campaign.id)
            )
        fresh = [(o.chat.title, o.error or "") for o in outcomes if not o.ok and not o.lost and o.fails == 1]
        if fresh and self.app.settings.notify_errors:
            await self.app.notify(
                texts.send_failed_text(campaign.name, fresh, len(outcomes)), keyboards.open_campaign(campaign.id)
            )


def send_summary(number: int, outcomes: list[Outcome]) -> tuple[bool, str]:
    """Итог «Отправить сейчас» для экрана рассылки."""
    sent = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]
    if len(outcomes) == 1:
        if sent:
            return True, f"Пост #{number} опубликован в «{sent[0].chat.title}»"
        return False, failed[0].error or "Не удалось отправить"
    problems = "; ".join(f"«{o.chat.title}»: {o.error}" for o in failed[:5])
    if len(failed) > 5:
        problems += f" и ещё {len(failed) - 5}"
    if not sent:
        return False, problems
    chats = texts.plural(len(outcomes), "чата", "чатов", "чатов")
    text = f"Пост #{number} опубликован в {len(sent)} из {len(outcomes)} {chats}"
    if failed:
        text += f". Не удалось: {problems}"
    return True, text
