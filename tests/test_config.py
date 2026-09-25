from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.config import Config, parse_ids


def make(**env: str) -> Config:
    return Config(_env_file=None, bot_token="1:x", **env)


def test_admin_ids_from_comma_list():
    assert make(admin_ids="123, 456").admin_ids == [123, 456]
    assert make(admin_ids="123 456;789").admin_ids == [123, 456, 789]


def test_legacy_admin_id_still_works_and_merges():
    assert make(admin_id="555").admin_ids == [555]
    assert make(admin_ids="1,2", admin_id="2").admin_ids == [1, 2]


def test_admin_ids_from_environment(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ADMIN_IDS", "111,222")
    assert Config(_env_file=None).admin_ids == [111, 222]


@pytest.mark.parametrize("value", ["", "abc", "12,x"])
def test_bad_admin_ids_rejected(value):
    with pytest.raises(ValidationError):
        make(admin_ids=value)


def test_parse_ids_variants():
    assert parse_ids(None) == []
    assert parse_ids(7) == [7]
    assert parse_ids([1, "2"]) == [1, 2]
