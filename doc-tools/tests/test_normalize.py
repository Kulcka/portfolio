from datetime import date, datetime

import pytest

from doctools.normalize import (_W11, _W12, _ctrl, amount_in_words, initials, inn_is_valid, normalize_email,
                                normalize_inn, normalize_phone, parse_date, parse_number, title_name)
from doctools.samples import BAD_INN, demo_inn


@pytest.mark.parametrize("raw, expected", [
    ("1 250,50", 1250.5), ("1,250.50", 1250.5), ("1.250,50", 1250.5), ("12 500 ₽", 12500),
    ("15 000 руб.", 15000), ("2 400", 2400), ("-5,5", -5.5), ("(100)", -100), ("3200.00", 3200.0),
    ("1 234 567", 1234567), (42, 42), ("по запросу", None), ("00123", None), ("", None), ("1.2.3", None),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("05.09.2026", date(2026, 9, 5)), ("5/9/26", date(2026, 9, 5)), ("2026-09-05", date(2026, 9, 5)),
    ("7 сентября 2026", date(2026, 9, 7)), ("1 мая 2026 г.", date(2026, 5, 1)), ("3 марта 2026 года", date(2026, 3, 3)),
    ("05.09.2026 14:30", datetime(2026, 9, 5, 14, 30)), (datetime(2026, 9, 1), date(2026, 9, 1)),
    ("31.02.2026", None), ("завтра", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("8 (900) 000-12-34", "+7 (900) 000-12-34"), ("89000001234", "+7 (900) 000-12-34"),
    ("+7 900 000 12 34", "+7 (900) 000-12-34"), ("900-000-12-34", "+7 (900) 000-12-34"),
    (79000001234, "+7 (900) 000-12-34"),
    ("8 900 000 12 34, 8 (495) 000-00-01", "+7 (900) 000-12-34, +7 (495) 000-00-01"),
    ("000-67-89", None), ("12345", None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_inn():
    good = demo_inn(1001)
    assert inn_is_valid(good)
    assert normalize_inn(good) == (good, "")
    assert normalize_inn(int(good)) == (good, "восстановлены ведущие нули")   # Excel хранил числом
    assert normalize_inn(BAD_INN) == (BAD_INN, "контрольные цифры не сходятся")
    assert normalize_inn("abc")[0] is None
    body = "0000000001"
    d11 = body + str(_ctrl(body, _W11))
    inn12 = d11 + str(_ctrl(d11, _W12))
    assert inn_is_valid(inn12) and not inn_is_valid(inn12[:-1] + str((int(inn12[-1]) + 1) % 10))


def test_email_and_names():
    assert normalize_email(" PETROV@Example.COM ") == "petrov@example.com"
    assert normalize_email("vasilieva@example") is None
    assert title_name("  иванова  анна-мария ") == "Иванова Анна-Мария"
    assert initials("Иванова Анна Сергеевна") == "Иванова А. С."


@pytest.mark.parametrize("raw, expected", [
    (1250000, "Один миллион двести пятьдесят тысяч рублей 00 копеек"),
    (384500.5, "Триста восемьдесят четыре тысячи пятьсот рублей 50 копеек"),
    (2001001, "Два миллиона одна тысяча один рубль 00 копеек"),
    (21.01, "Двадцать один рубль 01 копейка"),
    (0, "Ноль рублей 00 копеек"),
    (112, "Сто двенадцать рублей 00 копеек"),
    (45210.75, "Сорок пять тысяч двести десять рублей 75 копеек"),
])
def test_amount_in_words(raw, expected):
    assert amount_in_words(raw) == expected
