"""ДЕМО: «состарить» последний прогон, чтобы показать отчёт об изменениях.

Учебный сайт books.toscrape.com статичен — цены на нём не меняются, и
повторный прогон честно покажет «Изменений нет». Чтобы продемонстрировать
отчёт, скрипт копирует последний прогон в базе в новый прогон с пометкой
«ДЕМО» и вносит в копию правки:

* у трёх товаров меняет цену (в следующем прогоне — «Изменилась цена»);
* два товара убирает (в следующем прогоне — «Новые»);
* добавляет один товар, которого нет в текущей выборке («Пропали»);
* у одного товара меняет остаток, у другого — наличие («Другие изменения»).

Сайт при этом не запрашивается. Запуск:

    python scripts/simulate_changes.py configs/books_toscrape.yaml
    python -m site_parser run configs/books_toscrape.yaml --cache ...
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from site_parser.config import load_config
from site_parser.logging_setup import utf8_stdio
from site_parser.storage import SnapshotStore

NOTE = "ДЕМО: копия прошлого прогона с искусственными изменениями (scripts/simulate_changes.py)"


def main() -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", type=Path)
    args = parser.parse_args()

    config = load_config(args.config)
    store = SnapshotStore(config.storage.path)
    run = store.latest_run(config.name)
    if run is None:
        print("В базе нет прогонов — сначала выполните обычный прогон.")
        return 1
    items = store.load_items(run.id)
    keys = list(items)
    if len(keys) < 12:
        print("Для демо нужно хотя бы 12 записей в прошлом прогоне.")
        return 1
    price = config.price_field() or "price"

    for index, factor in ((1, 1.10), (5, 0.85), (9, 1.25)):
        item = items[keys[index]]
        if isinstance(item.get(price), (int, float)):
            item[price] = round(float(item[price]) * factor, 2)
    items[keys[3]]["stock_qty"] = int(items[keys[3]].get("stock_qty") or 0) + 7
    items[keys[7]]["in_stock"] = False
    for key in (keys[-1], keys[-2]):
        del items[key]
    items["demo-gone-0001"] = {
        "title": "1,000 Places to See Before You Die",
        price: 26.08,
        "currency": "GBP",
        "in_stock": True,
        "upc": "demo-gone-0001",
        "url": "https://books.toscrape.com/catalogue/1000-places-to-see-before-you-die_1/index.html",
    }

    now = datetime.now().astimezone()
    new_run = store.save_run(config.name, items, started_at=now, finished_at=now, complete=True, pages=0, note=NOTE)
    print(f"Создан демо-прогон №{new_run.id} на основе №{run.id}: {len(items)} записей.")
    print("Теперь повторите прогон — отчёт покажет изменения (с --cache сайт не запрашивается).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
