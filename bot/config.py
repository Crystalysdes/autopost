"""Настройки из переменных окружения и файла .env."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def parse_ids(value: Any) -> list[int]:
    """«123, 456» / «123 456» / 123 / [123, 456] -> [123, 456]."""
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(part) for part in re.split(r"[\s,;]+", value.strip()) if part]
    return [int(item) for item in value]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    # Telegram ID админов через запятую. У всех админов одинаковый полный доступ.
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Старое имя параметра (один админ) — поддерживается для совместимости
    admin_id: Annotated[list[int], NoDecode] = Field(default_factory=list)
    timezone: str = "Europe/Moscow"
    db_path: Path = Path("data/autopost.db")
    log_level: str = "INFO"

    @field_validator("admin_ids", "admin_id", mode="before")
    @classmethod
    def _split_ids(cls, value: Any) -> list[int]:
        return parse_ids(value)

    @model_validator(mode="after")
    def _merge_admins(self) -> Config:
        merged = list(dict.fromkeys([*self.admin_ids, *self.admin_id]))
        if not merged:
            raise ValueError("не указаны ADMIN_IDS — Telegram ID админов через запятую")
        self.admin_ids = merged
        self.admin_id = []
        return self

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Неизвестный часовой пояс: {value}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()
