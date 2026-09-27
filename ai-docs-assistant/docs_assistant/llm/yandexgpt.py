"""YandexGPT (Yandex Cloud, сервис AI Studio — бывший Foundation Models).

Два режима (переменная ``YANDEX_API_MODE``):

* ``native`` — REST API генерации текста:
  ``POST https://llm.api.cloud.yandex.net/foundationModels/v1/completion``,
  тело ``{"modelUri": "gpt://<каталог>/yandexgpt-lite", "completionOptions": {...},
  "messages": [{"role": ..., "text": ...}]}``;
* ``openai`` — OpenAI-совместимый API ``https://ai.api.cloud.yandex.net/v1``
  (``/chat/completions``), где доступны и сторонние модели каталога
  (Qwen, gpt-oss и др.); каталог передаётся заголовком ``OpenAI-Project``.

Авторизация: API-ключ сервисного аккаунта (``Authorization: Api-Key <ключ>``
в native, ``Bearer <ключ>`` в openai) или IAM-токен (``Bearer <токен>``;
для аккаунта пользователя нужен ``x-folder-id``).

По умолчанию шлём ``x-data-logging-enabled: false``: по документации Yandex
Cloud запросы с этим заголовком не сохраняются на серверах (важно для
персональных данных). Отключается переменной ``YANDEX_DATA_LOGGING=true``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from docs_assistant.config import YandexSettings
from docs_assistant.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse, TokenUsage
from docs_assistant.llm.http import JsonHttpClient
from docs_assistant.llm.openai_compat import OpenAICompatibleProvider, StaticAuth
from docs_assistant.security import REGISTRY


def model_uri(folder_id: str, model: str, scheme: str = "gpt") -> str:
    """``yandexgpt-lite`` → ``gpt://<каталог>/yandexgpt-lite``; готовый URI не трогаем."""
    if "://" in model:
        return model
    if not folder_id:
        raise LLMError("yandexgpt: задайте YANDEX_FOLDER_ID (идентификатор каталога Yandex Cloud)")
    return f"{scheme}://{folder_id}/{model}"


def _auth_headers(settings: YandexSettings, *, openai_mode: bool) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.api_key:
        REGISTRY.add(settings.api_key)
        prefix = "Bearer" if openai_mode else "Api-Key"
        headers["Authorization"] = f"{prefix} {settings.api_key.get()}"
    elif settings.iam_token:
        REGISTRY.add(settings.iam_token)
        headers["Authorization"] = f"Bearer {settings.iam_token.get()}"
        if settings.folder_id and not openai_mode:
            headers["x-folder-id"] = settings.folder_id
    else:
        raise LLMError("yandexgpt: задайте YANDEX_API_KEY или YANDEX_IAM_TOKEN")
    if openai_mode and settings.folder_id:
        headers["OpenAI-Project"] = settings.folder_id
    if not settings.data_logging:
        headers["x-data-logging-enabled"] = "false"
    return headers


class YandexGPTProvider(LLMProvider):
    """Нативный REST API генерации текста."""

    name = "yandexgpt"

    def __init__(
        self,
        settings: YandexSettings,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model_uri(settings.folder_id, settings.model)
        self._url = settings.native_url
        self._headers = _auth_headers(settings, openai_mode=False)
        self._http = JsonHttpClient(
            provider=self.name, timeout=timeout, retries=retries, transport=transport, sleep=sleep
        )

    def complete(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.1, max_tokens: int = 700
    ) -> LLMResponse:
        payload = {
            "modelUri": self.model,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),  # в API это строка (int64 в JSON)
            },
            "messages": [{"role": m.role, "text": m.content} for m in messages],
        }
        data = self._http.post_json(self._url, headers=self._headers, json_body=payload)
        return parse_yandex_completion(data, model=self.model)

    def close(self) -> None:
        self._http.close()


def parse_yandex_completion(data: dict[str, Any], *, model: str) -> LLMResponse:
    result = data.get("result", data)
    alternatives = result.get("alternatives") if isinstance(result, dict) else None
    if not isinstance(alternatives, list) or not alternatives:
        raise LLMError("yandexgpt: в ответе нет вариантов (alternatives)")
    first = alternatives[0]
    if first.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
        raise LLMError("yandexgpt: генерация остановлена фильтром содержимого")
    message = first.get("message") or {}
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str):
        raise LLMError("yandexgpt: в ответе нет текста")

    usage = None
    raw_usage = result.get("usage")
    if isinstance(raw_usage, dict):
        usage = TokenUsage(
            prompt_tokens=_int(raw_usage.get("inputTextTokens")),
            completion_tokens=_int(raw_usage.get("completionTokens")),
            total_tokens=_int(raw_usage.get("totalTokens")),
        )
    return LLMResponse(text=text.strip(), model=str(result.get("modelVersion") or model), usage=usage)


def create_yandex_provider(
    settings: YandexSettings,
    *,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMProvider:
    if settings.api_mode == "openai":
        return OpenAICompatibleProvider(
            name="yandexgpt",
            base_url=settings.openai_base_url,
            model=model_uri(settings.folder_id, settings.model),
            auth=StaticAuth(_auth_headers(settings, openai_mode=True)),
            timeout=timeout,
            transport=transport,
            sleep=sleep,
        )
    return YandexGPTProvider(settings, timeout=timeout, transport=transport, sleep=sleep)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
