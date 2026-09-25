from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import Message

from bot.services.content import ContentError, capture, custom_emoji_count, post_text, post_title

OWNER = {"id": 1000, "is_bot": False, "first_name": "Owner"}
PRIVATE = {"id": 1000, "type": "private"}


def msg(message_id: int = 1, **fields: Any) -> Message:
    return Message.model_validate(
        {"message_id": message_id, "date": 1_700_000_000, "chat": PRIVATE, "from": OWNER, **fields}
    )


def photo(file_id: str) -> list[dict[str, Any]]:
    return [
        {"file_id": f"{file_id}_s", "file_unique_id": "s", "width": 90, "height": 90},
        {"file_id": file_id, "file_unique_id": "b", "width": 1280, "height": 1280},
    ]


def test_text_keeps_entities_and_link_preview_exactly():
    text = "  🔥 Акция!  "
    captured = capture(
        msg(
            text=text,
            entities=[
                {"type": "custom_emoji", "offset": 2, "length": 2, "custom_emoji_id": "5368324170671202286"},
                {"type": "bold", "offset": 5, "length": 5},
            ],
            link_preview_options={"is_disabled": True},
        )
    )
    assert captured.kind == "text"
    assert captured.payload["text"] == text  # без strip — иначе съедут смещения
    assert captured.payload["entities"][0] == {
        "type": "custom_emoji",
        "offset": 2,
        "length": 2,
        "custom_emoji_id": "5368324170671202286",
    }
    assert captured.payload["link_preview"] == {"is_disabled": True}
    assert captured.forward_from is None
    assert custom_emoji_count(captured.kind, captured.payload) == 1


def test_photo_with_spoiler_and_caption_above():
    captured = capture(
        msg(
            photo=photo("big"),
            caption="Подпись",
            caption_entities=[{"type": "italic", "offset": 0, "length": 7}],
            has_media_spoiler=True,
            show_caption_above_media=True,
        )
    )
    assert captured.kind == "photo"
    assert captured.payload == {
        "items": [
            {
                "type": "photo",
                "file_id": "big",
                "caption": "Подпись",
                "caption_entities": [{"type": "italic", "offset": 0, "length": 7}],
                "has_spoiler": True,
                "show_caption_above_media": True,
            }
        ]
    }


def test_gif_is_animation_not_document():
    animation = {"file_id": "gif1", "file_unique_id": "g", "width": 1, "height": 1, "duration": 1}
    document = {"file_id": "gif1", "file_unique_id": "g"}
    captured = capture(msg(animation=animation, document=document))
    assert captured.kind == "animation"


def test_live_photo_checked_before_photo():
    live = {"file_id": "live1", "file_unique_id": "l", "width": 1, "height": 1, "duration": 3}
    captured = capture(msg(live_photo=live, photo=photo("still")))
    assert captured.payload["items"][0] == {
        "type": "live_photo",
        "file_id": "live1",
        "photo_file_id": "still",
    }


def test_album_sorted_with_per_item_captions():
    first = msg(2, photo=photo("p2"), media_group_id="g", caption="Вторая")
    second = msg(1, photo=photo("p1"), media_group_id="g", caption="Первая")
    video = msg(
        3,
        video={"file_id": "v1", "file_unique_id": "v", "width": 1, "height": 1, "duration": 1},
        media_group_id="g",
    )
    captured = capture(first, [first, second, video])
    assert captured.kind == "album"
    assert [i["file_id"] for i in captured.payload["items"]] == ["p1", "p2", "v1"]
    assert captured.payload["items"][0]["caption"] == "Первая"
    assert post_title(captured.kind, captured.payload) == "🗂 Альбом (3)"
    assert post_text(captured.kind, captured.payload) == "Первая"


def test_forward_records_source_and_imports_buttons():
    forwarded = msg(
        7,
        text="Пост из канала",
        forward_origin={
            "type": "channel",
            "date": 1,
            "chat": {"id": -100500, "type": "channel", "title": "Канал"},
            "message_id": 3,
        },
        reply_markup={
            "inline_keyboard": [[{"text": "Сайт", "url": "https://s.ru"}, {"text": "cb", "callback_data": "x"}]]
        },
    )
    captured = capture(forwarded)
    assert captured.forward_from == {"chat_id": 1000, "message_ids": [7]}
    assert captured.buttons == [[{"text": "Сайт", "url": "https://s.ru"}]]


def test_long_caption_rejected():
    with pytest.raises(ContentError, match="1024"):
        capture(msg(photo=photo("p"), caption="x" * 1025))


@pytest.mark.parametrize(
    "fields",
    [
        {
            "poll": {
                "id": "1",
                "question": "?",
                "options": [],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": True,
                "type": "regular",
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            }
        },
        {"location": {"latitude": 1.0, "longitude": 2.0}},
        {"contact": {"phone_number": "+1", "first_name": "A"}},
        {"dice": {"emoji": "🎲", "value": 3}},
    ],
)
def test_unsupported_types_rejected(fields):
    with pytest.raises(ContentError):
        capture(msg(**fields))


def test_empty_service_message_rejected():
    with pytest.raises(ContentError):
        capture(msg(new_chat_title="x"))
