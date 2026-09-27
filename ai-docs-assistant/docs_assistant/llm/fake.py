"""Провайдеры без нейросети.

* :class:`FakeLLMProvider` — для тестов: отдаёт заранее заданные ответы
  (или бросает заданные ошибки) и запоминает все запросы.
* :class:`ExtractiveProvider` — офлайн-режим для демонстрации и работы без
  ключей: берёт лучший по рангу фрагмент, цитирует из него до трёх строк и ставит
  ссылку. Не пересказывает и не рассуждает — только цитирует.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache

from docs_assistant.llm.base import ChatMessage, LLMProvider, LLMResponse
from docs_assistant.prompt import NO_ANSWER_MARKER, parse_prompt
from docs_assistant.textproc import Synonyms, tokenize

Scripted = str | Exception | Callable[[Sequence[ChatMessage]], str]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
MAX_LINES = 3


class FakeLLMProvider(LLMProvider):
    name = "fake"
    model = "fake"

    def __init__(self, responses: Iterable[Scripted] = (), *, default: Scripted | None = None) -> None:
        self._responses = list(responses)
        self._default = default
        self.calls: list[list[ChatMessage]] = []

    def complete(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.1, max_tokens: int = 700
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self._responses:
            item = self._responses.pop(0)
        elif self._default is not None:
            item = self._default
        else:
            item = cite_first_fragment
        if isinstance(item, Exception):
            raise item
        text = item(messages) if callable(item) else item
        return LLMResponse(text=text, model=self.model)


def cite_first_fragment(messages: Sequence[ChatMessage]) -> str:
    """Ответ «наивной модели»: первое предложение первого фрагмента со ссылкой."""
    _, fragments = parse_prompt(_user_content(messages))
    if not fragments:
        return NO_ANSWER_MARKER
    label, text = fragments[0]
    first = _SENTENCE_SPLIT_RE.split(text.strip(), maxsplit=1)[0]
    return f"{first} {label}"


class ExtractiveProvider(LLMProvider):
    name = "extractive"
    model = "extractive"

    def complete(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.1, max_tokens: int = 700
    ) -> LLMResponse:
        question, fragments = parse_prompt(_user_content(messages))
        groups = _query_groups(question)
        lines: list[tuple[int, str, str, set[str]]] = []  # (ранг фрагмента, ссылка, строка, основы)
        for rank, (label, text) in enumerate(fragments):
            # Заголовок раздела (он в ссылке) относится ко всем строкам раздела:
            # в FAQ вопрос стоит в заголовке, а ответ — в тексте под ним.
            label_terms = set(tokenize(label.split(",", 1)[1] if "," in label else ""))
            for line in _candidate_lines(text):
                lines.append((rank, label, line, set(tokenize(line)) | label_terms))
        if not lines or not groups:
            return LLMResponse(text=NO_ANSWER_MARKER, model=self.model)

        # Редкое среди строк слово вопроса важнее частого: «аккумулятор» весомее «посылки».
        weights = []
        for group in groups:
            containing = sum(1 for *_, terms in lines if group & terms)
            weights.append(math.log(1.0 + len(lines) / containing) if containing else 0.0)

        # Фрагменты идут в порядке ранжирования поиска: цитируем первый, где есть совпадения,
        # до MAX_LINES лучших строк в порядке документа.
        for rank in range(len(fragments)):
            scored = [
                (sum(w for group, w in zip(groups, weights) if group & terms), position, line, label)
                for position, (line_rank, label, line, terms) in enumerate(lines)
                if line_rank == rank
            ]
            best = max((score for score, *_ in scored), default=0.0)
            if best <= 0:
                continue
            chosen = sorted(
                sorted(scored, key=lambda item: (-item[0], item[1]))[:MAX_LINES], key=lambda item: item[1]
            )
            chosen = [item for item in chosen if item[0] >= 0.6 * best]
            text = " ".join(line if line.endswith((".", "!", "?", "…")) else line + "." for _, _, line, _ in chosen)
            return LLMResponse(text=f"{text} {chosen[0][3]}", model=self.model)
        return LLMResponse(text=NO_ANSWER_MARKER, model=self.model)


def _candidate_lines(text: str) -> list[str]:
    """Строки фрагмента; длинные абзацы режутся на предложения."""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= 300:
            result.append(line)
        else:
            result.extend(part.strip() for part in _SENTENCE_SPLIT_RE.split(line) if part.strip())
    return result


@lru_cache(maxsize=1)
def _synonyms() -> Synonyms:
    return Synonyms.load()


def _query_groups(question: str) -> list[frozenset[str]]:
    synonyms = _synonyms()
    groups: list[frozenset[str]] = []
    for term in dict.fromkeys(tokenize(question)):
        group = synonyms.expand(term) | {term}
        if group not in groups:
            groups.append(frozenset(group))
    return groups


def _user_content(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""
