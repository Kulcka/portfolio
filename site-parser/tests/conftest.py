"""Общие заготовки тестов: поддельные сеть, часы и страницы. Сеть в тестах не используется."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from site_parser.config import SiteConfig, load_config
from site_parser.redact import clear_secrets

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIGS = ROOT / "configs"

BOOKS = "https://books.toscrape.com"
QUOTES = "https://quotes.toscrape.com"


def fixture_bytes(relative: str) -> bytes:
    return (FIXTURES / relative).read_bytes()


@dataclass
class FakeResponse:
    status_code: int = 200
    content: bytes = b""
    headers: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    url: str = ""
    _json: Any = None

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


def html_response(body: bytes | str, *, status: int = 200, content_type: str = "text/html") -> FakeResponse:
    content = body.encode("utf-8") if isinstance(body, str) else body
    return FakeResponse(
        status_code=status, content=content, headers=CaseInsensitiveDict({"Content-Type": content_type})
    )


Route = FakeResponse | Exception | Callable[[str], FakeResponse] | list[FakeResponse | Exception]


class FakeSession:
    """Подмена ``requests.Session``: ответы по адресам, журнал запросов."""

    def __init__(self, routes: dict[str, Route] | None = None, default: Route | None = None) -> None:
        self.routes: dict[str, Route] = dict(routes or {})
        self.default = default
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.post_response: FakeResponse | Exception = FakeResponse(200, b'{"ok": true}')

    def _resolve(self, url: str) -> FakeResponse:
        route = self.routes.get(url, self.default)
        if route is None:
            return html_response("not found", status=404)
        if isinstance(route, list):
            if not route:
                raise AssertionError(f"ответы для {url} закончились")
            item = route.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(url)
        return route

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        response = self._resolve(url)
        if not response.url:
            response.url = url
        return response

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append((url, kwargs))
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response

    def close(self) -> None:
        pass

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


class FakeClock:
    """Часы и sleep без реального ожидания."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def books_product_page(url: str) -> FakeResponse:
    """Карточка книги из фикстуры с уникальным UPC и названием для каждого адреса."""
    html = fixture_bytes("books/product_a-light-in-the-attic.html").decode("utf-8")
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    slug = url.rstrip("/").split("/")[-2]
    html = html.replace("a897fe39b1053632", digest)
    html = re.sub(r"<h1>.*?</h1>", f"<h1>{slug}</h1>", html, count=1)
    return html_response(html)


def books_site_routes() -> dict[str, Route]:
    """Каталог из двух страниц: page-1 (есть «next») и последняя страница (фикстура page-50)."""
    return {
        f"{BOOKS}/robots.txt": html_response("not found", status=404),
        f"{BOOKS}/catalogue/page-1.html": html_response(fixture_bytes("books/catalogue_page-1.html")),
        f"{BOOKS}/catalogue/page-2.html": html_response(fixture_bytes("books/catalogue_page-50.html")),
    }


def books_session() -> FakeSession:
    def default(url: str) -> FakeResponse:
        if url.endswith("/index.html") and "/catalogue/" in url:
            return books_product_page(url)
        if url.startswith(f"{BOOKS}/media/"):
            return FakeResponse(200, b"\xff\xd8\xff\xe0fakejpeg", CaseInsensitiveDict({"Content-Type": "image/jpeg"}))
        return html_response("not found", status=404)

    return FakeSession(books_site_routes(), default=default)


@pytest.fixture
def books_config() -> SiteConfig:
    return load_config(CONFIGS / "books_toscrape.yaml")


@pytest.fixture
def quotes_config() -> SiteConfig:
    return load_config(CONFIGS / "quotes_toscrape.yaml")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Никаких настоящих секретов и сети в тестах."""
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_SHEET_ID"):
        monkeypatch.delenv(var, raising=False)

    def _no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("тест пытался выйти в сеть")

    monkeypatch.setattr(requests.Session, "request", _no_network)
    clear_secrets()
    yield
    clear_secrets()
