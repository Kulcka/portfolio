"""Индекс документов на диске с частичной пересборкой.

Индекс — один JSON-файл ``index.json`` в папке ``INDEX_DIR``: список файлов
(хеш SHA-256, размер, время изменения) и их фрагменты вместе с основами слов.
При синхронизации заново читаются только новые и изменённые файлы; файл,
у которого совпали размер и время изменения, даже не хешируется. Запись
атомарная (временный файл + ``os.replace``), поэтому бот, читающий индекс,
никогда не увидит его наполовину записанным.

Если поменялись параметры разбивки или правила нормализации текста,
индекс пересобирается целиком — иначе старые и новые фрагменты были бы
несопоставимы.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from docs_assistant.chunking import Chunk, chunk_document
from docs_assistant.loaders import LOADER_VERSION, SUPPORTED_EXTENSIONS, DocumentLoadError, load_document
from docs_assistant.textproc import TOKENIZER_VERSION

logger = logging.getLogger(__name__)

INDEX_FORMAT_VERSION = 1
INDEX_FILENAME = "index.json"


class IndexBuildError(Exception):
    """Индекс нельзя построить (например, нет папки с документами)."""


@dataclass
class FileRecord:
    sha256: str
    size: int
    mtime_ns: int
    title: str
    chunks: list[Chunk]


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    total_files: int = 0
    total_chunks: int = 0
    full_rebuild: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def summary(self) -> str:
        lines = [
            f"Файлов в индексе: {self.total_files}, фрагментов: {self.total_chunks}.",
            f"Добавлено: {len(self.added)}, обновлено: {len(self.updated)}, "
            f"удалено: {len(self.removed)}, без изменений: {len(self.unchanged)}.",
        ]
        if self.full_rebuild:
            lines.append("Индекс пересобран целиком (новый индекс или изменились параметры).")
        for name, reason in sorted(self.failed.items()):
            lines.append(f"Не прочитан {name}: {reason}")
        return "\n".join(lines)


class IndexStore:
    def __init__(
        self,
        index_dir: Path,
        *,
        chunk_size: int = 800,
        chunk_overlap: int = 150,
        max_file_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.index_dir = Path(index_dir)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_file_bytes = max_file_bytes

    @property
    def path(self) -> Path:
        return self.index_dir / INDEX_FILENAME

    @property
    def params(self) -> dict[str, object]:
        return {
            "format": INDEX_FORMAT_VERSION,
            "tokenizer": TOKENIZER_VERSION,
            "loader": LOADER_VERSION,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
        }

    # --- чтение --------------------------------------------------------------

    def load_records(self) -> dict[str, FileRecord] | None:
        """Записи из индекса; ``None``, если индекса нет, он повреждён или устарел."""
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Индекс %s не читается (%s) — будет пересобран", self.path, type(exc).__name__)
            return None
        if data.get("params") != self.params:
            logger.info("Параметры индекса изменились — полная пересборка")
            return None
        records: dict[str, FileRecord] = {}
        for name, item in data.get("files", {}).items():
            records[name] = FileRecord(
                sha256=item["sha256"],
                size=int(item["size"]),
                mtime_ns=int(item["mtime_ns"]),
                title=item.get("title", ""),
                chunks=[Chunk.from_dict(chunk) for chunk in item.get("chunks", [])],
            )
        return records

    def load_chunks(self) -> list[Chunk]:
        records = self.load_records() or {}
        return [chunk for name in sorted(records) for chunk in records[name].chunks]

    # --- синхронизация -------------------------------------------------------

    def sync(self, docs_dir: Path) -> SyncReport:
        """Привести индекс в соответствие с папкой документов."""
        docs_dir = Path(docs_dir)
        if not docs_dir.is_dir():
            raise IndexBuildError(f"папка с документами не найдена: {docs_dir}")

        previous = self.load_records()
        report = SyncReport(full_rebuild=previous is None)
        previous = previous or {}
        current: dict[str, FileRecord] = {}
        touched = False  # файл пересохранили без изменений — запомним новое время

        for path in scan_documents(docs_dir):
            name = path.relative_to(docs_dir).as_posix()
            try:
                stat = path.stat()
            except OSError as exc:
                report.failed[name] = f"нет доступа к файлу ({type(exc).__name__})"
                continue
            if stat.st_size > self.max_file_bytes:
                limit_mb = self.max_file_bytes / 1024 / 1024
                report.failed[name] = f"файл больше {limit_mb:.0f} МБ"
                continue

            old = previous.get(name)
            if old and old.size == stat.st_size and old.mtime_ns == stat.st_mtime_ns:
                current[name] = old
                report.unchanged.append(name)
                continue

            try:
                digest = _sha256(path)
            except OSError as exc:
                report.failed[name] = f"нет доступа к файлу ({type(exc).__name__})"
                continue
            if old and old.sha256 == digest:
                old.size, old.mtime_ns = stat.st_size, stat.st_mtime_ns
                touched = True
                current[name] = old
                report.unchanged.append(name)
                continue

            try:
                document = load_document(path)
            except DocumentLoadError as exc:
                report.failed[name] = str(exc)
                logger.warning("Файл %s пропущен: %s", name, exc)
                continue
            # В id входит и путь: одинаковые копии файла в разных папках не должны совпасть.
            id_prefix = hashlib.sha256(f"{name}\n{digest}".encode()).hexdigest()[:12]
            chunks = chunk_document(
                name, document, size=self.chunk_size, overlap=self.chunk_overlap, id_prefix=id_prefix
            )
            current[name] = FileRecord(
                sha256=digest, size=stat.st_size, mtime_ns=stat.st_mtime_ns, title=document.title, chunks=chunks
            )
            (report.updated if old else report.added).append(name)

        report.removed = sorted(set(previous) - set(current))
        report.total_files = len(current)
        report.total_chunks = sum(len(record.chunks) for record in current.values())
        if report.changed or report.full_rebuild or touched or not self.path.is_file():
            self._write(current)
        return report

    def _write(self, records: dict[str, FileRecord]) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "params": self.params,
            "files": {
                name: {
                    "sha256": record.sha256,
                    "size": record.size,
                    "mtime_ns": record.mtime_ns,
                    "title": record.title,
                    "chunks": [chunk.to_dict() for chunk in record.chunks],
                }
                for name, record in sorted(records.items())
            },
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


def scan_documents(docs_dir: Path) -> list[Path]:
    """Поддерживаемые файлы папки (рекурсивно), без скрытых и временных файлов Word."""
    result = []
    for path in docs_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        relative = path.relative_to(docs_dir).parts
        if any(part.startswith(".") for part in relative) or path.name.startswith("~$"):
            continue
        result.append(path)
    return sorted(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
