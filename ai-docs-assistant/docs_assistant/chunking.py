"""Разбивка документов на фрагменты с перекрытием.

Фрагмент не пересекает границу раздела (страницы PDF или заголовка DOCX/MD):
так ссылка «[файл, стр. N]» всегда точна. Внутри раздела текст режется по
предложениям и абзацам до ``size`` символов; соседние фрагменты перекрываются
хвостом из целых предложений не длиннее ``overlap`` символов, чтобы ответ,
попавший на стык, не потерялся.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from docs_assistant.loaders import LoadedDocument
from docs_assistant.textproc import tokenize

_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9«\"(])")


@dataclass(frozen=True)
class Chunk:
    id: str
    doc: str  # путь относительно папки документов, через «/»
    title: str
    text: str
    page: int | None
    section: str | None
    ordinal: int
    tokens: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["tokens"] = list(self.tokens)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Chunk:
        return cls(
            id=str(data["id"]),
            doc=str(data["doc"]),
            title=str(data.get("title") or ""),
            text=str(data["text"]),
            page=int(data["page"]) if data.get("page") is not None else None,  # type: ignore[arg-type]
            section=str(data["section"]) if data.get("section") else None,
            ordinal=int(data["ordinal"]),  # type: ignore[arg-type]
            tokens=tuple(data.get("tokens") or ()),  # type: ignore[arg-type]
        )


def chunk_document(
    doc: str, document: LoadedDocument, *, size: int, overlap: int, id_prefix: str
) -> list[Chunk]:
    """Нарезать документ на фрагменты."""
    chunks: list[Chunk] = []
    for section in document.sections:
        for piece in split_text(section.text, size=size, overlap=overlap):
            searchable = " ".join(filter(None, (document.title, section.heading, piece)))
            chunks.append(
                Chunk(
                    id=f"{id_prefix}-{len(chunks):04d}",
                    doc=doc,
                    title=document.title,
                    text=piece,
                    page=section.page,
                    section=section.heading,
                    ordinal=len(chunks),
                    tokens=tuple(tokenize(searchable)),
                )
            )
    return chunks


def split_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Разрезать текст на куски не длиннее ``size`` с перекрытием ``overlap``."""
    if size <= 0:
        raise ValueError("size должен быть положительным")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap должен быть в диапазоне [0, size)")

    units = list(_units(text, size))
    if not units:
        return []

    pieces: list[str] = []
    current: list[tuple[str, bool]] = []
    for unit in units:
        if current and _length(current + [unit]) > size:
            pieces.append(_join(current))
            tail = _tail(current, overlap)
            current = tail if _length(tail + [unit]) <= size else []
        current.append(unit)
    if current:
        pieces.append(_join(current))
    return pieces


def _units(text: str, size: int) -> Iterable[tuple[str, bool]]:
    """Предложения с пометкой «начинает абзац». Слишком длинные режутся по словам."""
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        first = True
        for sentence in _SENTENCE_END_RE.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            for part in _hard_split(sentence, size):
                yield part, first
                first = False


def _hard_split(sentence: str, size: int) -> Iterable[str]:
    if len(sentence) <= size:
        yield sentence
        return
    words: list[str] = []
    length = 0
    for word in sentence.split():
        while len(word) > size:  # слово длиннее фрагмента (например, base64) — режем как есть
            if words:
                yield " ".join(words)
                words, length = [], 0
            yield word[:size]
            word = word[size:]
        if words and length + 1 + len(word) > size:
            yield " ".join(words)
            words, length = [], 0
        words.append(word)
        length += len(word) + (1 if length else 0)
    if words:
        yield " ".join(words)


def _join(units: list[tuple[str, bool]]) -> str:
    out: list[str] = []
    for index, (text, starts_paragraph) in enumerate(units):
        if index:
            out.append("\n" if starts_paragraph else " ")
        out.append(text)
    return "".join(out)


def _length(units: list[tuple[str, bool]]) -> int:
    return sum(len(text) for text, _ in units) + max(len(units) - 1, 0)


def _tail(units: list[tuple[str, bool]], overlap: int) -> list[tuple[str, bool]]:
    """Последние целые предложения общей длиной не больше ``overlap``."""
    if overlap <= 0:
        return []
    tail: list[tuple[str, bool]] = []
    for unit in reversed(units[1:]):  # весь предыдущий фрагмент целиком не повторяем
        if _length([unit] + tail) > overlap:
            break
        tail.insert(0, unit)
    return tail
