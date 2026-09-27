"""Демо-режим в терминале: тот же сценарий без мессенджера и без токенов.

Кнопки показываются пронумерованными — чтобы «нажать», введите номер.
`/contact +79991234567` имитирует кнопку «Отправить мой номер».
Доступны /start, /cancel, /forget, /stats, /export, /quit.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from .config import BotConfig
from .core.models import ButtonKind, Channel, Reply, UserRef
from .core.service import LeadService
from .core.storage import Storage

_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)


def html_to_console(text: str) -> str:
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    return html.unescape(_TAG.sub("", text))


class ConsoleNotifier:
    name = "console"

    async def notify_admin(self, text: str) -> None:
        print("\n  ┌─ уведомление в админ-чат ─────────────")
        for line in html_to_console(text).splitlines():
            print(f"  │ {line}")
        print("  └───────────────────────────────────────\n")


def _render(replies: list[Reply]) -> list[str]:
    """Напечатать ответы; вернуть payload'ы кнопок по порядку номеров."""
    payloads: list[str] = []
    for reply in replies:
        print(f"\nБот: {html_to_console(reply.text)}")
        for button in reply.buttons:
            if button.kind is ButtonKind.URL:
                print(f"     [ссылка] {button.text}: {button.value}")
            elif button.kind is ButtonKind.REQUEST_CONTACT:
                print(f"     [кнопка] {button.text} — введите /contact +7XXXXXXXXXX")
            else:
                payloads.append(button.value)
                print(f"     [{len(payloads)}] {button.text}")
    return payloads


async def run_console(config: BotConfig, db_path: Path) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    with Storage(db_path) as storage:
        service = LeadService(config, storage, notifiers=[ConsoleNotifier()])
        user = UserRef(channel=Channel.CONSOLE, user_id=1, username="demo_user", display_name="Демо Пользователь")
        print(f"Демо-режим бота «{config.company.name}». База: {db_path}. Выход — /quit.")
        buttons = _render(await service.start(user))
        while True:
            try:
                line = input("\nВы: ").strip()
            except EOFError:
                break
            if line in ("/quit", "/exit"):
                break
            if line.isdigit() and 1 <= int(line) <= len(buttons):
                replies = await service.handle_button(user, buttons[int(line) - 1])
            elif line.startswith("/contact"):
                replies = await service.handle_contact(user, line.removeprefix("/contact").strip())
            elif line == "/start":
                replies = await service.start(user)
            elif line == "/cancel":
                replies = await service.cancel(user)
            elif line == "/forget":
                replies = await service.forget(user)
            elif line == "/stats":
                replies = [Reply(service.stats_text())]
            elif line == "/export":
                export = service.export()
                Path(export.filename).write_bytes(export.content)
                replies = [Reply(f"Выгрузка: {export.filename} ({export.count} заявок)")]
            else:
                replies = await service.handle_text(user, line)
            buttons = _render(replies)
