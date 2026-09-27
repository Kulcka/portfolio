"""Разбор и приведение к единому виду значений: числа, даты, телефоны, ИНН, e-mail, ФИО.

Все функции чистые: на входе значение из ячейки (строка, число, дата, None),
на выходе нормализованное значение или None, если распознать не удалось.
Внешние базы (ФНС и т.п.) не используются — ИНН проверяется только по
контрольным цифрам.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_SPACES = re.compile(r"[\s    ﻿]+")


def clean_spaces(value: str) -> str:
    """Неразрывные пробелы, табы, переводы строк -> один пробел; обрезка краёв."""
    return _SPACES.sub(" ", value).strip()


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and clean_spaces(value) == "")


def norm_key(value: Any) -> str:
    """Ключ для сравнения строк: регистр, ё/е, пробелы и пунктуация не важны."""
    s = clean_spaces(str(value)).casefold().replace("ё", "е")
    s = re.sub(r"[^\w]+", " ", s)
    return clean_spaces(s)


# --------------------------------------------------------------------- числа
_CURRENCY = re.compile(r"(?i)(₽|руб(лей|ля|ль)?\.?|р\.|rub|usd|eur|\$|€|%)")
_NUM_BODY = re.compile(r"[+-]?\d[\d.,]*")


def looks_like_code(text: str) -> bool:
    """Строка цифр с ведущим нулём или слишком длинная — это код, а не число."""
    digits = text.strip()
    return bool(re.fullmatch(r"0\d+", digits)) or bool(re.fullmatch(r"\d{16,}", digits))


def parse_number(value: Any) -> int | float | None:
    """Число из строки с любыми разделителями: «1 250,50», «1,250.50», «1.250,50», «12 500 ₽».

    Одна запятая считается десятичной (российская запись). Возвращает int, если
    дробной части нет, иначе float. None — если это не число.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return None
    s = clean_spaces(str(value))
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = _CURRENCY.sub("", s).replace(" ", "").replace("'", "").replace("−", "-")
    if not s or looks_like_code(s):
        return None
    if not _NUM_BODY.fullmatch(s):
        return None
    if s[0] in "+-":
        negative ^= s[0] == "-"
        s = s[1:]
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        int_part, _, frac = s.rpartition(dec)
        if not re.fullmatch(r"\d{1,3}(\%s\d{3})*" % thou, int_part) or not frac.isdigit():
            return None
        s = int_part.replace(thou, "") + "." + frac
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        if s.count(sep) > 1:
            if not re.fullmatch(r"\d{1,3}(\%s\d{3})+" % sep, s):
                return None
            s = s.replace(sep, "")
        else:
            int_part, _, frac = s.partition(sep)
            if not int_part.isdigit() or not frac.isdigit():
                return None
            s = int_part + "." + frac
    elif not s.isdigit():
        return None
    num: int | float = float(s) if "." in s else int(s)
    return -num if negative else num


# ---------------------------------------------------------------------- даты
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_RE_DMY = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_RE_YMD = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_RE_TEXT = re.compile(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s*(?:г\.?|года))?", re.I)


def parse_date(value: Any) -> date | datetime | None:
    """Дата из строки: 05.09.2026, 5/9/26, 2026-09-05, «5 сентября 2026 г.». День — первым.

    Если во входе есть время — возвращается datetime, иначе date.
    """
    if isinstance(value, datetime):
        return value if (value.hour, value.minute, value.second) != (0, 0, 0) else value.date()
    if isinstance(value, date):
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return None
    s = clean_spaces(str(value))
    if not s:
        return None
    m = _RE_YMD.fullmatch(s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return _mk(y, mo, d, m[4], m[5], m[6])
    m = _RE_DMY.fullmatch(s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), m[3]
        year = int(y) if len(y) == 4 else (2000 + int(y) if int(y) <= 69 else 1900 + int(y))
        return _mk(year, mo, d, m[4], m[5], m[6])
    m = _RE_TEXT.fullmatch(s)
    if m:
        word = m[2].lower().replace("ё", "е")
        month = next((n for stem, n in _MONTHS.items() if word.startswith(stem) and (stem != "ма" or word in ("мая", "май"))), None)
        if month:
            return _mk(int(m[3]), month, int(m[1]), None, None, None)
    return None


def _mk(y: int, mo: int, d: int, hh: str | None, mm: str | None, ss: str | None) -> date | datetime | None:
    try:
        if hh is not None:
            return datetime(y, mo, d, int(hh), int(mm or 0), int(ss or 0))
        return date(y, mo, d)
    except ValueError:
        return None


# ------------------------------------------------------------------ телефоны
def normalize_phone(value: Any) -> str | None:
    """Российский номер -> «+7 (900) 000-12-34». Несколько номеров в ячейке — через запятую.

    None, если хотя бы один номер не распознан (значение надо оставить как есть
    и показать заказчику).
    """
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    parts = [p for p in re.split(r"[,;/]|\s{2,}", str(value)) if p.strip()]
    out = []
    for part in parts:
        digits = re.sub(r"\D", "", part)
        if len(digits) == 11 and digits[0] in "78":
            digits = digits[1:]
        elif not (len(digits) == 10 and digits[0] in "3489"):
            return None
        out.append(f"+7 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}")
    return ", ".join(out) if out else None


# ----------------------------------------------------------------------- ИНН
_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _ctrl(digits: str, weights: tuple[int, ...]) -> int:
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10


def inn_is_valid(inn: str) -> bool:
    """Проверка контрольных цифр ИНН (10 цифр — организация, 12 — физлицо/ИП)."""
    if not inn.isdigit():
        return False
    if len(inn) == 10:
        return _ctrl(inn, _W10) == int(inn[9])
    if len(inn) == 12:
        return _ctrl(inn, _W11) == int(inn[10]) and _ctrl(inn, _W12) == int(inn[11])
    return False


def normalize_inn(value: Any) -> tuple[str | None, str]:
    """ИНН -> строка цифр. Возвращает (значение, пометка).

    Пометки: "" — всё в порядке; "восстановлены ведущие нули" — Excel хранил ИНН
    числом и потерял нули в начале (восстанавливаем, только если после этого
    сходятся контрольные цифры); "контрольные цифры не сходятся"; None-значение —
    не похоже на ИНН.
    """
    if value is None:
        return None, "пусто"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None, "не похоже на ИНН"
    if len(digits) in (10, 12):
        return digits, "" if inn_is_valid(digits) else "контрольные цифры не сходятся"
    for target in (10, 12):
        if len(digits) < target:
            padded = digits.zfill(target)
            if inn_is_valid(padded):
                return padded, "восстановлены ведущие нули"
    return None, "не похоже на ИНН"


# ------------------------------------------------------------ e-mail и ФИО
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def normalize_email(value: Any) -> str | None:
    s = clean_spaces(str(value)).lower().replace(" ", "")
    s = s.removeprefix("mailto:")
    return s if _EMAIL.match(s) else None


def title_name(value: Any) -> str:
    """«иванова  анна-мария» -> «Иванова Анна-Мария». Инициалы «а.с.» -> «А. С.»."""
    s = clean_spaces(str(value))
    s = re.sub(r"(?<=\w)\.(?=\w)", ". ", s)
    words = []
    for word in s.split(" "):
        words.append("-".join(p[:1].upper() + p[1:].lower() for p in word.split("-")))
    return " ".join(words)


def initials(full_name: Any) -> str:
    """«Иванова Анна Сергеевна» -> «Иванова А. С.»."""
    parts = title_name(full_name).split(" ")
    if len(parts) < 2:
        return " ".join(parts)
    return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:] if p)


# ------------------------------------------------------ форматы для вывода
def format_money(value: Any) -> str:
    num = parse_number(value)
    if num is None:
        return str(value)
    q = Decimal(str(num)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"{q:,.2f}".replace(",", " ").replace(".", ",")


def format_number(value: Any) -> str:
    num = parse_number(value)
    if num is None:
        return str(value)
    if isinstance(num, float) and not num.is_integer():
        return f"{num:,.2f}".replace(",", " ").replace(".", ",")
    return f"{int(num):,}".replace(",", " ")


def format_date(value: Any) -> str:
    d = parse_date(value)
    if d is None:
        return str(value)
    return d.strftime("%d.%m.%Y %H:%M") if isinstance(d, datetime) else d.strftime("%d.%m.%Y")


# ----------------------------------------------------- сумма прописью (руб.)
_UNITS_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]
_ORDERS = [
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
]


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _triad(n: int, female: bool) -> list[str]:
    words = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        words.append(_TENS[rest // 10])
        words.append((_UNITS_F if female else _UNITS_M)[rest % 10])
    return [w for w in words if w]


def number_in_words(n: int) -> str:
    """Целое число прописью (мужской род): 1234 -> «одна тысяча двести тридцать четыре»."""
    if n == 0:
        return "ноль"
    words: list[str] = []
    triads = []
    while n:
        triads.append(n % 1000)
        n //= 1000
    for i in range(len(triads) - 1, -1, -1):
        t = triads[i]
        if not t:
            continue
        if i == 0:
            words += _triad(t, False)
        else:
            forms, female = _ORDERS[i - 1]
            words += _triad(t, female) + [plural(t, forms)]
    return " ".join(words)


def amount_in_words(value: Any) -> str:
    """Сумма в рублях прописью: 1234.5 -> «Одна тысяча двести тридцать четыре рубля 50 копеек»."""
    num = parse_number(value)
    if num is None:
        raise ValueError(f"не число: {value!r}")
    q = Decimal(str(abs(num))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    rub, kop = int(q), int((q - int(q)) * 100)
    text = f"{number_in_words(rub)} {plural(rub, ('рубль', 'рубля', 'рублей'))} {kop:02d} {plural(kop, ('копейка', 'копейки', 'копеек'))}"
    if num < 0:
        text = "минус " + text
    return text[0].upper() + text[1:]
