"""Проверка и нормализация российских номеров к виду +7XXXXXXXXXX."""

from __future__ import annotations

import re

_ALLOWED_CHARS = re.compile(r"^[\d\s()\-.+]+$")
# Первая цифра десятизначного номера зоны +7: 3, 4, 8, 9 — Россия, 6, 7 — Казахстан.
_VALID_FIRST_DIGITS = frozenset("346789")


class PhoneValidationError(ValueError):
    """Строка не похожа на номер телефона зоны +7."""


def normalize_phone(raw: str) -> str:
    """Привести номер к виду `+7XXXXXXXXXX`.

    Принимает привычные записи: `+7 (999) 123-45-67`, `8 999 123 45 67`,
    `89991234567`, `9991234567`, `7-999-123-45-67`. Отклоняет буквы, лишние
    или недостающие цифры и чужие коды стран.
    """
    text = raw.strip()
    if not text or not _ALLOWED_CHARS.match(text):
        raise PhoneValidationError("номер может содержать только цифры, пробелы, скобки, дефисы и +")
    if text.count("+") > 1 or ("+" in text and not text.startswith("+")):
        raise PhoneValidationError("знак + допустим только в начале номера")

    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        if len(digits) != 11 or not digits.startswith("7"):
            raise PhoneValidationError("ожидается номер в формате +7XXXXXXXXXX")
        national = digits[1:]
    elif len(digits) == 11 and digits[0] in "78":
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        raise PhoneValidationError("в номере должно быть 10 цифр после +7 или 8")

    if national[0] not in _VALID_FIRST_DIGITS:
        raise PhoneValidationError("такого кода нет в зоне +7")
    return "+7" + national


def format_phone(normalized: str) -> str:
    """`+79991234567` → `+7 999 123-45-67` (для людей)."""
    if not (len(normalized) == 12 and normalized.startswith("+7") and normalized[1:].isdigit()):
        return normalized
    n = normalized[2:]
    return f"+7 {n[:3]} {n[3:6]}-{n[6:8]}-{n[8:]}"


def mask_phone(normalized: str) -> str:
    """`+79991234567` → `+7 999 ***-**-67` (для уведомлений без полного номера)."""
    if not (len(normalized) == 12 and normalized.startswith("+7") and normalized[1:].isdigit()):
        return "***"
    n = normalized[2:]
    return f"+7 {n[:3]} ***-**-{n[8:]}"
