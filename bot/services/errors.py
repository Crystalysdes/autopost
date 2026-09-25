"""Понятные тексты для частых ошибок Telegram."""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

_KNOWN = (
    ("not enough rights", "У бота нет прав отправлять сообщения в этот чат"),
    ("have no rights", "У бота нет прав отправлять сообщения в этот чат"),
    ("need administrator rights", "Боту нужны права администратора в этом чате"),
    ("chat_write_forbidden", "Боту запрещено писать в этот чат"),
    ("message thread not found", "Тема форума не найдена — проверьте тему в «💬 Чаты» рассылки"),
    ("topic_closed", "Тема форума закрыта"),
    ("topic_deleted", "Тема форума удалена — выберите другую в «💬 Чаты» рассылки"),
    ("chat not found", "Чат не найден — возможно, бота удалили"),
    ("bot was kicked", "Бота удалили из чата"),
    ("not a member", "Бот больше не состоит в чате"),
    ("wrong file identifier", "Файл поста больше недоступен — замените пост"),
    ("file reference", "Файл поста больше недоступен — замените пост"),
    ("button_url_invalid", "Неверная ссылка в кнопке — исправьте кнопки поста"),
    ("message is too long", "Текст поста слишком длинный"),
    ("caption is too long", "Подпись слишком длинная (боту доступно до 1024 символов)"),
    ("slowmode", "В чате включён медленный режим"),
)


def humanize(error: BaseException) -> str:
    if isinstance(error, TelegramNetworkError):
        return "Нет связи с Telegram — пост мог не уйти"
    message = error.message if isinstance(error, TelegramAPIError) else str(error)
    lowered = message.lower()
    for needle, text in _KNOWN:
        if needle in lowered:
            return text
    return message or error.__class__.__name__
