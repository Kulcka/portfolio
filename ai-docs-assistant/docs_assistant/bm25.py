"""Okapi BM25 без внешних зависимостей.

Отличия от учебной формулы:

* запрос задаётся группами взаимозаменяемых основ (слово + синонимы + близкие
  формы); вклад группы в оценку — максимум по её членам, чтобы синонимы
  не складывались и не раздували оценку;
* IDF в варианте Lucene — всегда положительный, даже для очень частых слов.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence


class BM25:
    def __init__(self, documents: Sequence[Sequence[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.n_docs = len(documents)
        self.doc_len = [len(doc) for doc in documents]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 1.0
        self.doc_terms: list[frozenset[str]] = []
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for index, doc in enumerate(documents):
            counts = Counter(doc)
            self.doc_terms.append(frozenset(counts))
            for term, count in counts.items():
                postings[term].append((index, count))
        self._postings = dict(postings)
        self._vocabulary = sorted(self._postings)
        self._vocabulary_set = frozenset(self._vocabulary)

    def __contains__(self, term: str) -> bool:
        return term in self._vocabulary_set

    def df(self, term: str) -> int:
        return len(self._postings.get(term, ()))

    def idf(self, term: str) -> float:
        return self._idf_from_df(self.df(term))

    def group_idf(self, terms: Iterable[str]) -> float:
        """IDF группы взаимозаменяемых основ: документ считается, если в нём есть любая из них."""
        docs: set[int] = set()
        for term in terms:
            docs.update(doc for doc, _ in self._postings.get(term, ()))
        return self._idf_from_df(len(docs))

    def _idf_from_df(self, df: int) -> float:
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def term_scores(self, term: str) -> dict[int, float]:
        postings = self._postings.get(term)
        if not postings:
            return {}
        idf = self.idf(term)
        k1, b, avgdl = self.k1, self.b, self.avgdl or 1.0
        scores = {}
        for doc, tf in postings:
            norm = k1 * (1.0 - b + b * self.doc_len[doc] / avgdl)
            scores[doc] = idf * tf * (k1 + 1.0) / (tf + norm)
        return scores

    def score_groups(self, groups: Iterable[Iterable[str | tuple[str, float]]]) -> dict[int, float]:
        """Оценки документов для запроса из групп основ (нулевые не возвращаются).

        Член группы — основа или пара (основа, множитель): так синонимы весят
        меньше точного слова из вопроса.
        """
        total: dict[int, float] = defaultdict(float)
        for group in groups:
            best: dict[int, float] = {}
            for member in group:
                term, factor = member if isinstance(member, tuple) else (member, 1.0)
                for doc, score in self.term_scores(term).items():
                    score *= factor
                    if score > best.get(doc, 0.0):
                        best[doc] = score
            for doc, score in best.items():
                total[doc] += score
        return dict(total)

    def prefix_variants(self, term: str, *, min_len: int = 4, max_tail: int = 2, max_diff: int = 3) -> set[str]:
        """Основы из словаря, отличающиеся от ``term`` только хвостом.

        Snowball режет русские слова неровно («доставка» → «доставк», «доставят» →
        «достав», «задержали» → «задержа», «задержка» → «задержк»). Основы считаются
        формами одного слова, если их общее начало не короче ``min_len`` букв и у
        каждой после него остаётся не больше ``max_tail`` букв. Короткие основы
        не расширяются — «цен» не должна находить «ценност».
        """
        if len(term) < min_len or term.isdigit():
            return set()
        shortest_common = max(min_len, len(term) - max_tail)
        prefix = term[:shortest_common]
        variants: set[str] = set()
        start = bisect.bisect_left(self._vocabulary, prefix)
        for candidate in self._vocabulary[start:]:
            if not candidate.startswith(prefix):
                break
            if abs(len(candidate) - len(term)) > max_diff:
                continue
            common = _common_prefix(candidate, term)
            if common >= max(min_len, max(len(candidate), len(term)) - max_tail):
                variants.add(candidate)
        variants.discard(term)
        return variants


def _common_prefix(a: str, b: str) -> int:
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return length
