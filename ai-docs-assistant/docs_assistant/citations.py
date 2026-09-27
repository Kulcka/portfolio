"""Ссылки на источники: формат «[файл, стр. N]» и проверка ссылок в ответе модели.

Модель обязана ссылаться только на фрагменты, которые ей передали. Ссылки
на что-то другое (выдуманный документ, чужая страница) из ответа удаляются,
а допустимые приводятся к единому виду. Понимаются и частые вольности модели:
«[tarify.pdf, с. 2]», «[Tarify.pdf, страница 2]», «[2]» (номер фрагмента),
несколько источников в одних скобках через «;».
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_CITATION_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")
_FRAGMENT_NUMBER_RE = re.compile(r"^(?:фрагмент|источник|fragment|source)?\s*№?\s*(\d{1,3})$", re.IGNORECASE)
_MAX_SECTION_LEN = 80


def source_label(doc: str, page: int | None = None, section: str | None = None) -> str:
    """Ссылка на фрагмент: страница для PDF, раздел для DOCX/MD/TXT."""
    doc = _clean(doc)
    if page is not None:
        return f"[{doc}, стр. {page}]"
    if section:
        section = _clean(section)
        if len(section) > _MAX_SECTION_LEN:
            section = section[: _MAX_SECTION_LEN - 1].rstrip() + "…"
        return f"[{doc}, раздел «{section}»]"
    return f"[{doc}]"


def _clean(text: str) -> str:
    return " ".join(text.replace("[", "(").replace("]", ")").split())


@dataclass(frozen=True)
class _Parsed:
    label: str
    file: str
    basename: str
    page: int | None
    section: str | None


@dataclass(frozen=True)
class CitationCheck:
    text: str  # ответ с исправленными ссылками (недопустимые удалены)
    valid: tuple[str, ...]  # допустимые ссылки в порядке первого упоминания
    invalid: tuple[str, ...]  # что модель написала в скобках, но это не наш источник


def check_citations(answer: str, allowed_labels: Sequence[str]) -> CitationCheck:
    allowed = [_parse_label(label) for label in allowed_labels]
    valid: list[str] = []
    invalid: list[str] = []

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        resolved: list[str] = []
        for part in inner.split(";"):
            labels = _resolve(part.strip(), allowed)
            if not labels:
                invalid.append(part.strip())
            for label in labels:
                if label not in resolved:
                    resolved.append(label)
        for label in resolved:
            if label not in valid:
                valid.append(label)
        return " ".join(resolved)

    text = _CITATION_RE.sub(replace, answer)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)  # пробел перед знаком после удалённой ссылки
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return CitationCheck(text=text, valid=tuple(valid), invalid=tuple(invalid))


def _parse_label(label: str) -> _Parsed:
    inner = label.strip()[1:-1] if label.startswith("[") and label.endswith("]") else label
    file, _, rest = inner.partition(",")
    rest = rest.strip()
    page = None
    section = None
    if rest.startswith("стр."):
        page = int(re.sub(r"\D", "", rest) or 0) or None
    elif rest.startswith("раздел"):
        section = _norm(rest[len("раздел") :])
    file_norm = _norm(file)
    return _Parsed(label=label, file=file_norm, basename=file_norm.rsplit("/", 1)[-1], page=page, section=section)


def _resolve(part: str, allowed: list[_Parsed]) -> list[str]:
    """Допустимые ссылки, которые имела в виду модель (пусто — ссылка недопустима)."""
    number = _FRAGMENT_NUMBER_RE.match(part)
    if number:
        index = int(number.group(1)) - 1
        return [allowed[index].label] if 0 <= index < len(allowed) else []

    file, _, rest = part.partition(",")
    file_norm = _norm(file)
    if not file_norm:
        return []
    candidates = [
        item for item in allowed if file_norm in (item.file, item.basename) or item.file.endswith("/" + file_norm)
    ]
    if not candidates:
        return []

    rest = rest.strip()
    if rest:
        digits = re.findall(r"\d+", rest)
        for item in candidates:
            if item.page is not None and digits and int(digits[0]) == item.page:
                return [item.label]
        rest_norm = _norm(re.sub(r"^(раздел|разд\.|глава|пункт)\s*", "", rest.lower()))
        for item in candidates:
            if item.section and rest_norm and (rest_norm in item.section or item.section in rest_norm):
                return [item.label]
        return []
    # Ссылка на файл без страницы/раздела: честно указываем все переданные фрагменты этого файла.
    labels: list[str] = []
    for item in candidates:
        if item.label not in labels:
            labels.append(item.label)
    return labels


def _norm(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'“”„]", "", text)
    return " ".join(text.split()).strip(" .")
