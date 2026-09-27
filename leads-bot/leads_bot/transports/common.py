"""Общее для транспортов: права администратора, разбор команд, служебные тексты."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdminAccess:
    """Кому доступны /stats и /export: всем в админ-чате и перечисленным пользователям."""

    chat_id: int | None = None
    user_ids: frozenset[int] = frozenset()

    def is_admin(self, chat_id: int | None, user_id: int | None) -> bool:
        in_admin_chat = self.chat_id is not None and chat_id == self.chat_id
        return in_admin_chat or (user_id is not None and user_id in self.user_ids)


def parse_command(text: str) -> str | None:
    """`/start`, `/Start@my_bot payload` → `start`; обычный текст → None."""
    if not text.startswith("/") or len(text) < 2:
        return None
    word = text[1:].split(maxsplit=1)[0]
    return word.split("@", 1)[0].lower() or None


# Служебные тексты для администратора — клиент их не видит, поэтому не в config.yaml.
ADMIN_NO_LEADS = "Заявок пока нет."
ADMIN_EXPORT_CAPTION = "Выгрузка заявок: {count} шт."
ADMIN_EXPORT_FAILED = (
    "Не удалось отправить файл выгрузки. Выгрузить заявки можно на сервере командой "
    "<code>python -m leads_bot export</code>."
)
CHAT_ID_REPLY = "chat_id: <code>{chat_id}</code>\nuser_id: <code>{user_id}</code>"
