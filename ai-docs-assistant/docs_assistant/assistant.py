"""Сквозной сценарий: вопрос → поиск → модель → проверка → ответ со ссылками."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Literal

from docs_assistant.citations import check_citations, source_label
from docs_assistant.config import Settings
from docs_assistant.embeddings import EmbeddingCache, VectorIndex
from docs_assistant.index import IndexStore, SyncReport
from docs_assistant.llm.base import EmbeddingProvider, LLMError, LLMProvider, TokenUsage
from docs_assistant.prompt import build_prompt, is_no_answer, looks_like_injection
from docs_assistant.retriever import Retriever, SearchHit, SearchResult
from docs_assistant.textproc import Synonyms

logger = logging.getLogger(__name__)

Status = Literal["answered", "no_answer", "error"]

REFUSAL_TEMPLATE = "В документах нет ответа на этот вопрос. Пожалуйста, свяжитесь с {contact}."
ERROR_TEMPLATE = (
    "Не получилось подготовить ответ: сервис временно недоступен. "
    "Попробуйте позже или свяжитесь с {contact}."
)
EMPTY_QUESTION_TEXT = "Напишите вопрос текстом — например: «Сколько стоит доставка посылки до 5 кг?»"


@dataclass(frozen=True)
class SourceRef:
    label: str
    doc: str
    page: int | None
    section: str | None
    snippet: str


@dataclass(frozen=True)
class Answer:
    question: str
    text: str
    status: Status
    sources: tuple[SourceRef, ...] = ()
    hits: tuple[SearchHit, ...] = ()
    reason: str = ""
    usage: TokenUsage | None = None

    def render(self) -> str:
        """Текст для пользователя: ответ и список источников."""
        if not self.sources:
            return self.text
        lines = [self.text, "", "Источники:"]
        lines.extend(f"• {source.label}" for source in self.sources)
        return "\n".join(lines)


class DocsAssistant:
    def __init__(
        self,
        settings: Settings,
        llm: LLMProvider,
        *,
        embeddings: EmbeddingProvider | None = None,
        store: IndexStore | None = None,
        synonyms: Synonyms | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.embeddings = embeddings
        self.store = store or IndexStore(
            settings.index_dir,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            max_file_bytes=int(settings.max_file_mb * 1024 * 1024),
        )
        extra = [settings.synonyms_file] if settings.synonyms_file else []
        self.synonyms = synonyms or Synonyms.load(extra)
        self._retriever: Retriever | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> DocsAssistant:
        from docs_assistant.llm.factory import create_embeddings, create_llm

        return cls(settings, create_llm(settings), embeddings=create_embeddings(settings))

    # --- индекс --------------------------------------------------------------

    def reindex(self) -> SyncReport:
        """Синхронизировать индекс с папкой документов и перезагрузить поиск."""
        with self._lock:
            report = self.store.sync(self.settings.docs_dir)
            self._retriever = self._build_retriever()
        logger.info("Индекс обновлён: %s", report.summary().replace("\n", " "))
        return report

    def load(self) -> None:
        """Загрузить готовый индекс с диска (без чтения документов)."""
        with self._lock:
            self._retriever = self._build_retriever()

    def _build_retriever(self) -> Retriever:
        chunks = self.store.load_chunks()
        vectors = None
        if self.embeddings is not None and chunks:
            try:
                cache = EmbeddingCache(self.settings.index_dir, self.embeddings)
                vectors = VectorIndex(cache.vectors_for(chunks))
            except LLMError as exc:
                logger.warning("Эмбеддинги не посчитаны (%s) — поиск только по BM25", exc)
        min_similarity = self.settings.embeddings.min_similarity if self.settings.embeddings else None
        return Retriever(
            chunks,
            synonyms=self.synonyms,
            min_coverage=self.settings.min_coverage,
            vectors=vectors,
            embedder=self.embeddings if vectors is not None else None,
            min_similarity=min_similarity,
        )

    @property
    def retriever(self) -> Retriever:
        retriever = self._retriever
        if retriever is None:
            self.load()
            retriever = self._retriever
            if retriever is not None and not retriever.chunks:
                self.reindex()
                retriever = self._retriever
        assert retriever is not None
        return retriever

    def stats(self) -> dict[str, int]:
        chunks = self.retriever.chunks
        return {"documents": len({chunk.doc for chunk in chunks}), "chunks": len(chunks)}

    # --- ответы --------------------------------------------------------------

    def search(self, question: str, k: int | None = None) -> SearchResult:
        return self.retriever.search(question, k or self.settings.top_k)

    def ask(self, question: str) -> Answer:
        question = " ".join(question.split())[: self.settings.max_question_chars]
        if not question:
            return Answer(question="", text=EMPTY_QUESTION_TEXT, status="no_answer", reason="пустой вопрос")

        if looks_like_injection(question):
            # Клиенту службы доставки незачем просить бота «забыть инструкции»:
            # такой вопрос не отправляем модели вовсе.
            logger.warning("Вопрос похож на попытку изменить инструкции модели — отказ без вызова модели")
            return self._refusal(question, (), "вопрос похож на промпт-инъекцию")

        result = self.search(question)
        if not result.relevant:
            return self._refusal(question, result.hits, f"поиск: {result.reason}")

        prompt = build_prompt(question, result.hits, company=self.settings.company_name)
        for fragment in prompt.fragments:
            if fragment.suspicious:
                logger.warning("Фрагмент %s похож на промпт-инъекцию — помечен для модели", fragment.label)

        try:
            response = self.llm.complete(
                prompt.messages,
                temperature=self.settings.llm_temperature,
                max_tokens=self.settings.llm_max_tokens,
            )
        except LLMError as exc:
            logger.error("Модель не ответила: %s", exc)
            return Answer(
                question=question,
                text=ERROR_TEMPLATE.format(contact=self.settings.manager_contact),
                status="error",
                hits=result.hits,
                reason=str(exc),
            )

        text = response.text.strip()
        if prompt.canary in text:
            logger.warning("Модель попыталась вывести служебные инструкции — ответ заменён отказом")
            return self._refusal(question, result.hits, "модель раскрыла служебную метку", response.usage)
        if is_no_answer(text):
            return self._refusal(question, result.hits, "модель: во фрагментах нет ответа", response.usage)

        check = check_citations(text, prompt.labels)
        if check.invalid:
            logger.warning("Модель сослалась на непереданные источники: %s", "; ".join(check.invalid))
        if self.settings.require_citations and not check.valid:
            return self._refusal(question, result.hits, "ответ без ссылок на документы", response.usage)

        by_label = {source_label(h.chunk.doc, h.chunk.page, h.chunk.section): h for h in result.hits}
        sources = tuple(
            SourceRef(
                label=label,
                doc=by_label[label].chunk.doc,
                page=by_label[label].chunk.page,
                section=by_label[label].chunk.section,
                snippet=by_label[label].chunk.text[:200],
            )
            for label in check.valid
        )
        return Answer(
            question=question,
            text=check.text,
            status="answered",
            sources=sources,
            hits=result.hits,
            reason=result.reason,
            usage=response.usage,
        )

    def _refusal(
        self,
        question: str,
        hits: tuple[SearchHit, ...],
        reason: str,
        usage: TokenUsage | None = None,
    ) -> Answer:
        return Answer(
            question=question,
            text=REFUSAL_TEMPLATE.format(contact=self.settings.manager_contact),
            status="no_answer",
            hits=hits,
            reason=reason,
            usage=usage,
        )

    def close(self) -> None:
        self.llm.close()
        if self.embeddings is not None:
            self.embeddings.close()
