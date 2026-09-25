"""Проверки перед запуском рассылки."""

from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from bot.db.models import Campaign, Chat, Post
from bot.services.schedule_utils import next_slot


def schedule_problems(campaign: Campaign, posts: Sequence[Post], tz: ZoneInfo, now: int) -> list[str]:
    problems = []
    if not posts:
        problems.append("Сначала добавьте хотя бы один пост")
    if not campaign.times:
        problems.append("Сначала задайте время публикаций")
    if not campaign.weekdays:
        problems.append("Выберите хотя бы один день недели")
    if (
        campaign.times
        and campaign.weekdays
        and next_slot(campaign.times, campaign.weekdays, tz, now, campaign.start_date, campaign.end_date) is None
    ):
        problems.append("Период публикаций уже закончился — измените даты в расписании")
    return problems


def chat_problems(chat: Chat | None) -> list[str]:
    if chat is None or chat.status != "active":
        return ["Чат недоступен: бот не состоит в нём или чат не подтверждён"]
    if not chat.can_post:
        return ["У бота нет права публиковать в этом чате"]
    return []


def readiness_problems(
    campaign: Campaign, posts: Sequence[Post], chat: Chat | None, tz: ZoneInfo, now: int
) -> list[str]:
    return chat_problems(chat) + schedule_problems(campaign, posts, tz, now)
