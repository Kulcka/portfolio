"""Сравнение двух прогонов: новые записи, изменение цены, пропавшие, прочие изменения.

Записи сопоставляются по ключу (``changes.key`` в конфиге: артикул, UPC,
ссылка или несколько полей вместе).

Защита от ложных «пропали»: если текущий прогон неполный (были ошибки
загрузки) или записей стало меньше, чем ``gone_guard_ratio`` от прошлого
раза (по умолчанию половина), раздел «пропали» не заполняется — вероятнее
сбой сайта или сети, чем массовое снятие товаров с продажи.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from site_parser.config import URL_COLUMN, ChangesSpec
from site_parser.storage import RunInfo

Item = Mapping[str, Any]


def _key_part(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return ",".join(_key_part(v) for v in value)
    return str(value).strip()


def item_key(item: Item, key_fields: Iterable[str]) -> str:
    """Ключ записи для сопоставления прогонов. Нет ключевых полей — берём ссылку."""
    parts: list[str] = []
    for name in key_fields:
        value = item.get(name)
        if value is None or value == "" or value == []:
            return str(item.get(URL_COLUMN) or "")
        parts.append(_key_part(value))
    return " | ".join(parts)


def values_equal(old: Any, new: Any) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return type(old) is type(new) and old == new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return math.isclose(float(old), float(new), rel_tol=0.0, abs_tol=0.005)
    return bool(old == new)


@dataclass(frozen=True)
class PriceChange:
    key: str
    title: str
    url: str
    old: float | None
    new: float | None

    @property
    def delta(self) -> float | None:
        if self.old is None or self.new is None:
            return None
        return round(self.new - self.old, 2)

    @property
    def delta_percent(self) -> float | None:
        if self.old in (None, 0) or self.new is None:
            return None
        assert self.old is not None
        return round((self.new - self.old) / self.old * 100, 1)


@dataclass(frozen=True)
class FieldChange:
    key: str
    title: str
    url: str
    field: str
    old: Any
    new: Any


@dataclass
class DiffResult:
    previous_run: RunInfo | None
    current_count: int
    previous_count: int = 0
    new: list[dict[str, Any]] = field(default_factory=list)
    gone: list[dict[str, Any]] = field(default_factory=list)
    price_changes: list[PriceChange] = field(default_factory=list)
    field_changes: list[FieldChange] = field(default_factory=list)
    gone_suppressed_reason: str | None = None

    @property
    def is_first_run(self) -> bool:
        return self.previous_run is None

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.gone or self.price_changes or self.field_changes)

    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "price": len(self.price_changes),
            "gone": len(self.gone),
            "other": len(self.field_changes),
        }


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compute_diff(
    previous: Mapping[str, Item] | None,
    current: Mapping[str, Item],
    spec: ChangesSpec,
    *,
    title_field: str | None,
    price_field: str | None,
    current_complete: bool = True,
    previous_run: RunInfo | None = None,
) -> DiffResult:
    """Сравнить текущие записи с прошлым прогоном (оба — словари ``ключ → запись``)."""
    result = DiffResult(previous_run=previous_run, current_count=len(current))
    if previous is None:
        return result
    result.previous_count = len(previous)

    def title_of(item: Item, key: str) -> str:
        value = item.get(title_field) if title_field else None
        return str(value) if value not in (None, "") else key

    for key, item in current.items():
        old_item = previous.get(key)
        url = str(item.get(URL_COLUMN) or "")
        if old_item is None:
            result.new.append(dict(item))
            continue
        title = title_of(item, key)
        if price_field:
            old_price, new_price = old_item.get(price_field), item.get(price_field)
            if not values_equal(old_price, new_price):
                result.price_changes.append(PriceChange(key, title, url, _as_number(old_price), _as_number(new_price)))
        for name in spec.track:
            if name == price_field:
                continue
            old_value, new_value = old_item.get(name), item.get(name)
            if not values_equal(old_value, new_value):
                result.field_changes.append(FieldChange(key, title, url, name, old_value, new_value))

    gone = [dict(item) for key, item in previous.items() if key not in current]
    if gone:
        if not current_complete:
            result.gone_suppressed_reason = (
                "прогон прошёл с ошибками загрузки — пропавшие не считаем, чтобы не было ложных тревог"
            )
        elif len(previous) and len(current) < len(previous) * spec.gone_guard_ratio:
            result.gone_suppressed_reason = (
                f"записей стало намного меньше ({len(current)} против {len(previous)}) — "
                "похоже на сбой сайта, пропавшие не считаем"
            )
        else:
            result.gone = gone

    result.price_changes.sort(
        key=lambda c: abs(c.delta_percent) if c.delta_percent is not None else float("inf"),
        reverse=True,
    )
    return result
