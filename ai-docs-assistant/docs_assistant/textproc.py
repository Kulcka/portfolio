"""Нормализация русского и английского текста для поиска.

Шаги: нижний регистр, «ё» → «е», разбиение на слова и числа, удаление
стоп-слов, стемминг Snowball (русский для кириллицы, английский для латиницы).
Числа сохраняются как есть — в тарифах и регламентах они часто и есть ответ.

Словарь синонимов (:class:`Synonyms`) расширяет запрос: «цена» находит
«стоимость» и «тариф». Базовый словарь лежит в ``resources/synonyms_ru.txt``,
заказчик может добавить свой файл (переменная ``SYNONYMS_FILE``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from importlib import resources
from pathlib import Path

import snowballstemmer

# Меняется при любом изменении правил нормализации: индекс с другой версией
# пересобирается целиком, иначе старые основы слов не совпадут с новыми.
TOKENIZER_VERSION = "ru-en-snowball-2"

_WORD_RE = re.compile(r"[a-zа-я0-9]+")

STOPWORDS_RU = frozenset(
    """
    а без более бы был была были было быть в вам вас весь во вот все всего всех вы
    где да даже для до его ее ей ему если есть еще же за здесь и из или им их к
    как какая какие каким какими каких какого какое какой каком кем ко когда кто
    ли либо между меня мне много может можно мой моя мы на над надо нам нас не
    него нее нет ни них но ну о об однако он она они оно от очень по под при про
    с со так также такой там те тем то того тоже той только том ты у уже хотя
    чего чей чем что чтобы чье чья эта эти это этого этой этом этот эту я
    сколько скольких насколько какова каков каковы ваш ваша ваше ваши вашей вашего
    вашем вашим вашу наш наша наше наши свой своя свое свои пожалуйста подскажите
    скажите хочу хотел хотела нужно нужен нужна будет будут если
    который которая которое которые которого которой котором которым которую
    куда откуда почему зачем сейчас тогда потом ли либо разве неужели мой моя мое
    мои моего моей моем моим мою вообще именно ещё еще уже
    """.split()
)

STOPWORDS_EN = frozenset(
    """
    a an and are as at be by for from how i in is it of on or that the this to
    was what when where which who why will with you your do does can
    """.split()
)

STOPWORDS = STOPWORDS_RU | STOPWORDS_EN

_RU_STEMMER = snowballstemmer.stemmer("russian")
_EN_STEMMER = snowballstemmer.stemmer("english")


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


@lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    """Основа слова. Слово уже в нижнем регистре."""
    if word.isdigit():
        return word
    if any("а" <= ch <= "я" for ch in word):
        return _RU_STEMMER.stemWord(word)
    return _EN_STEMMER.stemWord(word)


def words(text: str) -> list[str]:
    """Слова и числа в нижнем регистре, без стоп-слов и однобуквенных слов."""
    return [
        w
        for w in _WORD_RE.findall(normalize(text))
        if w not in STOPWORDS and (len(w) > 1 or w.isdigit())
    ]


def tokenize(text: str) -> list[str]:
    """Основы слов для индекса BM25."""
    return [stem(w) for w in words(text)]


class Synonyms:
    """Группы взаимозаменяемых слов, хранятся как множества основ."""

    def __init__(self, groups: Iterable[Iterable[str]] = ()) -> None:
        self._by_term: dict[str, frozenset[str]] = {}
        for group in groups:
            self.add_group(group)

    def add_group(self, entries: Iterable[str]) -> None:
        stems: set[str] = set()
        for entry in entries:
            entry_stems = tokenize(entry)
            # Словосочетания пропускаем: их части («лицо», «служба») слишком общие.
            if len(entry_stems) == 1:
                stems.add(entry_stems[0])
        if len(stems) < 2:
            return
        # Если слово уже входит в другую группу — объединяем группы.
        merged = set(stems)
        for term in stems:
            merged |= self._by_term.get(term, frozenset())
        frozen = frozenset(merged)
        for term in frozen:
            self._by_term[term] = frozen

    def expand(self, term: str) -> frozenset[str]:
        return self._by_term.get(term, frozenset({term}))

    def __len__(self) -> int:
        return len({id(group) for group in self._by_term.values()})

    @classmethod
    def parse(cls, text: str) -> Synonyms:
        """Формат: одна группа на строку, слова через запятую; ``#`` — комментарий."""
        groups = []
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                groups.append([part.strip() for part in line.split(",") if part.strip()])
        return cls(groups)

    @classmethod
    def load(cls, extra_files: Sequence[Path] = ()) -> Synonyms:
        """Базовый словарь из пакета плюс файлы заказчика."""
        text = resources.files("docs_assistant").joinpath("resources/synonyms_ru.txt").read_text(
            encoding="utf-8"
        )
        for path in extra_files:
            text += "\n" + Path(path).read_text(encoding="utf-8-sig")
        return cls.parse(text)
