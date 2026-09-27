"""Извлечение полей со страницы по описанию из конфига.

Порядок обработки значения одного поля::

    CSS-селектор → текст или атрибут → регулярка → словарь map → приведение типа

Если поле не нашлось или не привелось к типу, берётся ``default``. Функции
модуля чистые (без сети), поэтому легко проверяются на сохранённых HTML.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from bs4 import Tag

from site_parser.config import FieldSpec, SpecsSpec
from site_parser.converters import convert, normalize_space

# Атрибуты-ссылки: относительный адрес сразу превращаем в абсолютный.
URL_ATTRS = frozenset({"href", "src", "data-src", "data-original", "data-lazy", "data-href"})


def _raw_value(element: Tag, attr: str) -> str | None:
    """Сырое значение элемента: текст, HTML или атрибут."""
    if attr == "text":
        return element.get_text(" ", strip=True)
    if attr == "html":
        return element.decode_contents().strip()
    if attr == "own_text":
        # Только собственный текст элемента, без вложенных тегов.
        return " ".join(s.strip() for s in element.find_all(string=True, recursive=False) if s.strip())
    value = element.get(attr)
    if value is None:
        return None
    if isinstance(value, list):  # class и другие многозначные атрибуты
        return " ".join(value)
    return str(value)


def _map_value(value: str, mapping: Mapping[str, Any]) -> Any:
    if value in mapping:
        return mapping[value]
    folded = value.casefold()
    for key, mapped in mapping.items():
        if key.casefold() == folded:
            return mapped
    return value


def process_value(raw: str | None, spec: FieldSpec, base_url: str) -> Any:
    """Прогнать одно сырое значение через регулярку, map и приведение типа."""
    if raw is None:
        return None
    value: Any = raw if spec.attr == "html" else normalize_space(raw)
    if spec.regex:
        match = re.search(spec.regex, value)
        if not match:
            return None
        value = match.group(1) if match.re.groups else match.group(0)
        value = value.strip()
    if spec.map is not None:
        value = _map_value(value, spec.map)
    type_name = spec.type
    if type_name == "str" and spec.attr in URL_ATTRS:
        type_name = "url"
    if spec.attr == "html" and type_name == "str":
        return value or None
    return convert(value, type_name, base_url)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def extract_field(root: Tag, spec: FieldSpec, base_url: str) -> Any:
    """Значение поля внутри ``root`` (карточки в списке или всей страницы)."""
    if spec.css is None:
        elements: list[Tag] = [root]
    elif spec.multiple:
        elements = list(root.select(spec.css))
    else:
        found = root.select_one(spec.css)
        elements = [found] if found is not None else []

    if spec.multiple:
        values = [process_value(_raw_value(el, spec.attr), spec, base_url) for el in elements]
        result: Any = [v for v in values if not _is_empty(v)]
    else:
        result = process_value(_raw_value(elements[0], spec.attr), spec, base_url) if elements else None

    if _is_empty(result):
        return spec.default
    return result


def extract_fields(root: Tag, fields: Iterable[FieldSpec], base_url: str) -> dict[str, Any]:
    return {spec.name: extract_field(root, spec, base_url) for spec in fields}


def missing_required(item: Mapping[str, Any], fields: Iterable[FieldSpec]) -> list[str]:
    """Обязательные поля, которые остались пустыми."""
    return [spec.name for spec in fields if spec.required and _is_empty(item.get(spec.name))]


def extract_specs(root: Tag, spec: SpecsSpec) -> dict[str, str]:
    """Таблица характеристик → ``{префикс + название: значение}``.

    Два режима: строки таблицы (``rows`` + ``key``/``value`` внутри строки)
    или параллельные списки (``keys`` и ``values``, например ``dt``/``dd``).
    """
    pairs: list[tuple[str, str]] = []
    if spec.rows:
        for row in root.select(spec.rows):
            key_el, value_el = row.select_one(spec.key), row.select_one(spec.value)
            if key_el is None or value_el is None:
                continue
            pairs.append((key_el.get_text(" ", strip=True), value_el.get_text(" ", strip=True)))
    elif spec.keys and spec.values:
        keys = [el.get_text(" ", strip=True) for el in root.select(spec.keys)]
        values = [el.get_text(" ", strip=True) for el in root.select(spec.values)]
        pairs.extend(zip(keys, values, strict=False))  # лишние ключи без значений отбрасываются

    include = set(spec.include)
    exclude = set(spec.exclude)
    result: dict[str, str] = {}
    for key, value in pairs:
        key = normalize_space(key).rstrip(":").strip()
        if not key or (include and key not in include) or key in exclude:
            continue
        result[f"{spec.prefix}{key}"] = normalize_space(value)
    return result


def select_all(root: Tag, selector: str) -> list[Tag]:
    return list(root.select(selector))


def select_link(root: Tag, selector: str, base_url: str) -> str | None:
    """Абсолютная ссылка из первого элемента по селектору (``href`` или ``src``)."""
    element = root.select_one(selector)
    if element is None:
        return None
    raw = element.get("href") or element.get("src")
    if raw is None:
        return None
    url: str | None = convert(raw if isinstance(raw, str) else " ".join(raw), "url", base_url)
    return url
