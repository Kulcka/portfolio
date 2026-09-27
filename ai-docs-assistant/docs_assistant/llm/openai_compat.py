"""OpenAI-совместимый API: OpenAI, OpenRouter, Ollama, vLLM, LM Studio и др.

Тот же клиент используется для YandexGPT в режиме совместимости с OpenAI
и для GigaChat — отличается только способ авторизации (:class:`HeaderAuth`).
"""

from __future__ import annotations

import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import httpx

from docs_assistant.llm.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMError,
    LLMProvider,
    LLMResponse,
    TokenUsage,
)
from docs_assistant.llm.http import JsonHttpClient
from docs_assistant.security import REGISTRY, Secret, safe_url

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class HeaderAuth(Protocol):
    """Источник заголовков авторизации."""

    def headers(self) -> dict[str, str]: ...

    def invalidate(self) -> None:
        """Сервис ответил 401 — сбросить закэшированный токен (если он есть)."""


class StaticAuth:
    """Постоянные заголовки: API-ключ и служебные заголовки провайдера."""

    refreshable = False

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        self._headers = dict(headers or {})

    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    def invalidate(self) -> None:
        return None


def bearer_auth(key: Secret | None, extra: Mapping[str, str] | None = None) -> StaticAuth:
    headers: dict[str, str] = {}
    if key:
        REGISTRY.add(key)
        headers["Authorization"] = f"Bearer {key.get()}"
    headers.update(extra or {})
    return StaticAuth(headers)


class OpenAICompatibleClient:
    """POST на ``base_url + path`` с авторизацией; при 401 один раз обновляет токен."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        auth: HeaderAuth,
        timeout: float = 60.0,
        verify: ssl.SSLContext | bool = True,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.http = JsonHttpClient(
            provider=name, timeout=timeout, verify=verify, retries=retries, transport=transport, sleep=sleep
        )

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + path
        try:
            return self.http.post_json(url, headers=self._headers(), json_body=payload)
        except LLMError as error:
            if error.status != 401 or not getattr(self.auth, "refreshable", False):
                raise
            self.auth.invalidate()
            return self.http.post_json(url, headers=self._headers(), json_body=payload)

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", **self.auth.headers()}

    def close(self) -> None:
        self.http.close()


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        auth: HeaderAuth | None = None,
        name: str = "openai",
        timeout: float = 60.0,
        verify: ssl.SSLContext | bool = True,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model:
            raise LLMError(f"{name}: не указана модель")
        self.name = name
        self.model = model
        self._client = OpenAICompatibleClient(
            name=name,
            base_url=base_url,
            auth=auth or StaticAuth(),
            timeout=timeout,
            verify=verify,
            retries=retries,
            transport=transport,
            sleep=sleep,
        )

    @property
    def endpoint(self) -> str:
        return safe_url(self._client.base_url)

    def complete(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.1, max_tokens: int = 700
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = self._client.post("/chat/completions", payload)
        return parse_chat_completion(data, provider=self.name, model=self.model)

    def close(self) -> None:
        self._client.close()


def parse_chat_completion(data: dict[str, Any], *, provider: str, model: str) -> LLMResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError(f"{provider}: в ответе нет вариантов (choices)")
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # формат «части сообщения»
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        content = ""
    content = _THINK_RE.sub("", content).strip()  # рассуждения reasoning-моделей в ответ не идут

    usage = None
    raw_usage = data.get("usage")
    if isinstance(raw_usage, dict):
        usage = TokenUsage(
            prompt_tokens=_int(raw_usage.get("prompt_tokens")),
            completion_tokens=_int(raw_usage.get("completion_tokens")),
            total_tokens=_int(raw_usage.get("total_tokens")),
        )
    return LLMResponse(text=content, model=str(data.get("model") or model), usage=usage)


class OpenAICompatibleEmbeddings(EmbeddingProvider):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        query_model: str | None = None,
        auth: HeaderAuth | None = None,
        batch_size: int = 16,
        name: str = "embeddings",
        timeout: float = 60.0,
        verify: ssl.SSLContext | bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.query_model = query_model or model
        self.batch_size = max(1, batch_size)
        self._client = OpenAICompatibleClient(
            name=name,
            base_url=base_url,
            auth=auth or StaticAuth(),
            timeout=timeout,
            verify=verify,
            transport=transport,
            sleep=sleep,
        )

    @property
    def model_id(self) -> str:
        return f"{safe_url(self._client.base_url)}|{self.model}"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed(list(texts[start : start + self.batch_size]), self.model))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], self.query_model)[0]

    def _embed(self, batch: list[str], model: str) -> list[list[float]]:
        # Некоторые сервисы (например, YandexGPT) принимают только одну строку за запрос.
        payload = {"model": model, "input": batch[0] if len(batch) == 1 else batch, "encoding_format": "float"}
        data = self._client.post("/embeddings", payload)
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(batch):
            raise LLMError(f"{self._client.name}: число векторов не совпало с числом текстов")
        items = sorted(items, key=lambda item: item.get("index", 0))
        vectors = []
        for item in items:
            vector = item.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise LLMError(f"{self._client.name}: пустой вектор в ответе")
            vectors.append([float(x) for x in vector])
        return vectors

    def close(self) -> None:
        self._client.close()


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
