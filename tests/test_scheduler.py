from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import DeleteMessages, PinChatMessage, SendMessage, UnpinChatMessage

from bot.services import scheduler as scheduler_module
from bot.services.scheduler import GRACE_SECONDS, MAX_FAILS, Scheduler
from tests.conftest import OWNER_ID, FakeClock

MSK = ZoneInfo("Europe/Moscow")
CHAT_TG = -1001111111111


def at(hour: int, minute: int = 0, day: int = 25) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK).timestamp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(at(8, 0))


@pytest.fixture
def scheduler(app, clock, monkeypatch) -> Scheduler:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", no_sleep)
    instance = Scheduler(app, clock=clock, rng=random.Random(0))
    app.scheduler = instance
    return instance


async def make_campaign(app, *, times=("09:00",), posts=("Пост 1",), chat_tg=CHAT_TG, **options):
    change = await app.repo.upsert_chat_membership(
        tg_id=chat_tg,
        title="Чат",
        username=None,
        chat_type="supergroup",
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=True,
        can_pin=True,
        actor_id=OWNER_ID,
        admin_ids=[OWNER_ID],
    )
    campaign = await app.repo.create_campaign(change.chat.id, "Реклама")
    for text in posts:
        await app.repo.add_post(campaign.id, kind="text", payload={"text": text})
    await app.repo.update_campaign(campaign.id, times=list(times), is_active=True, **options)
    await app.scheduler.reschedule([campaign.id])
    return await app.repo.get_campaign(campaign.id), change.chat


async def run_tick(scheduler: Scheduler) -> None:
    await scheduler.tick()
    await scheduler.drain()


def sent_to_chat(session, chat_tg=CHAT_TG):
    return [c for c in session.calls(SendMessage) if c.chat_id == chat_tg]


async def test_publishes_at_slot_and_advances(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("09:00", "18:00"))
    assert campaign.next_run_ts == int(at(9))

    clock.value = at(8, 59)
    await run_tick(scheduler)
    assert not sent_to_chat(session)

    clock.value = at(9) + 5
    await run_tick(scheduler)
    calls = sent_to_chat(session)
    assert len(calls) == 1 and calls[0].text == "Пост 1" and calls[0].parse_mode is None

    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_slot_ts == int(at(18)) and fresh.sent_count == 1 and fresh.last_message_ids
    await run_tick(scheduler)  # тот же момент — повторной отправки нет
    assert len(sent_to_chat(session)) == 1


async def test_rotation_sequential(app, scheduler, clock, session):
    await make_campaign(app, times=("09:00", "10:00", "11:00"), posts=("A", "B"))
    for hour in (9, 10, 11):
        clock.value = at(hour) + 1
        await run_tick(scheduler)
    assert [c.text for c in sent_to_chat(session)] == ["A", "B", "A"]


async def test_delete_previous_and_pin(app, scheduler, clock, session):
    await make_campaign(app, times=("09:00", "10:00"), delete_prev=True, pin=True)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    pins = session.calls(PinChatMessage)
    assert len(pins) == 1 and pins[0].disable_notification is True

    clock.value = at(10) + 1
    await run_tick(scheduler)
    deletes = session.calls(DeleteMessages)
    assert len(deletes) == 1 and deletes[0].chat_id == CHAT_TG
    assert deletes[0].message_ids == [pins[0].message_id]
    assert len(session.calls(PinChatMessage)) == 2
    assert not session.calls(UnpinChatMessage)  # прошлый удалён — откреплять нечего


async def test_pin_without_delete_unpins_previous(app, scheduler, clock, session):
    await make_campaign(app, times=("09:00", "10:00"), pin=True)
    for hour in (9, 10):
        clock.value = at(hour) + 1
        await run_tick(scheduler)
    unpins = session.calls(UnpinChatMessage)
    assert len(unpins) == 1 and unpins[0].message_id == session.calls(PinChatMessage)[0].message_id


async def test_no_duplicate_after_crash_between_claim_and_send(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app)
    clock.value = at(9) + 1

    # «Падение»: слот занят, но до отправки дело не дошло
    expected = campaign.next_run_ts
    claim = await app.repo.claim_run(
        campaign.id, expected, lambda c, posts: scheduler._decide(c, posts, expected), scheduler.now()
    )
    assert claim is not None
    await run_tick(scheduler)
    assert sent_to_chat(session) == []
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_slot_ts == int(at(9, day=26))


async def test_overdue_slot_is_skipped(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app)
    clock.value = at(9) + GRACE_SECONDS + 60
    await run_tick(scheduler)
    assert sent_to_chat(session) == []
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_slot_ts == int(at(9, day=26)) and fresh.is_active
    assert (await app.repo.log_counts_since(0)) == {"skipped": 1}


async def test_within_grace_still_posts(app, scheduler, clock, session):
    await make_campaign(app)
    clock.value = at(9) + GRACE_SECONDS - 60
    await run_tick(scheduler)
    assert len(sent_to_chat(session)) == 1


async def test_forbidden_marks_chat_left_and_notifies(app, scheduler, clock, session):
    campaign, chat = await make_campaign(app)
    kicked = TelegramForbiddenError(method=None, message="Forbidden: bot was kicked from the supergroup chat")
    session.queue(SendMessage, kicked)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert (await app.repo.get_chat(chat.id)).status == "left"
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active is False and fresh.next_run_ts is None
    notifications = [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID]
    assert notifications and "потерял доступ" in notifications[-1].text


async def test_auto_pause_after_repeated_failures(app, scheduler, clock, session):
    times = tuple(f"{h:02d}:00" for h in range(9, 9 + MAX_FAILS))
    campaign, _ = await make_campaign(app, times=times)
    for index in range(MAX_FAILS):
        session.queue(SendMessage, TelegramBadRequest(method=None, message="Bad Request: not enough rights to send"))
        clock.value = at(9 + index) + 1
        await run_tick(scheduler)
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active is False and fresh.fail_count == MAX_FAILS
    assert "нет прав" in fresh.last_error
    owner_messages = [c.text for c in session.calls(SendMessage) if c.chat_id == OWNER_ID]
    assert sum("Не удалось опубликовать" in m for m in owner_messages) == 1  # одно уведомление на серию
    assert "остановлена" in owner_messages[-1]


async def test_network_error_is_not_retried(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("09:00",))
    session.queue(SendMessage, TelegramNetworkError(method=None, message="Request timeout error"))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session)) == 1  # одна попытка, без повторов
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.fail_count == 1 and fresh.next_slot_ts == int(at(9, day=26))


async def test_retry_after_short_wait_retries_once(app, scheduler, clock, session):
    await make_campaign(app)
    session.queue(SendMessage, TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=3))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session)) == 2
    assert (await app.repo.log_counts_since(0)) == {"sent": 1}


async def test_migration_during_send(app, scheduler, clock, session):
    _, chat = await make_campaign(app, chat_tg=-5)
    session.queue(SendMessage, TelegramMigrateToChat(method=None, message="migrated", migrate_to_chat_id=-1009999))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert (await app.repo.get_chat(chat.id)).tg_id == -1009999
    assert [c.chat_id for c in session.calls(SendMessage) if c.chat_id in (-5, -1009999)] == [-5, -1009999]


async def test_send_now_keeps_schedule(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, posts=("A", "B"))
    ok, message = await scheduler.send_now(campaign.id)
    assert ok and "#1" in message
    ok, message = await scheduler.send_now(campaign.id)
    assert ok and "#2" in message
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_run_ts == campaign.next_run_ts
    assert [c.text for c in sent_to_chat(session)] == ["A", "B"]


async def test_send_now_reports_error(app, scheduler, session):
    campaign, _ = await make_campaign(app)
    session.queue(SendMessage, TelegramBadRequest(method=None, message="Bad Request: message thread not found"))
    ok, message = await scheduler.send_now(campaign.id)
    assert not ok and "Тема форума" in message


async def test_period_end_finishes_campaign(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("09:00",), end_date="2026-09-25")
    clock.value = at(9) + 1
    await run_tick(scheduler)
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active is False and fresh.next_run_ts is None
    owner_messages = [c.text for c in session.calls(SendMessage) if c.chat_id == OWNER_ID]
    assert any("завершена" in m for m in owner_messages)


async def test_paused_all_blocks_publishing(app, scheduler, clock, session):
    await make_campaign(app)
    await app.settings.set(app.repo, "paused_all", True)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert sent_to_chat(session) == []


async def test_campaign_without_posts_stops(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, posts=())
    clock.value = at(9) + 1
    await run_tick(scheduler)
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active is False
    assert any("не осталось постов" in c.text for c in session.calls(SendMessage) if c.chat_id == OWNER_ID)
