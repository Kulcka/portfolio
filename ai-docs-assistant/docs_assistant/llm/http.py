"""HTTP-клиент для провайдеров: повторы, понятные ошибки, никаких секретов в тексте."""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from docs_assistant.llm.base import LLMError
from docs_assistant.security import redact, safe_url

logger = logging.getLogger(__name__)

_MAX_RETRY_AFTER = 30.0


def ssl_context(ca_bundle: Path | None) -> ssl.SSLContext | bool:
    """Стандартные корневые сертификаты плюс дополнительный (например, Минцифры для GigaChat)."""
    if ca_bundle is None:
        return True
    import certifi

    if not Path(ca_bundle).is_file():
        raise LLMError(f"файл сертификата не найден: {ca_bundle}")
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(ca_bundle))
    return context


class JsonHttpClient:
    """POST с JSON-ответом, повторами на 429/5xx/таймаутах и безопасными ошибками."""

    def __init__(
        self,
        *,
        provider: str,
        timeout: float = 60.0,
        verify: ssl.SSLContext | bool = True,
        retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout, verify=verify, transport=transport)

    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Отправить запрос; при временных ошибках повторить с паузой. Бросает :class:`LLMError`."""
        attempt = 0
        while True:
            try:
                return self._post_once(url, headers, json_body, form)
            except LLMError as error:
                if not error.retryable or attempt >= self.retries:
                    raise
                wait = error.retry_after if error.retry_after is not None else self.backoff * (2**attempt)
                logger.warning("%s; повтор через %.1f с", error, wait)
                self._sleep(wait)
                attempt += 1

    def _post_once(
        self, url: str, headers: Mapping[str, str], json_body: Any, form: Mapping[str, str] | None
    ) -> dict[str, Any]:
        try:
            if form is not None:
                response = self._client.post(url, headers=dict(headers), data=dict(form))
            else:
                response = self._client.post(url, headers=dict(headers), json=json_body)
        except httpx.TimeoutException:
            raise LLMError(f"{self.provider}: превышено время ожидания ответа", retryable=True) from None
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise LLMError(
                    f"{self.provider}: не удалось проверить TLS-сертификат {safe_url(url)}. "
                    "Для GigaChat установите корневой сертификат Минцифры "
                    "(https://www.gosuslugi.ru/crt) и укажите путь к нему в GIGACHAT_CA_BUNDLE"
                ) from None
            raise LLMError(
                f"{self.provider}: не удалось подключиться к {safe_url(url)}", retryable=True
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(
                f"{self.provider}: сетевая ошибка ({type(exc).__name__})", retryable=True
            ) from None

        if response.status_code != 200:
            raise error_from_response(self.provider, response)
        try:
            body = response.json()
        except ValueError:
            raise LLMError(f"{self.provider}: сервис вернул не JSON") from None
        if not isinstance(body, dict):
            raise LLMError(f"{self.provider}: неожиданный формат ответа")
        return body

    def close(self) -> None:
        self._client.close()


def error_from_response(provider: str, response: httpx.Response) -> LLMError:
    status = response.status_code
    retryable = False
    if status in (401, 403):
        reason = "доступ запрещён — проверьте ключ, токен и права сервисного аккаунта"
    elif status == 404:
        reason = "адрес API или модель не найдены"
    elif status == 429:
        reason, retryable = "превышен лимит запросов", True
    elif status in (408, 409) or status >= 500:
        reason, retryable = "временная ошибка на стороне сервиса", True
    else:
        reason = "запрос отклонён"
    detail = _detail(response)
    message = f"{provider}: {reason} (HTTP {status})" + (f": {detail}" if detail else "")
    return LLMError(
        redact(message), retryable=retryable, status=status, retry_after=_retry_after(response)
    )


def _detail(response: httpx.Response) -> str:
    """Короткое описание ошибки из тела ответа провайдера."""
    text = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = str(error.get("message") or error.get("code") or "")
        elif error:
            text = str(error)
        text = text or str(body.get("message") or body.get("detail") or "")
    elif body is None:
        text = response.text
    return " ".join(text.split())[:200]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(max(float(raw), 0.0), _MAX_RETRY_AFTER)
    except ValueError:
        return None
