"""Хранилище состояний ввода (FSM) в памяти."""

from __future__ import annotations

from copy import copy
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StateType, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


class LeanMemoryStorage(MemoryStorage):
    """Как MemoryStorage, но чтение не создаёт записей, а пустые записи удаляются.

    aiogram читает состояние для каждого апдейта — в том числе для каждого сообщения в группах,
    где бот админ. Обычный MemoryStorage (defaultdict) заводил бы запись на каждую пару
    «группа + участник» и рос бы без конца.
    """

    async def get_state(self, key: StorageKey) -> str | None:
        record = self.storage.get(key)
        return record.state if record else None

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = self.storage.get(key)
        return record.data.copy() if record else {}

    async def get_value(self, storage_key: StorageKey, dict_key: str, default: Any | None = None) -> Any | None:
        record = self.storage.get(storage_key)
        return copy(record.data.get(dict_key, default)) if record else default

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        if value is None and key not in self.storage:
            return
        await super().set_state(key, value)
        self._drop_if_empty(key)

    async def set_data(self, key: StorageKey, data: Any) -> None:
        if not data and key not in self.storage:
            return
        await super().set_data(key, data)
        self._drop_if_empty(key)

    def _drop_if_empty(self, key: StorageKey) -> None:
        record = self.storage.get(key)
        if record is not None and record.state is None and not record.data:
            del self.storage[key]
