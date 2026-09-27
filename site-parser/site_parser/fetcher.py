"""Сетевой слой: вежливые запросы с повторами, паузами, robots.txt и кэшем.

* Свой User-Agent и заголовки из конфига.
* Пауза между запросами к одному хосту (``request.delay``, либо больше, если
  так просит ``Crawl-delay`` в robots.txt).
* Повторы при сетевых ошибках, 429 и 5xx с экспоненциальной паузой
  (``backoff * 2**попытка``, не больше ``backoff_max``); ``Retry-After``
  сервера уважается.
* robots.txt проверяется до запроса (по умолчанию включено).
* Кэш страниц на диске — для отладки селекторов без повторной нагрузки на сайт.

Кодировка: если сервер не указал charset в ``Content-Type``, страница
декодируется по ``<meta charset>`` (так делает BeautifulSoup), а не по
ISO-8859-1, как по умолчанию ``requests`` — иначе «£» превращается в «Â£».
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from site_parser.config import RequestSettings
from site_parser.robots import RobotsPolicy

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524})
NOT_FOUND_STATUSES = frozenset({404, 410})
MAX_BODY_BYTES = 30 * 1024 * 1024

_CHARSET_RE = re.compile(r"charset=[\"']?([\w.:-]+)", re.IGNORECASE)


class FetchError(Exception):
    """Страницу получить не удалось."""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason
        self.status = status


class NotFound(FetchError):
    """Сервер ответил 404/410 — страницы нет."""


class RobotsDisallowed(FetchError):
    """Адрес запрещён в robots.txt."""


@dataclass
class Page:
    """Полученная страница (или файл)."""

    url: str
    status: int
    content: bytes
    encoding: str | None = None
    content_type: str = ""
    from_cache: bool = False

    def soup(self) -> BeautifulSoup:
        return BeautifulSoup(self.content, "lxml", from_encoding=self.encoding)

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    cache_hits: int = 0
    bytes: int = 0
    robots_blocked: int = 0
    by_status: dict[int, int] = field(default_factory=dict)


def charset_from_content_type(content_type: str) -> str | None:
    match = _CHARSET_RE.search(content_type or "")
    return match.group(1).lower() if match else None


class PageCache:
    """Кэш страниц на диске: ``<sha256(url)>.bin`` + ``.json`` с метаданными."""

    def __init__(self, directory: Path, ttl_hours: float | None = None) -> None:
        self.directory = directory
        self.ttl_seconds = ttl_hours * 3600 if ttl_hours else None

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.bin", self.directory / f"{digest}.json"

    def get(self, url: str) -> Page | None:
        body_path, meta_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if self.ttl_seconds is not None and time.time() - meta["saved_at"] > self.ttl_seconds:
                return None
            content = body_path.read_bytes()
        except (OSError, ValueError, KeyError):
            return None
        return Page(
            url=meta.get("final_url", url),
            status=int(meta.get("status", 200)),
            content=content,
            encoding=meta.get("encoding"),
            content_type=meta.get("content_type", ""),
            from_cache=True,
        )

    def put(self, url: str, page: Page) -> None:
        body_path, meta_path = self._paths(url)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(page.content)
            meta = {
                "url": url,
                "final_url": page.url,
                "status": page.status,
                "encoding": page.encoding,
                "content_type": page.content_type,
                "saved_at": time.time(),
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("не удалось сохранить страницу в кэш: %s", exc)


class Fetcher:
    """HTTP-клиент парсера. Все запросы к сайту идут только через него."""

    def __init__(
        self,
        settings: RequestSettings,
        *,
        session: Any | None = None,
        cache: PageCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self.cache = cache
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request: dict[str, float] = {}
        self.stats = FetchStats()
        self.robots: RobotsPolicy | None = (
            RobotsPolicy(settings.user_agent, self._fetch_robots) if settings.respect_robots else None
        )
        self._headers = {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            **settings.headers,
        }

    @classmethod
    def from_settings(cls, settings: RequestSettings, *, use_cache: bool | None = None) -> Fetcher:
        enabled = settings.cache if use_cache is None else use_cache
        cache = PageCache(Path(settings.cache_dir), settings.cache_ttl_hours) if enabled else None
        return cls(settings, cache=cache)

    # ------------------------------------------------------------------ public

    def get(self, url: str) -> Page:
        """Получить HTML-страницу. Бросает ``FetchError`` и наследников."""
        if self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                self.stats.cache_hits += 1
                log.debug("из кэша: %s", url)
                return cached
        page = self._get(url)
        if self.cache is not None:
            self.cache.put(url, page)
        return page

    def get_binary(self, url: str, *, accept: str = "image/*,*/*;q=0.5") -> Page:
        """Получить файл (картинку). Кэш страниц не используется."""
        return self._get(url, accept=accept)

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    # ----------------------------------------------------------------- private

    def _get(self, url: str, *, accept: str | None = None) -> Page:
        if self.robots is not None and not self.robots.can_fetch(url):
            self.stats.robots_blocked += 1
            raise RobotsDisallowed(url, "адрес запрещён в robots.txt")
        response = self._request(url, accept=accept)
        status = response.status_code
        if status in NOT_FOUND_STATUSES:
            raise NotFound(url, f"страницы нет (HTTP {status})", status)
        if status >= 400:
            raise FetchError(url, f"сервер ответил HTTP {status}", status)
        content = response.content or b""
        content_type = response.headers.get("Content-Type", "")
        return Page(
            url=str(response.url or url),
            status=status,
            content=content,
            encoding=charset_from_content_type(content_type),
            content_type=content_type,
        )

    def _fetch_robots(self, url: str) -> tuple[int, str]:
        response = self._request(url)
        content_type = response.headers.get("Content-Type", "")
        encoding = charset_from_content_type(content_type) or "utf-8"
        return response.status_code, (response.content or b"").decode(encoding, errors="replace")

    def _delay_for(self, url: str) -> float:
        delay = self.settings.delay
        if self.robots is not None and urlsplit(url).path != "/robots.txt":
            crawl_delay = self.robots.crawl_delay(url)
            if crawl_delay is not None and crawl_delay > delay:
                delay = crawl_delay
        if self.settings.jitter:
            delay += self._rng.uniform(0, self.settings.jitter * delay)
        return delay

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_request.get(host)
        if last is not None:
            wait = self._delay_for(url) - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _backoff(self, attempt: int, response: Any | None) -> float:
        pause = min(self.settings.backoff * (2**attempt), self.settings.backoff_max)
        retry_after = _retry_after_seconds(response) if response is not None else None
        if retry_after is not None:
            pause = max(pause, min(retry_after, self.settings.backoff_max))
        return pause

    def _request(self, url: str, *, accept: str | None = None) -> Any:
        """GET с повторами. Возвращает ответ с любым итоговым статусом."""
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        attempts = self.settings.retries + 1
        for attempt in range(attempts):
            self._throttle(url)
            self.stats.requests += 1
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.settings.timeout,
                    verify=self.settings.verify_ssl,
                    allow_redirects=True,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                reason = _short_reason(exc)
                if attempt + 1 >= attempts:
                    raise FetchError(url, f"сеть: {reason} (попыток: {attempts})") from None
                pause = self._backoff(attempt, None)
                log.warning("%s: %s — повтор через %.1f с", url, reason, pause)
                self.stats.retries += 1
                self._sleep(pause)
                continue
            except requests.RequestException as exc:
                raise FetchError(url, f"ошибка запроса: {_short_reason(exc)}") from None

            status = int(response.status_code)
            self.stats.by_status[status] = self.stats.by_status.get(status, 0) + 1
            size = len(response.content or b"")
            if size > MAX_BODY_BYTES:
                raise FetchError(url, f"слишком большой ответ ({size} байт)", status)
            self.stats.bytes += size
            if status in RETRY_STATUSES and attempt + 1 < attempts:
                pause = self._backoff(attempt, response)
                log.warning("%s: HTTP %s — повтор через %.1f с", url, status, pause)
                self.stats.retries += 1
                self._sleep(pause)
                continue
            log.debug("GET %s -> %s (%d байт)", url, status, size)
            return response
        raise AssertionError("unreachable")  # pragma: no cover


def _short_reason(exc: BaseException) -> str:
    """Короткое описание сетевой ошибки без длинных цепочек urllib3."""
    if isinstance(exc, requests.Timeout):
        return "таймаут"
    if isinstance(exc, requests.ConnectionError):
        return "нет соединения"
    return type(exc).__name__


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After")
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    return max(0.0, moment.timestamp() - time.time())
