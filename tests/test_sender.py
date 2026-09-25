from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import ForwardMessages, SendMediaGroup, SendMessage, SendPhoto, SendVideoNote

from bot.services.sender import PostData, PostSendError, received_custom_emoji, send_post

CHAT = -1001234567890
ENTITIES = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "777"}]


async def test_text_post_sends_entities_without_parse_mode(bot, session):
    post = PostData(
        id=1,
        kind="text",
        payload={"text": "🔥 hi", "entities": ENTITIES, "link_preview": {"is_disabled": True}},
        buttons=[[{"text": "Сайт", "url": "https://a.ru", "style": "success"}]],
    )
    result = await send_post(bot, CHAT, post, silent=True, protect=True, thread_id=5)
    call = session.calls(SendMessage)[0]
    assert call.parse_mode is None
    assert call.entities[0].custom_emoji_id == "777"
    assert call.link_preview_options.is_disabled is True
    assert call.disable_notification is True and call.protect_content is True
    assert call.message_thread_id == 5
    assert call.reply_markup.inline_keyboard[0][0].style == "success"
    assert len(result.message_ids) == 1
    assert received_custom_emoji(result.messages) == 1


async def test_photo_keeps_spoiler_and_caption_position(bot, session):
    post = PostData(
        id=1,
        kind="photo",
        payload={
            "items": [
                {
                    "type": "photo",
                    "file_id": "F",
                    "caption": "c",
                    "has_spoiler": True,
                    "show_caption_above_media": True,
                }
            ]
        },
    )
    await send_post(bot, CHAT, post)
    call = session.calls(SendPhoto)[0]
    assert call.photo == "F"
    assert call.has_spoiler is True and call.show_caption_above_media is True
    assert call.parse_mode is None


async def test_album_items_have_no_parse_mode(bot, session):
    post = PostData(
        id=1,
        kind="album",
        payload={
            "items": [
                {"type": "photo", "file_id": "A", "caption": "🔥", "caption_entities": ENTITIES},
                {"type": "video", "file_id": "B", "has_spoiler": True},
                {"type": "live_photo", "file_id": "L", "photo_file_id": "P"},
            ]
        },
    )
    result = await send_post(bot, CHAT, post)
    call = session.calls(SendMediaGroup)[0]
    assert [m.parse_mode for m in call.media] == [None, None, None]
    assert call.media[0].caption_entities[0].custom_emoji_id == "777"
    assert call.media[1].has_spoiler is True
    assert call.media[2].photo == "P"
    assert len(result.message_ids) == 3


async def test_video_note_has_no_caption_fields(bot, session):
    await send_post(
        bot,
        CHAT,
        PostData(id=1, kind="video_note", payload={"items": [{"type": "video_note", "file_id": "V"}]}),
    )
    assert session.calls(SendVideoNote)[0].video_note == "V"


async def test_forward_mode(bot, session):
    post = PostData(
        id=1,
        kind="text",
        payload={"text": "x"},
        forward_from={"chat_id": 1000, "message_ids": [5, 6]},
        send_mode="forward",
    )
    result = await send_post(bot, CHAT, post, silent=True)
    call = session.calls(ForwardMessages)[0]
    assert call.from_chat_id == 1000 and call.message_ids == [5, 6]
    assert call.disable_notification is True
    assert result.forwarded and len(result.message_ids) == 2
    assert not session.calls(SendMessage)


async def test_forward_of_deleted_source_raises(bot, session):
    session.queue(ForwardMessages, [])
    post = PostData(
        id=1,
        kind="text",
        payload={"text": "x"},
        forward_from={"chat_id": 1, "message_ids": [5]},
        send_mode="forward",
    )
    with pytest.raises(PostSendError):
        await send_post(bot, CHAT, post)


async def test_icons_rejected_retry_without_icons(bot, session):
    session.queue(SendMessage, TelegramBadRequest(method=None, message="Bad Request: BUTTON_ICON_CUSTOM_EMOJI_INVALID"))
    post = PostData(
        id=1,
        kind="text",
        payload={"text": "x"},
        buttons=[[{"text": "A", "url": "https://a.ru", "icon_custom_emoji_id": "5"}]],
    )
    result = await send_post(bot, CHAT, post)
    calls = session.calls(SendMessage)
    assert calls[0].reply_markup.inline_keyboard[0][0].icon_custom_emoji_id == "5"
    assert calls[1].reply_markup.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert result.icons_dropped


async def test_other_bad_request_propagates(bot, session):
    session.queue(SendMessage, TelegramBadRequest(method=None, message="Bad Request: chat not found"))
    with pytest.raises(TelegramBadRequest):
        await send_post(bot, CHAT, PostData(id=1, kind="text", payload={"text": "x"}))
