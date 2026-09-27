"""Тонкий клиент HTTP Bot API мессенджера MAX.

Написан по официальной документации https://dev.max.ru/docs-api и схеме
https://github.com/max-messenger/api-schema (прочитаны 27.09.2026):

- базовый адрес `https://platform-api2.max.ru`;
- токен — только в заголовке `Authorization: <token>` (через query-параметр
  больше не принимается);
- сертификат сервера выдан «Russian Trusted Sub CA» (Минцифры) — корневой
  сертификат «Russian Trusted Root CA» нужно добавить в доверенные, либо
  указать путь к нему в `MAX_CA_BUNDLE`;
- ошибки приходят как `{"code": "...", "message": "..."}`.

Токен не попадает ни в URL, ни в тексты исключений.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiohttp

from ..logging_setup import mask_secrets

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://platform-api2.max.ru"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ATTACHMENT_NOT_READY = "attachment.not.ready"


class MaxApiError(Exception):
    def __init__(self, status: int, code: str | None, description: str) -> None:
        self.status = status
        self.code = code
        self.description = description
        super().__init__(f"MAX API: HTTP {status}" + (f", {code}" if code else "") + f": {description}")


class MaxAuthError(MaxApiError):
    """Токен не принят (HTTP 401) — повторять запрос бессмысленно."""


def build_ssl_context(ca_bundle: Path | None) -> ssl.SSLContext:
    """Системные корневые сертификаты плюс, при необходимости, сертификат Минцифры из файла."""
    context = ssl.create_default_context()
    if ca_bundle is not None:
        context.load_verify_locations(cafile=str(ca_bundle))
    return context


class MaxApiClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        ca_bundle: Path | None = None,
        session: aiohttp.ClientSession | None = None,
        request_timeout: float = 30.0,
        retry_delay: float = 1.0,
    ) -> None:
        if not token:
            raise ValueError("пустой токен MAX")
        self.__token = token
        self._base_url = base_url.rstrip("/")
        self._ca_bundle = ca_bundle
        self._session = session
        self._owns_session = session is None
        self._request_timeout = request_timeout
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return f"MaxApiClient(base_url={self._base_url!r})"

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            connector = aiohttp.TCPConnector(ssl=build_ssl_context(self._ca_bundle))
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        timeout: float | None = None,
        authorize: bool = True,
    ) -> Any:
        url = path_or_url if path_or_url.startswith("https://") else self._base_url + path_or_url
        headers = {"Authorization": self.__token} if authorize else {}
        clean_params = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        client_timeout = aiohttp.ClientTimeout(total=timeout or self._request_timeout)
        async with self._get_session().request(
            method,
            url,
            params=clean_params or None,
            json=json_body,
            data=data,
            headers=headers,
            timeout=client_timeout,
        ) as response:
            body = await response.text()
            status = response.status
        payload = _parse_json(body)
        if status >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else None
            description = mask_secrets(str(message or body or "пустой ответ")[:300], [self.__token])
            error_cls = MaxAuthError if status == 401 else MaxApiError
            raise error_cls(status, code, description)
        return payload

    # --- методы API -----------------------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def set_commands(self, commands: Iterable[tuple[str, str]]) -> None:
        body = {"commands": [{"name": name, "description": description} for name, description in commands]}
        await self._request("PATCH", "/me/commands", json_body=body)

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
        types: Iterable[str] = (),
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Long polling: GET /updates. Возвращает события и маркер для следующего запроса."""
        params = {
            "marker": marker,
            "timeout": timeout,
            "limit": limit,
            "types": ",".join(types) or None,
        }
        result = await self._request("GET", "/updates", params=params, timeout=timeout + 15)
        if not isinstance(result, dict):
            return [], None
        return list(result.get("updates") or []), result.get("marker")

    async def send_message(
        self,
        *,
        text: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
        text_format: str | None = "html",
        not_ready_retries: int = 5,
    ) -> dict[str, Any]:
        """POST /messages. Если вложение ещё обрабатывается (`attachment.not.ready`) — повтор с паузой."""
        if (chat_id is None) == (user_id is None):
            raise ValueError("нужно указать ровно одно из chat_id или user_id")
        body: dict[str, Any] = {"text": text}
        if attachments:
            body["attachments"] = attachments
        if text_format:
            body["format"] = text_format
        params = {"chat_id": chat_id, "user_id": user_id}

        delay = self._retry_delay
        for attempt in range(not_ready_retries + 1):
            try:
                return await self._request("POST", "/messages", params=params, json_body=body)
            except MaxApiError as exc:
                not_ready = exc.code == ATTACHMENT_NOT_READY or ATTACHMENT_NOT_READY in exc.description
                if not not_ready or attempt == not_ready_retries:
                    raise
                log.debug("MAX: вложение ещё не готово, повтор через %.0f с", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
        raise AssertionError("unreachable")

    async def answer_callback(
        self,
        callback_id: str,
        *,
        notification: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> None:
        """POST /answers — ответ на нажатие callback-кнопки."""
        body: dict[str, Any] = {}
        if notification is not None:
            body["notification"] = notification
        if message is not None:
            body["message"] = message
        await self._request("POST", "/answers", params={"callback_id": callback_id}, json_body=body)

    async def upload_file(self, filename: str, content: bytes, content_type: str = XLSX_CONTENT_TYPE) -> str:
        """Загрузить файл: POST /uploads?type=file → multipart на выданный URL. Вернёт токен вложения."""
        endpoint = await self._request("POST", "/uploads", params={"type": "file"})
        upload_url = endpoint.get("url") if isinstance(endpoint, dict) else None
        if not upload_url:
            raise MaxApiError(200, None, "POST /uploads не вернул адрес загрузки")
        form = aiohttp.FormData()
        form.add_field("data", content, filename=filename, content_type=content_type)
        # Токен бота на сервер загрузки не передаём: для multipart-загрузки он не нужен по документации.
        uploaded = await self._request("POST", upload_url, data=form, authorize=False, timeout=120)
        token = (endpoint.get("token") if isinstance(endpoint, dict) else None) or (
            uploaded.get("token") if isinstance(uploaded, dict) else None
        )
        if not token:
            raise MaxApiError(200, None, "сервер загрузки не вернул токен файла")
        return str(token)

    async def get_subscriptions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/subscriptions")
        return list(result.get("subscriptions") or []) if isinstance(result, dict) else []

    async def subscribe(self, url: str, *, update_types: Iterable[str], secret: str | None = None) -> None:
        body: dict[str, Any] = {"url": url, "update_types": list(update_types)}
        if secret:
            body["secret"] = secret
        await self._request("POST", "/subscriptions", json_body=body)


def _param(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _parse_json(body: str) -> Any:
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}
