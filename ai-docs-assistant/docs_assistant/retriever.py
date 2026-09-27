"""Поиск фрагментов: BM25 и, по желанию, эмбеддинги с гибридным ранжированием.

Порядок работы:

1. Вопрос → основы слов; каждая основа расширяется синонимами и близкими
   формами из словаря индекса (группа взаимозаменяемых основ).
2. BM25 по группам. Если подключены эмбеддинги — ещё ранжирование по
   косинусной близости, и два списка сливаются методом Reciprocal Rank
   Fusion (RRF): ему не нужно приводить разнородные оценки к одной шкале.
3. Решение «в документах есть ответ» — по *полноте совпадения*: доля
   веса (IDF) слов вопроса, найденных в лучшем фрагменте. Вопрос «как
   приготовить борщ» не совпадёт почти ни одним значимым словом — и модель
   даже не будет вызвана. Порог ``MIN_COVERAGE`` подбирается замером
   (``python -m docs_assistant eval --tune``), а не назначается.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from docs_assistant.bm25 import BM25
from docs_assistant.chunking import Chunk
from docs_assistant.embeddings import VectorIndex
from docs_assistant.llm.base import EmbeddingProvider, LLMError
from docs_assistant.textproc import Synonyms, tokenize

logger = logging.getLogger(__name__)

# Числа в вопросе («посылка 3 кг») часто не встречаются в документах буквально
# (там «до 5 кг»), поэтому их вес в полноте совпадения уменьшен вдвое.
NUMBER_WEIGHT = 0.5
# Нижняя граница множителя полноты: фрагмент с частичным совпадением не обнуляется.
COVERAGE_FLOOR = 0.25
# Множитель для совпадения по синониму (а не по самому слову вопроса). Замер 27.09.2026
# на eval/questions.json: 0.5–0.85 на dev ничего не дали (hit@1 8/10 при любом), на test
# отняли один вопрос («коробка разбита» → «повреждение»). Поэтому по умолчанию 1.0.
SYNONYM_FACTOR = 1.0


@dataclass(frozen=True)
class QueryTerm:
    term: str
    weight: float
    variants: frozenset[str]  # формы слова и синонимы
    exact: frozenset[str] = frozenset()  # только формы самого слова

    def scored_variants(self) -> list[tuple[str, float]]:
        return [(v, 1.0 if v in self.exact else SYNONYM_FACTOR) for v in sorted(self.variants)]


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float
    bm25: float
    coverage: float
    similarity: float | None = None


@dataclass(frozen=True)
class SearchResult:
    query: str
    hits: tuple[SearchHit, ...]
    relevant: bool
    best_coverage: float
    best_similarity: float | None
    reason: str


class Retriever:
    def __init__(
        self,
        chunks: Sequence[Chunk],
        *,
        synonyms: Synonyms | None = None,
        min_coverage: float = 0.5,
        vectors: VectorIndex | None = None,
        embedder: EmbeddingProvider | None = None,
        min_similarity: float | None = None,
        rrf_k: int = 60,
        candidates: int = 50,
    ) -> None:
        if vectors is not None and len(vectors) != len(chunks):
            raise ValueError("число векторов не совпадает с числом фрагментов")
        self.chunks = list(chunks)
        self.synonyms = synonyms or Synonyms()
        self.min_coverage = min_coverage
        self.vectors = vectors
        self.embedder = embedder
        self.min_similarity = min_similarity
        self.rrf_k = rrf_k
        self.candidates = candidates
        self.bm25 = BM25([chunk.tokens for chunk in self.chunks])

    @property
    def hybrid(self) -> bool:
        return self.vectors is not None and self.embedder is not None

    def query_terms(self, query: str) -> list[QueryTerm]:
        terms: list[QueryTerm] = []
        seen: set[frozenset[str]] = set()
        for term in dict.fromkeys(tokenize(query)):
            variants = set(self.synonyms.expand(term)) | {term}
            for member in list(variants):
                variants |= self.bm25.prefix_variants(member)
            key = frozenset(variants)
            if key in seen:
                continue
            seen.add(key)
            # Вес — по всей группе: «стоит» даёт основу «сто», которой в документах нет,
            # но её синонимы («стоимост», «тариф») частые — значит, слово не редкое.
            weight = self.bm25.group_idf(key) * (NUMBER_WEIGHT if term.isdigit() else 1.0)
            exact = frozenset({term} | self.bm25.prefix_variants(term))
            terms.append(QueryTerm(term=term, weight=weight, variants=key, exact=exact))
        return terms

    def coverage(self, doc_index: int, terms: Sequence[QueryTerm]) -> float:
        total = sum(term.weight for term in terms)
        if not total:
            return 0.0
        doc_terms = self.bm25.doc_terms[doc_index]
        matched = sum(term.weight for term in terms if term.variants & doc_terms)
        return matched / total

    def search(self, query: str, k: int = 4) -> SearchResult:
        terms = self.query_terms(query)
        bm25_scores = self.bm25.score_groups(term.scored_variants() for term in terms) if terms else {}
        coverage = {doc: self.coverage(doc, terms) for doc in bm25_scores}
        # BM25 любит короткие фрагменты, где одно слово запроса встречается густо.
        # Умножение на полноту совпадения поднимает фрагменты, где есть все понятия вопроса.
        adjusted = {doc: score * (COVERAGE_FLOOR + coverage[doc]) for doc, score in bm25_scores.items()}
        bm25_ranked = sorted(adjusted.items(), key=lambda item: (-item[1], item[0]))[: self.candidates]

        similarities = self._similarities(query)
        if similarities is not None:
            fused: dict[int, float] = defaultdict(float)
            for rank, (doc, _) in enumerate(bm25_ranked):
                fused[doc] += 1.0 / (self.rrf_k + rank + 1)
            by_similarity = sorted(range(len(similarities)), key=lambda i: (-similarities[i], i))
            for rank, doc in enumerate(by_similarity[: self.candidates]):
                fused[doc] += 1.0 / (self.rrf_k + rank + 1)
            ranked = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        else:
            ranked = bm25_ranked

        hits = tuple(
            SearchHit(
                chunk=self.chunks[doc],
                score=score,
                bm25=bm25_scores.get(doc, 0.0),
                coverage=coverage[doc] if doc in coverage else self.coverage(doc, terms),
                similarity=similarities[doc] if similarities is not None else None,
            )
            for doc, score in ranked[:k]
        )
        best_coverage = max((hit.coverage for hit in hits), default=0.0)
        sims = [hit.similarity for hit in hits if hit.similarity is not None]
        best_similarity = max(sims) if sims else None
        relevant, reason = self._decide(terms, hits, best_coverage, best_similarity)
        return SearchResult(
            query=query,
            hits=hits,
            relevant=relevant,
            best_coverage=best_coverage,
            best_similarity=best_similarity,
            reason=reason,
        )

    def _similarities(self, query: str) -> list[float] | None:
        if not self.hybrid:
            return None
        assert self.embedder is not None and self.vectors is not None
        try:
            return self.vectors.similarities(self.embedder.embed_query(query))
        except (LLMError, ValueError) as exc:
            logger.warning("Эмбеддинги недоступны (%s) — ищу только по BM25", exc)
            return None

    def _decide(
        self,
        terms: Sequence[QueryTerm],
        hits: Sequence[SearchHit],
        best_coverage: float,
        best_similarity: float | None,
    ) -> tuple[bool, str]:
        if not hits:
            return False, "ничего не найдено"
        if not terms and best_similarity is None:
            return False, "в вопросе нет значимых слов"
        if best_coverage >= self.min_coverage:
            return True, f"совпадение {best_coverage:.2f} ≥ порога {self.min_coverage:.2f}"
        if (
            self.min_similarity is not None
            and best_similarity is not None
            and best_similarity >= self.min_similarity
        ):
            return True, f"близость {best_similarity:.2f} ≥ порога {self.min_similarity:.2f}"
        return False, f"совпадение {best_coverage:.2f} < порога {self.min_coverage:.2f}"
