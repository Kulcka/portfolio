"""Создание провайдера модели и эмбеддингов по настройкам."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from docs_assistant.config import Settings
from docs_assistant.llm.base import EmbeddingProvider, LLMError, LLMProvider
from docs_assistant.llm.fake import ExtractiveProvider, FakeLLMProvider
from docs_assistant.llm.gigachat import create_gigachat_embeddings, create_gigachat_provider
from docs_assistant.llm.openai_compat import OpenAICompatibleEmbeddings, OpenAICompatibleProvider, bearer_auth
from docs_assistant.llm.yandexgpt import create_yandex_provider


def create_llm(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> LLMProvider:
    provider = settings.llm_provider
    if provider == "extractive":
        return ExtractiveProvider()
    if provider == "fake":
        return FakeLLMProvider()
    if provider == "openai":
        return OpenAICompatibleProvider(
            name="openai",
            base_url=settings.openai.base_url,
            model=settings.openai.model,
            auth=bearer_auth(settings.openai.api_key),
            timeout=settings.llm_timeout,
            transport=transport,
        )
    if provider == "yandexgpt":
        return create_yandex_provider(settings.yandex, timeout=settings.llm_timeout, transport=transport)
    if provider == "gigachat":
        return create_gigachat_provider(settings.gigachat, timeout=settings.llm_timeout, transport=transport)
    raise LLMError(f"неизвестный провайдер: {provider}")


def create_embeddings(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> EmbeddingProvider | None:
    """Эмбеддинги включаются переменной EMBEDDINGS_MODEL; без неё поиск — только BM25."""
    emb = settings.embeddings
    if emb is None:
        return None
    if emb.base_url.rstrip("/") == settings.gigachat.base_url.rstrip("/"):
        return create_gigachat_embeddings(
            settings.gigachat, model=emb.model, batch_size=emb.batch_size, timeout=settings.llm_timeout,
            transport=transport,
        )
    extra: dict[str, str] = {}
    if emb.project:
        extra["OpenAI-Project"] = emb.project
    host = urlsplit(emb.base_url).hostname or ""
    if host.endswith("api.cloud.yandex.net") and not settings.yandex.data_logging:
        extra["x-data-logging-enabled"] = "false"
    return OpenAICompatibleEmbeddings(
        base_url=emb.base_url,
        model=emb.model,
        query_model=emb.query_model,
        auth=bearer_auth(emb.api_key, extra),
        batch_size=emb.batch_size,
        timeout=settings.llm_timeout,
        transport=transport,
    )
