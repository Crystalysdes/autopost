"""Состояния ожидания ввода."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Input(StatesGroup):
    rename = State()
    posts = State()  # приём постов пачкой до «Готово»
    replace = State()
    buttons = State()
    times = State()
    count = State()
    window = State()
    start_date = State()
    end_date = State()
    thread = State()
    timezone = State()
    spam_words = State()
    spam_allow = State()


class Picker(StatesGroup):
    picking = State()
