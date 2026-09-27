"""Скачивание картинок товаров.

Файл называется по ключу записи (артикул, UPC) — так картинку легко
сопоставить с товаром и загрузить на маркетплейс. Уже скачанные файлы не
скачиваются повторно. Путь к файлу (относительно папки выгрузки) пишется в
колонку ``images.column``.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import mimetypes
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from site_parser.config import ImagesSpec
from site_parser.fetcher import Fetcher, FetchError

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"}
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ImageStats:
    downloaded: int = 0
    existing: int = 0
    failed: int = 0


def safe_file_stem(key: str) -> str:
    """Имя файла из ключа записи: латиница, цифры, ``._-``; длинное — с хэшем."""
    cleaned = _UNSAFE_RE.sub("_", key).strip("._")
    if cleaned and cleaned == key and len(cleaned) <= 80:
        return cleaned
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned[:40]}_{digest}" if cleaned else digest


def _extension(url: str, content_type: str = "") -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return guessed or ".jpg"


def _urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def download_images(
    items: list[dict[str, Any]],
    spec: ImagesSpec,
    fetcher: Fetcher,
    output_dir: Path,
    key_of: Callable[[dict[str, Any]], str],
    *,
    limit: int | None = None,
) -> ImageStats:
    """Скачать картинки для записей. ``limit`` — сколько картинок обработать максимум."""
    stats = ImageStats()
    target_dir = output_dir / spec.dir
    handled = 0
    for item in items:
        urls = _urls(item.get(spec.field))
        if not urls:
            continue
        stem = safe_file_stem(key_of(item))
        files: list[str] = []
        for index, url in enumerate(urls, start=1):
            if limit is not None and handled >= limit:
                break
            handled += 1
            name_stem = stem if len(urls) == 1 else f"{stem}_{index}"
            existing = sorted(
                p for p in target_dir.glob(glob.escape(name_stem) + ".*") if p.suffix.lower() in IMAGE_EXTENSIONS
            )
            if existing:
                stats.existing += 1
                files.append(f"{spec.dir}/{existing[0].name}")
                continue
            try:
                page = fetcher.get_binary(url)
            except FetchError as exc:
                stats.failed += 1
                log.warning("Картинка не скачана: %s", exc)
                continue
            if not page.content_type.lower().startswith("image/"):
                stats.failed += 1
                log.warning("По адресу %s не картинка (%s) — пропуск", url, page.content_type or "тип не указан")
                continue
            path = target_dir / f"{name_stem}{_extension(url, page.content_type)}"
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".part")
                tmp.write_bytes(page.content)
                os.replace(tmp, path)
            except OSError as exc:
                stats.failed += 1
                log.warning("Не удалось сохранить картинку %s: %s", path, exc)
                continue
            stats.downloaded += 1
            files.append(f"{spec.dir}/{path.name}")
        if files:
            item[spec.column] = files if len(files) > 1 else files[0]
        if limit is not None and handled >= limit:
            break
    return stats
