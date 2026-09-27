"""Хранилище прогонов в SQLite: с чем сравнивать следующий прогон.

Таблицы:

* ``runs``  — прогоны: конфиг, время, полный/неполный, число записей;
* ``items`` — записи прогона (JSON) по ключу.

Для сравнения берётся последний **полный** прогон (без ошибок загрузки),
а если таких нет — просто последний. Старые прогоны удаляются, хранится
``storage.keep_runs`` последних на конфиг.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    config_name TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT    NOT NULL,
    complete    INTEGER NOT NULL,
    items_count INTEGER NOT NULL,
    pages       INTEGER NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_config ON runs (config_name, id);
CREATE TABLE IF NOT EXISTS items (
    run_id   INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    item_key TEXT    NOT NULL,
    data     TEXT    NOT NULL,
    PRIMARY KEY (run_id, item_key)
);
"""


@dataclass(frozen=True)
class RunInfo:
    id: int
    config_name: str
    started_at: datetime
    finished_at: datetime
    complete: bool
    items_count: int
    pages: int
    note: str | None = None


class SnapshotStore:
    """Доступ к SQLite. Соединение открывается на каждую операцию — файл не
    остаётся заблокированным между прогонами (важно на Windows)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(_SCHEMA)
            with conn:  # транзакция: commit или rollback
                yield conn

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunInfo:
        return RunInfo(
            id=int(row["id"]),
            config_name=row["config_name"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            complete=bool(row["complete"]),
            items_count=int(row["items_count"]),
            pages=int(row["pages"]),
            note=row["note"],
        )

    def save_run(
        self,
        config_name: str,
        items: Mapping[str, Mapping[str, Any]],
        *,
        started_at: datetime,
        finished_at: datetime,
        complete: bool,
        pages: int,
        note: str | None = None,
    ) -> RunInfo:
        """Сохранить прогон целиком (одной транзакцией)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (config_name, started_at, finished_at, complete, items_count, pages, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    config_name,
                    started_at.isoformat(timespec="seconds"),
                    finished_at.isoformat(timespec="seconds"),
                    int(complete),
                    len(items),
                    pages,
                    note,
                ),
            )
            run_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO items (run_id, item_key, data) VALUES (?, ?, ?)",
                ((run_id, key, json.dumps(dict(item), ensure_ascii=False, default=str)) for key, item in items.items()),
            )
        return RunInfo(
            id=run_id,
            config_name=config_name,
            started_at=started_at,
            finished_at=finished_at,
            complete=complete,
            items_count=len(items),
            pages=pages,
            note=note,
        )

    def latest_run(self, config_name: str, *, prefer_complete: bool = True) -> RunInfo | None:
        """Последний прогон конфига (по умолчанию — последний полный)."""
        with self._connect() as conn:
            row = None
            if prefer_complete:
                row = conn.execute(
                    "SELECT * FROM runs WHERE config_name = ? AND complete = 1 ORDER BY id DESC LIMIT 1",
                    (config_name,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM runs WHERE config_name = ? ORDER BY id DESC LIMIT 1",
                    (config_name,),
                ).fetchone()
        return self._row_to_run(row) if row is not None else None

    def load_items(self, run_id: int) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT item_key, data FROM items WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return {row["item_key"]: json.loads(row["data"]) for row in rows}

    def list_runs(self, config_name: str, limit: int = 20) -> list[RunInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE config_name = ? ORDER BY id DESC LIMIT ?",
                (config_name, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def prune(self, config_name: str, keep: int) -> int:
        """Удалить старые прогоны, оставив ``keep`` последних. Вернуть число удалённых."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM runs WHERE config_name = ? AND id NOT IN ("
                " SELECT id FROM runs WHERE config_name = ? ORDER BY id DESC LIMIT ?)",
                (config_name, config_name, keep),
            )
            return int(cursor.rowcount or 0)
