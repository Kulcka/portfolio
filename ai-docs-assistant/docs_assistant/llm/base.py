"""Общий интерфейс языковых моделей и эмбеддингов."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: TokenUsage | None = None


class LLMError(RuntimeError):
    """Ошибка обращения к модели.

    Текст ошибки безопасен для журнала: в нём нет ключей, токенов и полных URL
    с параметрами. Исходное исключение HTTP-клиента намеренно не цепляется
    (``raise ... from None``), чтобы его текст не попал в трассировку.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


class LLMProvider(ABC):
    """Чат-модель: получает сообщения, возвращает текст ответа."""

    name: str = "llm"
    model: str = ""

    @abstractmethod
    def complete(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.1, max_tokens: int = 700
    ) -> LLMResponse:
        """Сгенерировать ответ. Бросает :class:`LLMError`."""

    def close(self) -> None:  # noqa: B027 - необязательный хук
        """Освободить сетевые ресурсы."""


class EmbeddingProvider(ABC):
    """Модель эмбеддингов для гибридного поиска."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Идентификатор модели: по нему кэшируются векторы фрагментов."""

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы фрагментов документов."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Вектор поискового запроса."""

    def close(self) -> None:  # noqa: B027
        """Освободить сетевые ресурсы."""
