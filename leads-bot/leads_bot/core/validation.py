"""Проверка имени и комментария."""

from __future__ import annotations

import re

NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 60
COMMENT_MAX_LENGTH = 1000

_NAME_EXTRA_CHARS = frozenset(" -'’.")
_SPACES = re.compile(r"\s+")


class NameValidationError(ValueError):
    pass


class CommentTooLongError(ValueError):
    pass


def normalize_name(raw: str) -> str:
    """Имя: буквы любого алфавита, пробел, дефис, апостроф, точка; 2–60 символов."""
    name = _SPACES.sub(" ", raw).strip()
    if not NAME_MIN_LENGTH <= len(name) <= NAME_MAX_LENGTH:
        raise NameValidationError(f"длина имени от {NAME_MIN_LENGTH} до {NAME_MAX_LENGTH} символов")
    if not all(ch.isalpha() or ch in _NAME_EXTRA_CHARS for ch in name):
        raise NameValidationError("в имени допустимы только буквы, пробел и дефис")
    if sum(ch.isalpha() for ch in name) < NAME_MIN_LENGTH:
        raise NameValidationError("в имени слишком мало букв")
    return name


def normalize_comment(raw: str) -> str:
    """Комментарий: обрезать пробелы по краям, не длиннее 1000 символов."""
    comment = raw.strip()
    if len(comment) > COMMENT_MAX_LENGTH:
        raise CommentTooLongError(f"комментарий длиннее {COMMENT_MAX_LENGTH} символов")
    return comment
