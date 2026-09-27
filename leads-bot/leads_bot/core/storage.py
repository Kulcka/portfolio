"""Хранение в SQLite: заявки, незаконченные диалоги, служебные значения.

Запросы синхронные: каждая операция занимает доли миллисекунды, а бот приёма
заявок не бывает высоконагруженным. Важное следствие для сценария: между
чтением диалога и его сохранением нет `await`, поэтому два одновременных
нажатия одной кнопки не создадут две заявки.

Время хранится в UTC в ISO 8601 (`2026-09-27T12:00:00+00:00`), поэтому строки
можно сравнивать напрямую.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from .models import Channel, Dialog, Lead, LeadDraft, Step

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    channel         TEXT    NOT NULL,
    user_id         INTEGER NOT NULL,
    username        TEXT,
    display_name    TEXT,
    service_id      TEXT    NOT NULL,
    service_title   TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    phone           TEXT    NOT NULL,
    comment         TEXT    NOT NULL DEFAULT '',
    consent_at      TEXT    NOT NULL,
    policy_url      TEXT    NOT NULL,
    policy_version  TEXT    NOT NULL DEFAULT '',
    admin_notified  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_leads_user_time ON leads (channel, user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads (created_at);

CREATE TABLE IF NOT EXISTS dialogs (
    channel     TEXT    NOT NULL,
    user_id     INTEGER NOT NULL,
    step        TEXT    NOT NULL,
    data        TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (channel, user_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def to_db_time(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("ожидается время с часовым поясом")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_db_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"База {self.path} создана более новой версией бота (схема {version}), обновите бота"
            )
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # --- диалоги ---------------------------------------------------------------------------

    def get_dialog(self, channel: Channel, user_id: int) -> Dialog | None:
        row = self._conn.execute(
            "SELECT step, data, updated_at FROM dialogs WHERE channel = ? AND user_id = ?",
            (channel.value, user_id),
        ).fetchone()
        if row is None:
            return None
        return Dialog(
            channel=channel,
            user_id=user_id,
            step=Step(row["step"]),
            updated_at=from_db_time(row["updated_at"]),
            data=json.loads(row["data"]),
        )

    def save_dialog(self, dialog: Dialog) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO dialogs (channel, user_id, step, data, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (channel, user_id) DO UPDATE
                    SET step = excluded.step, data = excluded.data, updated_at = excluded.updated_at
                """,
                (
                    dialog.channel.value,
                    dialog.user_id,
                    dialog.step.value,
                    json.dumps(dialog.data, ensure_ascii=False),
                    to_db_time(dialog.updated_at),
                ),
            )

    def delete_dialog(self, channel: Channel, user_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM dialogs WHERE channel = ? AND user_id = ?", (channel.value, user_id))

    def purge_dialogs(self, older_than: datetime) -> int:
        """Удалить брошенные на полпути диалоги (в них могут быть имя и телефон)."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM dialogs WHERE updated_at < ?", (to_db_time(older_than),))
        return cursor.rowcount

    def count_dialogs(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0])

    # --- заявки ----------------------------------------------------------------------------

    def add_lead(self, draft: LeadDraft) -> Lead:
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO leads (created_at, channel, user_id, username, display_name, service_id,
                                   service_title, name, phone, comment, consent_at, policy_url, policy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_db_time(draft.created_at),
                    draft.channel.value,
                    draft.user_id,
                    draft.username,
                    draft.display_name,
                    draft.service_id,
                    draft.service_title,
                    draft.name,
                    draft.phone,
                    draft.comment,
                    to_db_time(draft.consent_at),
                    draft.policy_url,
                    draft.policy_version,
                ),
            )
        lead_id = cursor.lastrowid
        assert lead_id is not None
        return self.get_lead(lead_id)  # type: ignore[return-value]

    def get_lead(self, lead_id: int) -> Lead | None:
        row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_lead(row) if row else None

    def mark_notified(self, lead_id: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE leads SET admin_notified = 1 WHERE id = ?", (lead_id,))

    def list_leads(self, since: datetime | None = None) -> list[Lead]:
        if since is None:
            rows = self._conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM leads WHERE created_at >= ? ORDER BY id", (to_db_time(since),)
            ).fetchall()
        return [_row_to_lead(row) for row in rows]

    def user_lead_times_after(self, channel: Channel, user_id: int, after: datetime) -> list[datetime]:
        """Время заявок пользователя строго позже `after` (для антиспама), по возрастанию."""
        rows = self._conn.execute(
            "SELECT created_at FROM leads WHERE channel = ? AND user_id = ? AND created_at > ? ORDER BY created_at",
            (channel.value, user_id, to_db_time(after)),
        ).fetchall()
        return [from_db_time(row["created_at"]) for row in rows]

    def count_leads(self, since: datetime | None = None) -> int:
        if since is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
        return int(
            self._conn.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (to_db_time(since),)).fetchone()[0]
        )

    def count_by_service(self, since: datetime | None = None) -> list[tuple[str, int]]:
        query = "SELECT service_title, COUNT(*) AS n FROM leads {where} GROUP BY service_title ORDER BY n DESC, service_title"
        if since is None:
            rows = self._conn.execute(query.format(where="")).fetchall()
        else:
            rows = self._conn.execute(query.format(where="WHERE created_at >= ?"), (to_db_time(since),)).fetchall()
        return [(row["service_title"], int(row["n"])) for row in rows]

    def count_by_channel(self, since: datetime | None = None) -> dict[Channel, int]:
        query = "SELECT channel, COUNT(*) AS n FROM leads {where} GROUP BY channel"
        if since is None:
            rows = self._conn.execute(query.format(where="")).fetchall()
        else:
            rows = self._conn.execute(query.format(where="WHERE created_at >= ?"), (to_db_time(since),)).fetchall()
        return {Channel(row["channel"]): int(row["n"]) for row in rows}

    def delete_user_data(self, channel: Channel, user_id: int) -> int:
        """Отзыв согласия: удалить заявки и незаконченный диалог пользователя. Вернёт число заявок."""
        with self._conn:
            cursor = self._conn.execute("DELETE FROM leads WHERE channel = ? AND user_id = ?", (channel.value, user_id))
            self._conn.execute("DELETE FROM dialogs WHERE channel = ? AND user_id = ?", (channel.value, user_id))
        return cursor.rowcount

    def purge_leads(self, older_than: datetime) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM leads WHERE created_at < ?", (to_db_time(older_than),))
        return cursor.rowcount

    # --- служебные значения ----------------------------------------------------------------

    def get_value(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def _row_to_lead(row: sqlite3.Row) -> Lead:
    return Lead(
        id=int(row["id"]),
        created_at=from_db_time(row["created_at"]),
        channel=Channel(row["channel"]),
        user_id=int(row["user_id"]),
        username=row["username"],
        display_name=row["display_name"],
        service_id=row["service_id"],
        service_title=row["service_title"],
        name=row["name"],
        phone=row["phone"],
        comment=row["comment"],
        consent_at=from_db_time(row["consent_at"]),
        policy_url=row["policy_url"],
        policy_version=row["policy_version"],
        admin_notified=bool(row["admin_notified"]),
    )
