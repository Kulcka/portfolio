"""Проверка телефона, имени и комментария."""

from __future__ import annotations

import pytest

from leads_bot.core.phone import PhoneValidationError, format_phone, mask_phone, normalize_phone
from leads_bot.core.validation import CommentTooLongError, NameValidationError, normalize_comment, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (999) 123-45-67", "+79991234567"),
        ("+79991234567", "+79991234567"),
        ("89991234567", "+79991234567"),
        ("8 999 123 45 67", "+79991234567"),
        ("8-999-123-45-67", "+79991234567"),
        ("7 999 123 45 67", "+79991234567"),
        ("79991234567", "+79991234567"),  # так номер присылает Telegram-контакт
        ("9991234567", "+79991234567"),
        ("(999) 123.45.67", "+79991234567"),
        ("  +7 495 123-45-67  ", "+74951234567"),  # городской московский
        ("8 800 555-35-35", "+78005553535"),
        ("+7 701 123 45 67", "+77011234567"),  # Казахстан, та же зона +7
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "12345",
        "999123456",  # 9 цифр
        "899912345678",  # 12 цифр
        "+8 999 123-45-67",  # чужой код страны
        "+380 50 123 45 67",
        "+1 202 555 0100",
        "9 99 12 34 56 7a",
        "звоните вечером",
        "7+9991234567",  # плюс не в начале
        "++79991234567",
        "0991234567",  # в зоне +7 нет кодов на 0
        "+7 199 123-45-67",
        "+7 599 123-45-67",
    ],
)
def test_normalize_invalid(raw: str) -> None:
    with pytest.raises(PhoneValidationError):
        normalize_phone(raw)


def test_format_and_mask() -> None:
    assert format_phone("+79991234567") == "+7 999 123-45-67"
    assert mask_phone("+79991234567") == "+7 999 ***-**-67"
    assert format_phone("не номер") == "не номер"
    assert mask_phone("не номер") == "***"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Иван", "Иван"),
        ("  Анна   Мария ", "Анна Мария"),
        ("Салтыков-Щедрин", "Салтыков-Щедрин"),
        ("O'Neil", "O'Neil"),
        ("Ёж", "Ёж"),
        ("Асель Нурланқызы", "Асель Нурланқызы"),
    ],
)
def test_name_valid(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "И", "1234", "Иван123", "<script>", "-", "И" * 61, "@ivan", "Иван 😀"])
def test_name_invalid(raw: str) -> None:
    with pytest.raises(NameValidationError):
        normalize_name(raw)


def test_comment() -> None:
    assert normalize_comment("  нужна смета  ") == "нужна смета"
    assert normalize_comment("x" * 1000) == "x" * 1000
    with pytest.raises(CommentTooLongError):
        normalize_comment("x" * 1001)
