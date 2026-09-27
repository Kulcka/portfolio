"""Обход сайта: страницы каталога, пагинация, переход в карточки.

Режимы:

* **каталог** (есть секция ``list``): со стартовой страницы собираются
  карточки по ``list.item``, из каждой — поля ``list.fields``; если задана
  секция ``detail``, парсер заходит по ``list.link`` в карточку товара и
  добавляет её поля; затем переход на следующую страницу (``pagination``);
* **карточки по списку ссылок** (только ``detail``): каждый стартовый адрес
  (или строка из ``start_urls_file``) — страница товара.

Сбой одной карточки не останавливает прогон: запись остаётся с полями из
списка, ошибка попадает в итог. Сбой страницы каталога останавливает обход
этого каталога (дальше идти не по чему) — прогон помечается неполным.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from site_parser.config import URL_COLUMN, Column, SiteConfig
from site_parser.diff import item_key
from site_parser.extract import extract_fields, extract_specs, missing_required, select_link
from site_parser.fetcher import Fetcher, FetchError, NotFound

log = logging.getLogger(__name__)

Item = dict[str, Any]


@dataclass
class ScrapeStats:
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    finished_at: datetime | None = None
    list_pages: int = 0
    detail_pages: int = 0
    failed_pages: int = 0
    duplicates: int = 0
    skipped: int = 0
    images: int = 0
    images_failed: int = 0

    @property
    def pages(self) -> int:
        return self.list_pages + self.detail_pages

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now().astimezone()
        return (end - self.started_at).total_seconds()


@dataclass
class ScrapeResult:
    items: list[Item] = field(default_factory=list)
    stats: ScrapeStats = field(default_factory=ScrapeStats)
    errors: list[str] = field(default_factory=list)
    limit_reached: bool = False

    @property
    def complete(self) -> bool:
        """Прогон прошёл без ошибок страниц (данные можно считать полными)."""
        return not self.errors

    def add_error(self, message: str) -> None:
        log.error(message)
        self.errors.append(message)


class Scraper:
    """Собирает записи с сайта по конфигу."""

    def __init__(
        self,
        config: SiteConfig,
        fetcher: Fetcher,
        *,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> None:
        self.config = config
        self.fetcher = fetcher
        pagination = config.pagination
        self.max_pages = max_pages if max_pages is not None else (pagination.max_pages if pagination else None)
        self.max_items = max_items if max_items is not None else config.max_items

    # ------------------------------------------------------------ разбор HTML

    def parse_list_page(self, soup: BeautifulSoup | Tag, page_url: str) -> list[Item]:
        """Карточки со страницы каталога; ссылка на карточку — в ``url``."""
        spec = self.config.listing
        if spec is None:
            return []
        items: list[Item] = []
        for card in soup.select(spec.item):
            item = extract_fields(card, spec.fields, page_url)
            link = select_link(card, spec.link, page_url) if spec.link else None
            item[URL_COLUMN] = link
            items.append(item)
        return items

    def parse_detail_page(self, soup: BeautifulSoup | Tag, page_url: str) -> Item:
        """Поля карточки товара и таблица характеристик."""
        spec = self.config.detail
        if spec is None:
            return {}
        item = extract_fields(soup, spec.fields, page_url)
        if spec.specs is not None:
            for key, value in extract_specs(soup, spec.specs).items():
                item.setdefault(key, value)  # явно описанное поле важнее
        return item

    def next_page_url(self, soup: BeautifulSoup | Tag, page_url: str, page_no: int) -> str | None:
        """Адрес следующей страницы каталога или ``None``, если это последняя."""
        pagination = self.config.pagination
        if pagination is None:
            return None
        if pagination.template:
            return pagination.template.format(page=page_no + 1)
        if pagination.next:
            return select_link(soup, pagination.next, page_url)
        return None

    # ------------------------------------------------------------------ обход

    def run(self) -> ScrapeResult:
        result = ScrapeResult()
        seen: set[str] = set()
        try:
            for start_url in self.config.start_urls:
                if self._limit_reached(result):
                    break
                if self.config.listing is not None:
                    self._crawl_listing(start_url, result, seen)
                else:
                    self._scrape_detail_only(start_url, result, seen)
        finally:
            result.stats.finished_at = datetime.now().astimezone()
        return result

    def _limit_reached(self, result: ScrapeResult) -> bool:
        if self.max_items is not None and len(result.items) >= self.max_items:
            if not result.limit_reached:
                log.info("Достигнут лимит записей: %d", self.max_items)
            result.limit_reached = True
            return True
        return False

    def _crawl_listing(self, start_url: str, result: ScrapeResult, seen: set[str]) -> None:
        pagination = self.config.pagination
        template_mode = bool(pagination and pagination.template)
        page_no = pagination.start if pagination else 1
        url: str | None = start_url
        visited: set[str] = set()
        pages_done = 0

        while url:
            if url in visited:
                log.warning("Пагинация зациклилась на %s — остановка", url)
                break
            if self.max_pages is not None and pages_done >= self.max_pages:
                log.info("Достигнут лимит страниц каталога: %d", self.max_pages)
                break
            visited.add(url)
            try:
                page = self.fetcher.get(url)
            except NotFound:
                if template_mode and pages_done > 0:
                    log.info("Страница %s не найдена — каталог закончился", url)
                else:
                    result.stats.failed_pages += 1
                    result.add_error(f"страница каталога не найдена (404): {url}")
                break
            except FetchError as exc:
                result.stats.failed_pages += 1
                result.add_error(f"страница каталога не загружена: {exc}")
                break

            pages_done += 1
            result.stats.list_pages += 1
            soup = page.soup()
            cards = self.parse_list_page(soup, page.url)
            log.info("Страница каталога %d: %s — карточек: %d", pages_done, page.url, len(cards))
            if not cards:
                if pages_done == 1:
                    result.add_error(
                        f"на странице {page.url} нет карточек по селектору list.item — "
                        "проверьте селектор или доступность сайта"
                    )
                break

            for item in cards:
                if self._limit_reached(result):
                    return
                self._complete_item(item, page.url, result)
                self._accept(item, result, seen)
            url = self.next_page_url(soup, page.url, page_no)
            page_no += 1

    def _complete_item(self, item: Item, page_url: str, result: ScrapeResult) -> None:
        """Дозаполнить запись полями из карточки товара."""
        link = item.get(URL_COLUMN)
        if self.config.detail is not None and link:
            detail = self._fetch_detail(link, result)
            for key, value in detail.items():
                if value is not None and value != "" and value != []:
                    item[key] = value
                else:
                    item.setdefault(key, value)
        if not item.get(URL_COLUMN):
            item[URL_COLUMN] = page_url

    def _fetch_detail(self, url: str, result: ScrapeResult) -> Item:
        try:
            page = self.fetcher.get(url)
        except FetchError as exc:
            result.stats.failed_pages += 1
            result.add_error(f"карточка не загружена: {exc}")
            return {}
        result.stats.detail_pages += 1
        return self.parse_detail_page(page.soup(), page.url)

    def _scrape_detail_only(self, url: str, result: ScrapeResult, seen: set[str]) -> None:
        try:
            page = self.fetcher.get(url)
        except FetchError as exc:
            result.stats.failed_pages += 1
            result.add_error(f"карточка не загружена: {exc}")
            return
        result.stats.detail_pages += 1
        item = self.parse_detail_page(page.soup(), page.url)
        item[URL_COLUMN] = page.url
        self._accept(item, result, seen)

    def _accept(self, item: Item, result: ScrapeResult, seen: set[str]) -> None:
        missing = missing_required(item, self.config.all_fields())
        if missing:
            result.stats.skipped += 1
            log.warning(
                "Запись пропущена — пустые обязательные поля %s: %s",
                ", ".join(missing),
                item.get(URL_COLUMN),
            )
            return
        key = item_key(item, self.config.changes.key)
        if key in seen:
            result.stats.duplicates += 1
            log.debug("Повтор записи %s — пропущена", key)
            return
        seen.add(key)
        result.items.append(item)
        if len(result.items) % 20 == 0:
            log.info("Собрано записей: %d", len(result.items))


def build_columns(config: SiteConfig, items: list[Item]) -> list[Column]:
    """Колонки выгрузки: поля конфига, затем характеристики, затем ссылка и картинка.

    Характеристики (``detail.specs``) у разных товаров разные, поэтому их
    колонки собираются по факту — в порядке первого появления.
    """
    static = config.static_columns()
    tail_names = {URL_COLUMN, config.images.column if config.images else None}
    head = [c for c in static if c.name not in tail_names]
    tail = [c for c in static if c.name in tail_names]
    known = {c.name for c in static}
    dynamic: list[Column] = []
    for item in items:
        for key in item:
            if key not in known and not key.startswith("_"):
                known.add(key)
                dynamic.append(Column(key, key, "str"))
    return head + dynamic + tail


def fill_stats(items: list[Item], columns: list[Column]) -> list[tuple[Column, int]]:
    """Сколько записей имеют непустое значение в каждой колонке."""
    stats: list[tuple[Column, int]] = []
    for column in columns:
        filled = sum(1 for item in items if item.get(column.name) not in (None, "", []))
        stats.append((column, filled))
    return stats
