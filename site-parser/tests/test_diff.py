"""Сравнение прогонов и хранилище SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from site_parser.config import ChangesSpec
from site_parser.diff import compute_diff, item_key, values_equal
from site_parser.storage import RunInfo, SnapshotStore

SPEC = ChangesSpec(key=("sku",), track=("in_stock",))


def _item(sku: str, price: float | None, *, title: str | None = None, in_stock: bool = True) -> dict[str, object]:
    return {
        "sku": sku,
        "title": title or f"Товар {sku}",
        "price": price,
        "in_stock": in_stock,
        "url": f"https://s.test/{sku}",
    }


def _index(*items: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(i["sku"]): i for i in items}


def _diff(previous: dict | None, current: dict, **kwargs: object):  # type: ignore[no-untyped-def]
    return compute_diff(previous, current, SPEC, title_field="title", price_field="price", **kwargs)  # type: ignore[arg-type]


def test_first_run_has_nothing_to_compare() -> None:
    diff = _diff(None, _index(_item("A", 10)))
    assert diff.is_first_run
    assert not diff.has_changes
    assert diff.current_count == 1


def test_new_gone_price_and_tracked_changes() -> None:
    previous = _index(_item("A", 100.0), _item("B", 50.0), _item("C", 10.0), _item("D", 5.0))
    current = _index(
        _item("A", 90.0),  # цена −10%
        _item("B", 50.0, in_stock=False),  # закончился
        _item("C", 10.001),  # копейки после округления — не изменение
        _item("E", 7.0),  # новый
    )  # D пропал
    diff = _diff(previous, current)

    assert [i["sku"] for i in diff.new] == ["E"]
    assert [i["sku"] for i in diff.gone] == ["D"]
    assert len(diff.price_changes) == 1
    change = diff.price_changes[0]
    assert (change.key, change.old, change.new, change.delta, change.delta_percent) == ("A", 100.0, 90.0, -10.0, -10.0)
    assert change.title == "Товар A"
    assert [(c.key, c.field, c.old, c.new) for c in diff.field_changes] == [("B", "in_stock", True, False)]
    assert diff.counts() == {"new": 1, "price": 1, "gone": 1, "other": 1}


def test_price_changes_sorted_by_size() -> None:
    previous = _index(_item("A", 100.0), _item("B", 100.0), _item("C", 100.0))
    current = _index(_item("A", 101.0), _item("B", 150.0), _item("C", 80.0))
    diff = _diff(previous, current)
    assert [c.key for c in diff.price_changes] == ["B", "C", "A"]


def test_price_appeared_or_disappeared() -> None:
    diff = _diff(_index(_item("A", None)), _index(_item("A", 12.5)))
    assert diff.price_changes[0].old is None
    assert diff.price_changes[0].delta_percent is None


def test_gone_suppressed_for_incomplete_run() -> None:
    previous = _index(_item("A", 1), _item("B", 2))
    diff = _diff(previous, _index(_item("A", 1)), current_complete=False)
    assert diff.gone == []
    assert diff.gone_suppressed_reason and "ошибками" in diff.gone_suppressed_reason


def test_gone_suppressed_when_most_items_vanished() -> None:
    previous = _index(*[_item(str(n), 1.0) for n in range(10)])
    diff = _diff(previous, _index(_item("0", 1.0), _item("1", 1.0)))
    assert diff.gone == []
    assert diff.gone_suppressed_reason and "меньше" in diff.gone_suppressed_reason


def test_item_key_variants() -> None:
    assert item_key({"sku": "A-1"}, ["sku"]) == "A-1"
    assert item_key({"author": "Эйнштейн", "text": "Цитата"}, ["author", "text"]) == "Эйнштейн | Цитата"
    assert item_key({"sku": None, "url": "https://s.test/1"}, ["sku"]) == "https://s.test/1"
    assert item_key({"id": 5.0}, ["id"]) == "5"


def test_values_equal() -> None:
    assert values_equal(10, 10.0)
    assert values_equal(9.999, 10.0)
    assert not values_equal(9.99, 10.0)
    assert not values_equal(True, 1)
    assert values_equal(["a", "b"], ["a", "b"])
    assert not values_equal(None, 0)


def test_store_roundtrip_previous_run_and_prune(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "state" / "db.sqlite3")
    t0 = datetime(2026, 9, 27, 10, 0).astimezone()
    assert store.latest_run("shop") is None

    first = store.save_run(
        "shop", _index(_item("A", 1.5)), started_at=t0, finished_at=t0 + timedelta(minutes=1), complete=True, pages=3
    )
    partial = store.save_run(
        "shop",
        _index(_item("B", 2)),
        started_at=t0,
        finished_at=t0 + timedelta(minutes=2),
        complete=False,
        pages=1,
        note="сбой сети",
    )
    store.save_run("other", _index(_item("Z", 9)), started_at=t0, finished_at=t0, complete=True, pages=1)

    latest = store.latest_run("shop")
    assert isinstance(latest, RunInfo)
    assert latest.id == first.id  # неполный прогон не берётся за базу сравнения
    assert store.latest_run("shop", prefer_complete=False).id == partial.id  # type: ignore[union-attr]
    assert store.load_items(first.id) == {"A": _item("A", 1.5)}

    for n in range(5):
        store.save_run("shop", {}, started_at=t0, finished_at=t0, complete=True, pages=0, note=str(n))
    assert store.prune("shop", keep=3) == 4
    runs = store.list_runs("shop")
    assert [r.note for r in runs] == ["4", "3", "2"]
    assert store.load_items(first.id) == {}  # записи удалены каскадом
    assert len(store.list_runs("other")) == 1
