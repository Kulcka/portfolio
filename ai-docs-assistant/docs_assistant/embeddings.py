"""Кэш векторов фрагментов на диске и косинусная близость.

Векторы считаются только для новых фрагментов: ключ кэша — хеш текста,
файл кэша — свой для каждой модели эмбеддингов. При смене модели старый
кэш не используется (векторы разных моделей несравнимы).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Sequence
from pathlib import Path

from docs_assistant.chunking import Chunk
from docs_assistant.llm.base import EmbeddingProvider

logger = logging.getLogger(__name__)


def embedding_text(chunk: Chunk) -> str:
    return ". ".join(filter(None, (chunk.title, chunk.section, chunk.text)))


class EmbeddingCache:
    def __init__(self, index_dir: Path, provider: EmbeddingProvider) -> None:
        self.provider = provider
        model_hash = hashlib.sha256(provider.model_id.encode()).hexdigest()[:12]
        self.path = Path(index_dir) / f"embeddings-{model_hash}.json"

    def vectors_for(self, chunks: Sequence[Chunk]) -> list[list[float]]:
        """Векторы для всех фрагментов; недостающие запрашиваются у провайдера."""
        cache = self._load()
        keys = [_key(embedding_text(chunk)) for chunk in chunks]
        missing = [(key, embedding_text(chunk)) for key, chunk in zip(keys, chunks) if key not in cache]
        if missing:
            logger.info("Эмбеддинги: считаю %d новых фрагментов из %d", len(missing), len(chunks))
            unique = dict(missing)
            vectors = self.provider.embed_documents(list(unique.values()))
            cache.update(zip(unique.keys(), vectors))
        # Кэш хранит только актуальные фрагменты — удалённые документы не копятся.
        actual = {key: cache[key] for key in keys}
        if missing or len(actual) != len(cache):
            self._save(actual)
        return [actual[key] for key in keys]

    def _load(self) -> dict[str, list[float]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Кэш эмбеддингов %s повреждён — пересчитаю", self.path.name)
            return {}
        if data.get("model_id") != self.provider.model_id:
            return {}
        return {key: list(map(float, vector)) for key, vector in data.get("vectors", {}).items()}

    def _save(self, vectors: dict[str, list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"model_id": self.provider.model_id, "vectors": vectors}), encoding="utf-8")
        os.replace(tmp, self.path)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VectorIndex:
    """Нормированные векторы фрагментов для косинусной близости."""

    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        self._vectors = [_normalized(vector) for vector in vectors]

    def __len__(self) -> int:
        return len(self._vectors)

    def similarities(self, query: Sequence[float]) -> list[float]:
        q = _normalized(query)
        if self._vectors and len(q) != len(self._vectors[0]):
            raise ValueError("размерность вектора запроса не совпадает с индексом")
        return [sum(a * b for a, b in zip(q, vector)) for vector in self._vectors]


def _normalized(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)
