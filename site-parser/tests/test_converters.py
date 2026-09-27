"""Приведение типов: числа, цены в разных форматах, валюта, наличие, ссылки."""

from __future__ import annotations

import pytest

from site_parser.converters import (
    convert,
    parse_number,
    to_availability,
    to_bool,
    to_currency,
    to_int,
    to_price,
    to_str,
    to_url,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("£51.77", 51.77),
        ("1 299,00 ₽", 1299.0),
        ("1 299 руб.", 1299.0),
        ("$1,299.99", 1299.99),
        ("1.299,99 €", 1299.99),
        ("12,5", 12.5),
        ("12,345", 12345.0),
        ("0,345", 0.345),
        ("1.299.000", 1299000.0),
        ("1'299.50 CHF", 1299.5),
        ("от 1 500 ₽", 1500.0),
        ("−15%", -15.0),
        ("-3.5", -3.5),
        ("Цена: 990.", 990.0),
        ("In stock (22 available)", 22.0),
    ],
)
def test_parse_number_formats(text: str, expected: float) -> None:
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "нет цены", "Бесплатно", None])
def test_parse_number_without_digits(text: str | None) -> None:
    assert parse_number(text) is None


def test_parse_number_passes_numbers_and_rejects_bool() -> None:
    assert parse_number(5) == 5.0
    assert parse_number(2.5) == 2.5
    assert parse_number(True) is None


def test_price_is_rounded_to_cents() -> None:
    assert to_price("£51.7749") == 51.77
    assert to_price("нет") is None


def test_int_rounds_and_extracts() -> None:
    assert to_int("22 available") == 22
    assert to_int("1 299 шт.") == 1299
    assert to_int("4.6") == 5
    assert to_int("—") is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("£51.77", "GBP"),
        ("1 299 ₽", "RUB"),
        ("1 299 руб.", "RUB"),
        ("1299 р.", "RUB"),
        ("Цена 100 RUB", "RUB"),
        ("100 RUR", "RUB"),
        ("$10", "USD"),
        ("10 USD", "USD"),
        ("9,99 €", "EUR"),
        ("5000 ₸", "KZT"),
        ("5000 тенге", "KZT"),
        ("200 грн", "UAH"),
        ("15 Br", "BYN"),
    ],
)
def test_currency_detection(text: str, code: str) -> None:
    assert to_currency(text) == code


def test_currency_unknown() -> None:
    assert to_currency("100") is None
    assert to_currency("стр. 5") is None
    assert to_currency(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("In stock (22 available)", True),
        ("In stock", True),
        ("Out of stock", False),
        ("В наличии", True),
        ("Нет в наличии", False),
        ("Под заказ", False),
        ("Товар отсутствует", False),
        ("Есть на складе", True),
        ("Sold out", False),
        ("5 шт.", True),
        ("0 шт.", False),
        ("уточняйте", None),
        ("", None),
    ],
)
def test_availability(text: str, expected: bool | None) -> None:
    assert to_availability(text) is expected


def test_availability_passes_bool_from_map() -> None:
    assert to_availability(False) is False
    assert to_availability(True) is True


@pytest.mark.parametrize(("text", "expected"), [("Да", True), ("yes", True), ("нет", False), ("0", False), ("?", None)])
def test_bool(text: str, expected: bool | None) -> None:
    assert to_bool(text) is expected


def test_str_normalizes_whitespace() -> None:
    assert to_str("  A\n\n  Light in   the Attic ") == "A Light in the Attic"
    assert to_str("   ") is None


def test_url_joins_relative_and_skips_junk() -> None:
    base = "https://books.toscrape.com/catalogue/page-1.html"
    assert to_url("a-light_1000/index.html", base) == "https://books.toscrape.com/catalogue/a-light_1000/index.html"
    assert to_url("../media/x.jpg", "https://books.toscrape.com/catalogue/a/index.html") == (
        "https://books.toscrape.com/catalogue/media/x.jpg"
    )
    assert to_url("/page/2/", "https://quotes.toscrape.com/page/1/") == "https://quotes.toscrape.com/page/2/"
    assert to_url("#top", base) is None
    assert to_url("javascript:void(0)", base) is None


def test_convert_unknown_type() -> None:
    with pytest.raises(ValueError, match="неизвестный тип"):
        convert("1", "money")
