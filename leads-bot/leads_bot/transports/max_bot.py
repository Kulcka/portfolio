"""Транспорт MAX: long polling или webhook поверх `MaxApiClient`, то же ядро, что у Telegram.

События по документации MAX (https://dev.max.ru/docs-api/objects/Update и
схема https://github.com/max-messenger/api-schema):

- `bot_started` — пользователь нажал «Начать» (поля `chat_id`, `user`);
- `message_created` — сообщение (`message.sender`, `message.recipient.chat_id`,
  `message.recipient.chat_type` = dialog/chat/channel, `message.body.text`,
  `message.body.attachments`);
- `message_callback` — нажатие callback-кнопки (`callback.callback_id`,
  `callback.payload`, `callback.user`); ответ обязателен через POST /answers.

Кнопка «Отправить мой номер» — инлайн-кнопка `request_contact`; номер приходит
вложением `contact` в формате vCard (`payload.vcf_info`).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from aiohttp import web

from ..core.models import Button, ButtonKind, Channel, Reply, UserRef
from ..core.service import LeadService
from .common import (
    ADMIN_EXPORT_CAPTION,
    ADMIN_EXPORT_FAILED,
    ADMIN_NO_LEADS,
    CHAT_ID_REPLY,
    AdminAccess,
    parse_command,
)
from .max_api import MaxApiError, MaxAuthError

log = logging.getLogger(__name__)

UPDATE_TYPES = ("bot_started", "message_created", "message_callback")
MARKER_KEY = "max.updates_marker"
SECRET_HEADER = "X-Max-Bot-Api-Secret"
BOT_COMMANDS = (
    ("start", "Оставить заявку"),
    ("cancel", "Отменить заявку"),
    ("forget", "Удалить мои данные"),
)
# Ошибки, после которых работу можно продолжить: сеть, таймаут, ответ API с ошибкой.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (MaxApiError, aiohttp.ClientError, asyncio.TimeoutError)


class MaxApi(Protocol):
    """То, что транспорт использует из `MaxApiClient` (в тестах подменяется)."""

    async def get_me(self) -> dict[str, Any]: ...

    async def set_commands(self, commands: Iterable[tuple[str, str]]) -> None: ...

    async def get_updates(
        self, *, marker: int | None = None, timeout: int = 30, limit: int = 100, types: Iterable[str] = ()
    ) -> tuple[list[dict[str, Any]], int | None]: ...

    async def send_message(
        self,
        *,
        text: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
        text_format: str | None = "html",
    ) -> dict[str, Any]: ...

    async def answer_callback(
        self, callback_id: str, *, notification: str | None = None, message: dict[str, Any] | None = None
    ) -> None: ...

    async def upload_file(self, filename: str, content: bytes) -> str: ...

    async def get_subscriptions(self) -> list[dict[str, Any]]: ...

    async def subscribe(self, url: str, *, update_types: Iterable[str], secret: str | None = None) -> None: ...


# --- преобразования ------------------------------------------------------------------------


def user_ref(user: dict[str, Any]) -> UserRef:
    name = user.get("name") or " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return UserRef(
        channel=Channel.MAX,
        user_id=int(user["user_id"]),
        username=user.get("username") or None,
        display_name=name or None,
    )


def _button(button: Button) -> dict[str, Any]:
    if button.kind is ButtonKind.URL:
        return {"type": "link", "text": button.text, "url": button.value}
    if button.kind is ButtonKind.REQUEST_CONTACT:
        return {"type": "request_contact", "text": button.text}
    return {"type": "callback", "text": button.text, "payload": button.value}


def reply_attachments(reply: Reply) -> list[dict[str, Any]] | None:
    if not reply.keyboard:
        return None
    rows = [[_button(b) for b in row] for row in reply.keyboard]
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def phone_from_vcf(vcf: str) -> str | None:
    """Достать номер из vCard: `TEL;TYPE=cell:+79991234567`, `item1.TEL:...`, `TEL;VALUE=uri:tel:+7...`."""
    for line in vcf.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.partition(":")
        if not sep:
            continue
        prop = key.split(";", 1)[0].rsplit(".", 1)[-1].strip().upper()
        if prop != "TEL":
            continue
        value = value.strip()
        if value.lower().startswith("tel:"):
            value = value[4:]
        if value:
            return value
    return None


def contact_phone(attachments: Sequence[dict[str, Any]]) -> str | None:
    """Номер из вложения `contact`. Пустая строка — контакт есть, номера нет; None — контакта нет."""
    for attachment in attachments:
        if attachment.get("type") == "contact":
            payload = attachment.get("payload") or {}
            return phone_from_vcf(payload.get("vcf_info") or "") or ""
    return None


@dataclass(frozen=True)
class _Target:
    chat_id: int | None
    user_id: int | None

    def kwargs(self) -> dict[str, int]:
        if self.chat_id is not None:
            return {"chat_id": self.chat_id}
        if self.user_id is not None:
            return {"user_id": self.user_id}
        raise ValueError("не известно, кому отвечать")


class MaxNotifier:
    name = "max"

    def __init__(self, api: MaxApi, chat_id: int) -> None:
        self._api = api
        self._chat_id = chat_id

    async def notify_admin(self, text: str) -> None:
        await self._api.send_message(chat_id=self._chat_id, text=text)


# --- транспорт -----------------------------------------------------------------------------


class MaxTransport:
    def __init__(
        self,
        api: MaxApi,
        service: LeadService,
        admin: AdminAccess,
        *,
        webhook_secret: str | None = None,
        keyboard_cache_size: int = 1000,
    ) -> None:
        self._api = api
        self._service = service
        self._admin = admin
        self._webhook_secret = webhook_secret
        # id сообщения с кнопками → его текст: после нажатия переписываем сообщение без кнопок.
        self._keyboard_texts: OrderedDict[str, str] = OrderedDict()
        self._keyboard_cache_size = keyboard_cache_size

    def notifier(self) -> MaxNotifier | None:
        return MaxNotifier(self._api, self._admin.chat_id) if self._admin.chat_id is not None else None

    # --- запуск -------------------------------------------------------------------------------

    async def _prepare(self, mode: str) -> None:
        me = await self._api.get_me()  # проверка токена: при ошибке — MaxAuthError
        log.info("MAX: бот %s запущен (%s)", me.get("username") or me.get("name") or me.get("user_id"), mode)
        try:
            await self._api.set_commands(BOT_COMMANDS)
        except TRANSIENT_ERRORS as exc:
            log.warning("MAX: не удалось задать меню команд: %s", exc)

    async def run_polling(
        self,
        *,
        poll_timeout: int = 30,
        first_backoff: float = 1.0,
        max_backoff: float = 60.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        await self._prepare("long polling")
        try:
            if await self._api.get_subscriptions():
                log.warning(
                    "MAX: у бота есть webhook-подписка — long polling с ней не работает. "
                    "Удалите подписку (DELETE /subscriptions) или включите MAX_MODE=webhook"
                )
        except TRANSIENT_ERRORS as exc:
            log.debug("MAX: не удалось проверить подписки: %s", exc)

        storage = self._service.storage
        saved = storage.get_value(MARKER_KEY)
        marker = int(saved) if saved else None
        backoff = first_backoff
        while stop is None or not stop.is_set():
            try:
                updates, new_marker = await self._api.get_updates(marker=marker, timeout=poll_timeout, types=UPDATE_TYPES)
            except MaxAuthError:
                raise
            except TRANSIENT_ERRORS as exc:
                log.warning("MAX: ошибка получения событий (%s), повтор через %.0f с", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            backoff = first_backoff
            for update in updates:
                await self._safe_process(update)
            if new_marker is not None and new_marker != marker:
                marker = new_marker
                storage.set_value(MARKER_KEY, str(marker))

    async def run_webhook(self, *, public_url: str, path: str, host: str, port: int) -> None:
        """Поднять HTTP-приёмник и подписаться на события. HTTPS/443 обеспечивает прокси (nginx)."""
        await self._prepare("webhook")
        runner = web.AppRunner(self.webhook_app(path), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("MAX: приём webhook на %s:%d%s", host, port, path)
        try:
            await self._api.subscribe(public_url, update_types=UPDATE_TYPES, secret=self._webhook_secret)
            log.info("MAX: подписка на события оформлена")
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    def webhook_app(self, path: str) -> web.Application:
        async def handle(request: web.Request) -> web.Response:
            status = await self.handle_webhook(await request.read(), request.headers.get(SECRET_HEADER))
            return web.Response(status=status)

        async def health(_: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post(path, handle)
        app.router.add_get("/healthz", health)
        return app

    async def handle_webhook(self, body: bytes, secret_header: str | None) -> int:
        """Проверить секрет и обработать событие. Возвращает HTTP-статус ответа."""
        if self._webhook_secret is not None:
            if not hmac.compare_digest((secret_header or "").encode(), self._webhook_secret.encode()):
                log.warning("MAX: webhook-запрос с неверным секретом отклонён")
                return 403
        try:
            update = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400
        if not isinstance(update, dict):
            return 400
        await self._safe_process(update)
        return 200

    # --- обработка событий -------------------------------------------------------------------

    async def _safe_process(self, update: dict[str, Any]) -> None:
        try:
            await self.process_update(update)
        except Exception:  # одно битое событие не должно останавливать бота
            log.exception("MAX: ошибка обработки события %s", update.get("update_type"))

    async def process_update(self, update: dict[str, Any]) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            await self._on_bot_started(update)
        elif kind == "message_created":
            await self._on_message(update.get("message") or {})
        elif kind == "message_callback":
            await self._on_callback(update.get("callback") or {}, update.get("message"))
        else:
            log.debug("MAX: событие %s пропущено", kind)

    async def _on_bot_started(self, update: dict[str, Any]) -> None:
        if not update.get("user"):
            return
        user = user_ref(update["user"])
        target = _Target(chat_id=update.get("chat_id"), user_id=user.user_id)
        await self._send(target, await self._service.start(user))

    async def _on_message(self, message: dict[str, Any]) -> None:
        sender = message.get("sender")
        if not sender or sender.get("is_bot"):
            return
        user = user_ref(sender)
        recipient = message.get("recipient") or {}
        chat_id = recipient.get("chat_id")
        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        command = parse_command(text)
        target = _Target(chat_id=chat_id, user_id=user.user_id)

        if command == "id":
            await self._api.send_message(text=CHAT_ID_REPLY.format(chat_id=chat_id, user_id=user.user_id), **target.kwargs())
            return
        if command in ("stats", "export") and self._admin.is_admin(chat_id, user.user_id):
            await (self._send_stats(target) if command == "stats" else self._send_export(target))
            return
        if recipient.get("chat_type") != "dialog":
            return  # сценарий заявки — только в личном диалоге с ботом

        if command == "start":
            replies = await self._service.start(user)
        elif command == "cancel":
            replies = await self._service.cancel(user)
        elif command == "forget":
            replies = await self._service.forget(user)
        else:
            phone = contact_phone(body.get("attachments") or [])
            if phone is not None:
                replies = await self._service.handle_contact(user, phone)
            elif text:
                replies = await self._service.handle_text(user, text)
            else:
                replies = await self._service.handle_other(user)
        await self._send(target, replies)

    async def _on_callback(self, callback: dict[str, Any], message: dict[str, Any] | None) -> None:
        callback_id = callback.get("callback_id")
        if not callback_id or not callback.get("user"):
            return
        user = user_ref(callback["user"])
        message = message or {}
        recipient = message.get("recipient") or {}
        await self._acknowledge(str(callback_id), (message.get("body") or {}).get("mid"))
        if recipient.get("chat_type") not in (None, "dialog"):
            return
        replies = await self._service.handle_button(user, callback.get("payload") or "")
        await self._send(_Target(chat_id=recipient.get("chat_id"), user_id=user.user_id), replies)

    async def _acknowledge(self, callback_id: str, mid: str | None) -> None:
        """Ответить на нажатие; если знаем текст сообщения — переписать его без кнопок."""
        original = self._keyboard_texts.pop(mid, None) if mid else None
        try:
            if original is not None:
                await self._api.answer_callback(
                    callback_id, message={"text": original, "format": "html", "attachments": []}
                )
            else:
                await self._api.answer_callback(callback_id, notification=self._service.config.texts.callback_ack)
        except TRANSIENT_ERRORS as exc:
            log.warning("MAX: ответ на нажатие кнопки не принят: %s", exc)

    # --- отправка ----------------------------------------------------------------------------

    async def _send(self, target: _Target, replies: Sequence[Reply]) -> None:
        for reply in replies:
            attachments = reply_attachments(reply)
            result = await self._api.send_message(text=reply.text, attachments=attachments, **target.kwargs())
            if attachments:
                mid = (((result or {}).get("message") or {}).get("body") or {}).get("mid")
                if mid:
                    self._remember_keyboard(str(mid), reply.text)

    def _remember_keyboard(self, mid: str, text: str) -> None:
        self._keyboard_texts[mid] = text
        while len(self._keyboard_texts) > self._keyboard_cache_size:
            self._keyboard_texts.popitem(last=False)

    async def _send_stats(self, target: _Target) -> None:
        await self._api.send_message(text=self._service.stats_text(), **target.kwargs())

    async def _send_export(self, target: _Target) -> None:
        export = self._service.export()
        if export.count == 0:
            await self._api.send_message(text=ADMIN_NO_LEADS, **target.kwargs())
            return
        try:
            token = await self._api.upload_file(export.filename, export.content)
            await self._api.send_message(
                text=ADMIN_EXPORT_CAPTION.format(count=export.count),
                attachments=[{"type": "file", "payload": {"token": token}}],
                **target.kwargs(),
            )
            log.info("MAX: выгрузка %d заявок отправлена", export.count)
        except TRANSIENT_ERRORS as exc:
            log.warning("MAX: не удалось отправить выгрузку: %s", exc)
            await self._api.send_message(text=ADMIN_EXPORT_FAILED, **target.kwargs())
