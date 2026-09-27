"""Приведение сырых строк со страницы к типам: число, цена, валюта, наличие, ссылка.

Каждый конвертер принимает значение (обычно строку) и возвращает результат
нужного типа или ``None``, если строку привести не удалось. Исключений
конвертеры не бросают: одна «кривая» карточка не должна ронять весь прогон.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

# Число с разделителями разрядов (пробел, неразрывный пробел, апостроф, точка,
# запятая) и необязательным знаком: «1 299,00», «$1,299.99», «−15».
_NUMBER_RE = re.compile(r"[-+−]?\d[\d\s  '’.,]*")
_GROUP_SEPARATORS_RE = re.compile(r"[\s  '’]")


def normalize_space(text: str) -> str:
    """Схлопнуть пробелы, переводы строк и неразрывные пробелы в один пробел."""
    return " ".join(text.split())


def parse_number(value: Any) -> float | None:
    """Найти в строке первое число и вернуть его как ``float``.

    Понимает русский и западный формат:

    * ``"1 299,00 ₽"`` → 1299.0, ``"$1,299.99"`` → 1299.99;
    * ``"1.299,99 €"`` → 1299.99, ``"12,5"`` → 12.5;
    * одна запятая перед ровно тремя цифрами — разделитель тысяч
      (``"12,345"`` → 12345), кроме ``"0,345"`` → 0.345;
    * одиночная точка всегда десятичная (``"1.299"`` → 1.299).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    match = _NUMBER_RE.search(str(value))
    if not match:
        return None
    token = match.group(0)
    negative = token[0] in "-−"
    token = token.lstrip("+-−")
    token = _GROUP_SEPARATORS_RE.sub("", token).rstrip(".,")
    if not token:
        return None

    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        # Десятичный разделитель — тот, что встречается последним.
        decimal = "." if token.rfind(".") > token.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        token = token.replace(thousands, "").replace(decimal, ".")
    elif has_comma:
        parts = token.split(",")
        is_thousands = len(parts) > 2 or (len(parts[1]) == 3 and parts[0].strip("0") != "")
        token = token.replace(",", "") if is_thousands else token.replace(",", ".")
    elif has_dot and token.count(".") > 1:
        # «1.299.000» — точки как разделители тысяч.
        token = token.replace(".", "")

    try:
        number = float(token)
    except ValueError:
        return None
    return -number if negative else number


def to_float(value: Any, base_url: str = "") -> float | None:
    return parse_number(value)


def to_int(value: Any, base_url: str = "") -> int | None:
    number = parse_number(value)
    return None if number is None else round(number)


def to_price(value: Any, base_url: str = "") -> float | None:
    number = parse_number(value)
    return None if number is None else round(number, 2)


def to_str(value: Any, base_url: str = "") -> str | None:
    if value is None:
        return None
    text = normalize_space(str(value))
    return text or None


# Порядок важен: сначала коды ISO, затем символы, затем слова.
_CURRENCY_CODES = (
    "RUB",
    "RUR",
    "USD",
    "EUR",
    "GBP",
    "CNY",
    "JPY",
    "KZT",
    "UAH",
    "BYN",
    "TRY",
    "AED",
    "UZS",
)
_CURRENCY_SYMBOLS = (
    ("₽", "RUB"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
    ("¥", "CNY"),
    ("₸", "KZT"),
    ("₴", "UAH"),
    ("₺", "TRY"),
)
_CURRENCY_WORDS = (
    (re.compile(r"\bруб", re.IGNORECASE), "RUB"),
    (re.compile(r"(?<![а-яё])р\.", re.IGNORECASE), "RUB"),
    (re.compile(r"\bдолл", re.IGNORECASE), "USD"),
    (re.compile(r"\bевро\b", re.IGNORECASE), "EUR"),
    (re.compile(r"\bтенге\b", re.IGNORECASE), "KZT"),
    (re.compile(r"\bгрн", re.IGNORECASE), "UAH"),
    (re.compile(r"\bBr\b"), "BYN"),
)


def to_currency(value: Any, base_url: str = "") -> str | None:
    """Определить валюту по строке с ценой: ``"£51.77"`` → ``"GBP"``."""
    if value is None:
        return None
    text = str(value)
    upper = text.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return "RUB" if code == "RUR" else code
    for symbol, code in _CURRENCY_SYMBOLS:
        if symbol in text:
            return code
    for pattern, code in _CURRENCY_WORDS:
        if pattern.search(text):
            return code
    return None


# Фразы «нет в наличии» проверяются раньше «в наличии»: вторая — подстрока первой.
_OUT_OF_STOCK = (
    "нет в наличии",
    "нет на складе",
    "отсутствует",
    "под заказ",
    "ожидается",
    "закончил",
    "распродан",
    "снят с продажи",
    "нет в продаже",
    "out of stock",
    "sold out",
    "unavailable",
    "not available",
    "discontinued",
    "pre-order",
    "preorder",
    "backorder",
)
_IN_STOCK = (
    "в наличии",
    "есть на складе",
    "есть в наличии",
    "в продаже",
    "много",
    "мало",
    "есть",
    "in stock",
    "available",
    "в магазине",
)


def to_availability(value: Any, base_url: str = "") -> bool | None:
    """Наличие товара: ``"In stock (22 available)"`` → True, ``"Нет в наличии"`` → False.

    Если фраз не нашлось, но есть число (``"5 шт."``), наличие — это число > 0.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = normalize_space(str(value)).casefold()
    if not text:
        return None
    if any(phrase in text for phrase in _OUT_OF_STOCK):
        return False
    if any(phrase in text for phrase in _IN_STOCK):
        return True
    number = parse_number(text)
    return None if number is None else number > 0


_TRUE_WORDS = {"да", "yes", "true", "1", "+", "есть", "y", "on"}
_FALSE_WORDS = {"нет", "no", "false", "0", "-", "—", "n", "off"}


def to_bool(value: Any, base_url: str = "") -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = normalize_space(str(value)).casefold()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def to_url(value: Any, base_url: str = "") -> str | None:
    """Сделать ссылку абсолютной относительно адреса страницы."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    return urljoin(base_url, text) if base_url else text


Converter = Callable[[Any, str], Any]

CONVERTERS: dict[str, Converter] = {
    "str": to_str,
    "int": to_int,
    "float": to_float,
    "price": to_price,
    "currency": to_currency,
    "availability": to_availability,
    "bool": to_bool,
    "url": to_url,
}


def convert(value: Any, type_name: str, base_url: str = "") -> Any:
    """Привести ``value`` к типу ``type_name`` (ключ ``CONVERTERS``)."""
    try:
        converter = CONVERTERS[type_name]
    except KeyError:
        raise ValueError(f"неизвестный тип поля: {type_name!r}") from None
    return converter(value, base_url)
