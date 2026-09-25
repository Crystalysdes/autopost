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
CHATS = (-1001000000001, -1001000000002, -1001000000003)


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


async def add_chat(app, tg_id: int, *, title: str | None = None, can_post: bool = True):
    change = await app.repo.upsert_chat_membership(
        tg_id=tg_id,
        title=title or f"Чат {tg_id}",
        username=None,
        chat_type="supergroup",
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=can_post,
        can_pin=True,
        actor_id=OWNER_ID,
        admin_ids=[OWNER_ID],
    )
    return change.chat


async def make_campaign(app, *, times=("09:00",), posts=("Пост 1",), chats=(CHAT_TG,), **options):
    chat_rows = [await add_chat(app, tg_id) for tg_id in chats]
    campaign = await app.repo.create_campaign("Реклама", chat_ids=[c.id for c in chat_rows])
    for text in posts:
        await app.repo.add_post(campaign.id, kind="text", payload={"text": text})
    await app.repo.update_campaign(campaign.id, times=list(times), is_active=True, **options)
    await app.scheduler.reschedule([campaign.id])
    return await app.repo.get_campaign(campaign.id), (chat_rows[0] if len(chat_rows) == 1 else chat_rows)


async def link_of(app, campaign_id: int, chat_id: int):
    targets = {t.chat.id: t.link for t in await app.repo.campaign_targets(campaign_id)}
    return targets[chat_id]


async def run_tick(scheduler: Scheduler) -> None:
    await scheduler.tick()
    await scheduler.drain()


def sent_to_chat(session, chat_tg=CHAT_TG):
    return [c for c in session.calls(SendMessage) if c.chat_id == chat_tg]


def owner_messages(session) -> list[str]:
    return [c.text for c in session.calls(SendMessage) if c.chat_id == OWNER_ID]


async def test_publishes_at_slot_and_advances(app, scheduler, clock, session):
    campaign, chat = await make_campaign(app, times=("09:00", "18:00"))
    assert campaign.next_run_ts == int(at(9))

    clock.value = at(8, 59)
    await run_tick(scheduler)
    assert not sent_to_chat(session)

    clock.value = at(9) + 5
    await run_tick(scheduler)
    calls = sent_to_chat(session)
    assert len(calls) == 1 and calls[0].text == "Пост 1" and calls[0].parse_mode is None

    fresh = await app.repo.get_campaign(campaign.id)
    link = await link_of(app, campaign.id, chat.id)
    assert fresh.next_slot_ts == int(at(18)) and link.sent_count == 1 and link.last_message_ids
    await run_tick(scheduler)  # тот же момент — повторной отправки нет
    assert len(sent_to_chat(session)) == 1


async def test_one_campaign_publishes_to_every_chat(app, scheduler, clock, session):
    campaign, chats = await make_campaign(app, chats=CHATS, posts=("A", "B"), times=("09:00", "10:00"))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert [c.text for tg in CHATS for c in sent_to_chat(session, tg)] == ["A", "A", "A"]
    clock.value = at(10) + 1
    await run_tick(scheduler)
    assert [c.text for c in sent_to_chat(session, CHATS[1])] == ["A", "B"]  # очередь постов общая
    for chat in chats:
        assert (await link_of(app, campaign.id, chat.id)).sent_count == 2
    assert (await app.repo.log_counts_since(0)) == {"sent": 6}


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


async def test_previous_post_is_tracked_per_chat(app, scheduler, clock, session):
    await make_campaign(app, chats=CHATS[:2], times=("09:00", "10:00"), delete_prev=True)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    first_ids = {tg: sent_to_chat(session, tg) for tg in CHATS[:2]}
    assert all(len(calls) == 1 for calls in first_ids.values())
    clock.value = at(10) + 1
    await run_tick(scheduler)
    deletes = {d.chat_id: d.message_ids for d in session.calls(DeleteMessages)}
    assert set(deletes) == set(CHATS[:2])
    assert deletes[CHATS[0]] != deletes[CHATS[1]]  # у каждого чата свои сообщения


async def test_pin_without_delete_unpins_previous(app, scheduler, clock, session):
    await make_campaign(app, times=("09:00", "10:00"), pin=True)
    for hour in (9, 10):
        clock.value = at(hour) + 1
        await run_tick(scheduler)
    unpins = session.calls(UnpinChatMessage)
    assert len(unpins) == 1 and unpins[0].message_id == session.calls(PinChatMessage)[0].message_id


async def test_forum_topic_is_per_chat(app, scheduler, clock, session):
    campaign, chats = await make_campaign(app, chats=CHATS[:2])
    await app.repo.update_target(campaign.id, chats[0].id, thread_id=45)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert sent_to_chat(session, CHATS[0])[0].message_thread_id == 45
    assert sent_to_chat(session, CHATS[1])[0].message_thread_id is None


async def test_no_duplicate_after_crash_between_claim_and_send(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app)
    clock.value = at(9) + 1

    # «Падение»: слот занят, но до отправки дело не дошло
    expected = campaign.next_run_ts
    claim = await app.repo.claim_run(
        campaign.id, expected, lambda c, posts: scheduler._decide(c, posts, expected), scheduler.now()
    )
    assert claim is not None and len(claim.targets) == 1
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


async def test_without_available_chats_slot_passes_quietly(app, scheduler, clock, session):
    campaign = await app.repo.create_campaign("Без чатов")
    for text in ("A", "B"):
        await app.repo.add_post(campaign.id, kind="text", payload={"text": text})
    await app.repo.update_campaign(campaign.id, times=["09:00", "10:00"], is_active=True)
    await scheduler.reschedule([campaign.id])
    clock.value = at(9) + 1
    await run_tick(scheduler)
    fresh = await app.repo.get_campaign(campaign.id)
    assert session.calls(SendMessage) == []  # и уведомлений тоже нет
    assert fresh.is_active and fresh.next_slot_ts == int(at(10)) and fresh.last_post_id is None
    assert (await app.repo.log_counts_since(0)) == {"skipped": 1}

    # Появился чат — публикация начинается с первого поста
    chat = await add_chat(app, CHAT_TG)
    await app.repo.set_target(campaign.id, chat.id, True)
    clock.value = at(10) + 1
    await run_tick(scheduler)
    assert [c.text for c in sent_to_chat(session)] == ["A"]


async def test_chat_without_post_right_is_skipped(app, scheduler, clock, session):
    await make_campaign(app, chats=CHATS[:2])
    await add_chat(app, CHATS[1], can_post=False)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session, CHATS[0])) == 1 and sent_to_chat(session, CHATS[1]) == []


async def test_failure_in_one_chat_does_not_stop_others(app, scheduler, clock, session):
    campaign, chats = await make_campaign(app, chats=CHATS)

    def fail_second(method):
        if method.chat_id == CHATS[1]:
            raise TelegramBadRequest(method=method, message="Bad Request: not enough rights to send")
        return session._default(method)

    session.handlers[SendMessage] = fail_second
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session, CHATS[0])) == 1 and len(sent_to_chat(session, CHATS[2])) == 1
    failed = await link_of(app, campaign.id, chats[1].id)
    assert failed.fail_count == 1 and "нет прав" in failed.last_error and not failed.paused
    assert (await link_of(app, campaign.id, chats[0].id)).fail_count == 0
    messages = owner_messages(session)
    assert len(messages) == 1 and "Не удалось опубликовать" in messages[0] and "В остальных чатах" in messages[0]


async def test_auto_pause_only_for_failing_chat(app, scheduler, clock, session):
    times = tuple(f"{h:02d}:00" for h in range(9, 10 + MAX_FAILS))
    campaign, chats = await make_campaign(app, times=times, chats=CHATS[:2])

    def fail_first(method):
        if method.chat_id == CHATS[0]:
            raise TelegramBadRequest(method=method, message="Bad Request: not enough rights to send")
        return session._default(method)

    session.handlers[SendMessage] = fail_first
    for index in range(MAX_FAILS):
        clock.value = at(9 + index) + 1
        await run_tick(scheduler)
    paused = await link_of(app, campaign.id, chats[0].id)
    assert paused.paused and paused.fail_count == MAX_FAILS and "нет прав" in paused.last_error
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active  # рассылка работает дальше
    messages = owner_messages(session)
    assert sum("Не удалось опубликовать" in m for m in messages) == 1  # одно уведомление на серию
    assert "приостановлены" in messages[-1]

    attempts = len(sent_to_chat(session, CHATS[0]))
    clock.value = at(9 + MAX_FAILS) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session, CHATS[0])) == attempts  # на паузе — не трогаем
    assert len(sent_to_chat(session, CHATS[1])) == MAX_FAILS + 1


async def test_forbidden_marks_chat_left_and_others_continue(app, scheduler, clock, session):
    campaign, chats = await make_campaign(app, chats=CHATS[:2], times=("09:00", "10:00"))
    kicked = TelegramForbiddenError(method=None, message="Forbidden: bot was kicked from the supergroup chat")
    session.queue(SendMessage, kicked)  # первым отправляется в CHATS[0]
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert (await app.repo.get_chat(chats[0].id)).status == "left"
    assert len(sent_to_chat(session, CHATS[1])) == 1
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active and fresh.next_run_ts
    messages = owner_messages(session)
    assert len(messages) == 1 and "потерял доступ" in messages[0]

    clock.value = at(10) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session, CHATS[0])) == 1  # в чат без бота больше не шлём
    assert len(sent_to_chat(session, CHATS[1])) == 2


async def test_network_error_is_not_retried(app, scheduler, clock, session):
    campaign, chat = await make_campaign(app, times=("09:00",))
    session.queue(SendMessage, TelegramNetworkError(method=None, message="Request timeout error"))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session)) == 1  # одна попытка, без повторов
    fresh = await app.repo.get_campaign(campaign.id)
    assert (await link_of(app, campaign.id, chat.id)).fail_count == 1
    assert fresh.next_slot_ts == int(at(9, day=26))


async def test_retry_after_short_wait_retries_once(app, scheduler, clock, session):
    await make_campaign(app)
    session.queue(SendMessage, TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=3))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert len(sent_to_chat(session)) == 2
    assert (await app.repo.log_counts_since(0)) == {"sent": 1}


async def test_migration_during_send(app, scheduler, clock, session):
    campaign, chat = await make_campaign(app, chats=(-5,))
    session.queue(SendMessage, TelegramMigrateToChat(method=None, message="migrated", migrate_to_chat_id=-1009999))
    clock.value = at(9) + 1
    await run_tick(scheduler)
    assert (await app.repo.get_chat(chat.id)).tg_id == -1009999
    assert [c.chat_id for c in session.calls(SendMessage) if c.chat_id in (-5, -1009999)] == [-5, -1009999]
    assert (await link_of(app, campaign.id, chat.id)).sent_count == 1


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


async def test_send_now_summary_for_several_chats(app, scheduler, session):
    campaign, _ = await make_campaign(app, chats=CHATS)
    session.queue(SendMessage, TelegramBadRequest(method=None, message="Bad Request: not enough rights to send"))
    ok, message = await scheduler.send_now(campaign.id)
    assert ok and "опубликован в 2 из 3 чатов" in message and "нет прав" in message
    assert owner_messages(session) == []  # ручная отправка отвечает на экране, без уведомлений


async def test_send_now_without_chats(app, scheduler, session):
    campaign = await app.repo.create_campaign("R")
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    ok, message = await scheduler.send_now(campaign.id)
    assert not ok and "Нет чатов" in message
    assert session.calls(SendMessage) == []


async def test_period_end_finishes_campaign(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("09:00",), end_date="2026-09-25")
    clock.value = at(9) + 1
    await run_tick(scheduler)
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active is False and fresh.next_run_ts is None
    assert any("завершена" in m for m in owner_messages(session))


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
    assert fresh.is_active is False and fresh.last_error == "В рассылке нет постов"
    assert any("не осталось постов" in m for m in owner_messages(session))
