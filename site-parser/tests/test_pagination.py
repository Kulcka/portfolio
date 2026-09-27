"""Пагинация: ссылка «next», шаблон номера страницы, лимиты, защита от зацикливания."""

from __future__ import annotations

import dataclasses

from site_parser.config import PaginationSpec, SiteConfig
from site_parser.fetcher import Fetcher, Page
from site_parser.scraper import Scraper
from tests.conftest import BOOKS, QUOTES, FakeClock, FakeSession, books_session, fixture_bytes, html_response


def _fetcher(config: SiteConfig, session: FakeSession, clock: FakeClock) -> Fetcher:
    return Fetcher(config.request, session=session, sleep=clock.sleep, clock=clock.time)


def test_next_link_on_first_and_last_page(books_config: SiteConfig) -> None:
    scraper = Scraper(books_config, fetcher=None)  # type: ignore[arg-type]
    first_url = f"{BOOKS}/catalogue/page-1.html"
    first = Page(first_url, 200, fixture_bytes("books/catalogue_page-1.html")).soup()
    assert scraper.next_page_url(first, first_url, 1) == f"{BOOKS}/catalogue/page-2.html"
    last_url = f"{BOOKS}/catalogue/page-50.html"
    last = Page(last_url, 200, fixture_bytes("books/catalogue_page-50.html")).soup()
    assert scraper.next_page_url(last, last_url, 50) is None


def test_template_pagination_url(quotes_config: SiteConfig) -> None:
    scraper = Scraper(quotes_config, fetcher=None)  # type: ignore[arg-type]
    assert scraper.next_page_url(None, "", 3) == f"{QUOTES}/page/4/"  # type: ignore[arg-type]


def test_books_crawl_follows_next_until_last_page(books_config: SiteConfig, clock: FakeClock) -> None:
    session = books_session()
    result = Scraper(books_config, _fetcher(books_config, session, clock)).run()

    assert result.complete
    assert result.stats.list_pages == 2
    assert result.stats.detail_pages == 40
    assert len(result.items) == 40
    assert session.urls()[:2] == [f"{BOOKS}/robots.txt", f"{BOOKS}/catalogue/page-1.html"]
    assert f"{BOOKS}/catalogue/page-3.html" not in session.urls()  # на последней странице «next» нет
    item = result.items[0]
    assert item["title"] == "A Light in the Attic" and item["upc"]  # поля списка + поля карточки
    assert item["price"] == 51.77 and item["stock_qty"] == 22


def test_max_pages_limit(books_config: SiteConfig, clock: FakeClock) -> None:
    session = books_session()
    result = Scraper(books_config, _fetcher(books_config, session, clock), max_pages=1).run()
    assert result.stats.list_pages == 1
    assert len(result.items) == 20
    assert f"{BOOKS}/catalogue/page-2.html" not in session.urls()


def test_max_items_limit_stops_early(books_config: SiteConfig, clock: FakeClock) -> None:
    session = books_session()
    result = Scraper(books_config, _fetcher(books_config, session, clock), max_items=5).run()
    assert len(result.items) == 5
    assert result.limit_reached
    assert result.stats.detail_pages == 5


def _quotes_session(pages: int, *, end: str = "empty") -> FakeSession:
    routes: dict[str, object] = {f"{QUOTES}/robots.txt": html_response("nope", status=404)}
    for n in range(1, pages + 1):
        routes[f"{QUOTES}/page/{n}/"] = html_response(
            fixture_bytes("quotes/page-1.html"), content_type="text/html; charset=utf-8"
        )
    if end == "empty":
        routes[f"{QUOTES}/page/{pages + 1}/"] = html_response(fixture_bytes("quotes/page-11.html"))
    return FakeSession(routes)  # type: ignore[arg-type]


def test_template_pagination_stops_on_empty_page(quotes_config: SiteConfig, clock: FakeClock) -> None:
    session = _quotes_session(3, end="empty")
    config = dataclasses.replace(
        quotes_config, changes=dataclasses.replace(quotes_config.changes, key=("text", "author", "url"))
    )
    result = Scraper(config, _fetcher(config, session, clock)).run()
    assert result.stats.list_pages == 4  # 3 страницы с цитатами + пустая «No quotes found!»
    assert len(result.items) == 30
    assert result.complete


def test_template_pagination_stops_on_404(quotes_config: SiteConfig, clock: FakeClock) -> None:
    session = _quotes_session(2, end="404")
    result = Scraper(quotes_config, _fetcher(quotes_config, session, clock)).run()
    assert result.stats.list_pages == 2
    assert result.complete  # 404 после первой страницы — конец каталога, а не ошибка
    # на страницах одинаковые цитаты: ключ «автор + текст» убирает повторы
    assert len(result.items) == 10
    assert result.stats.duplicates == 10


def test_first_page_404_is_an_error(quotes_config: SiteConfig, clock: FakeClock) -> None:
    session = FakeSession({f"{QUOTES}/robots.txt": html_response("", status=404)})
    result = Scraper(quotes_config, _fetcher(quotes_config, session, clock)).run()
    assert not result.complete
    assert "не найдена" in result.errors[0]


def test_pagination_loop_is_detected(books_config: SiteConfig, clock: FakeClock) -> None:
    looped = fixture_bytes("books/catalogue_page-1.html").replace(b'href="page-2.html"', b'href="page-1.html"')
    session = FakeSession(
        {
            f"{BOOKS}/robots.txt": html_response("", status=404),
            f"{BOOKS}/catalogue/page-1.html": html_response(looped),
        }
    )
    config = dataclasses.replace(books_config, detail=None, pagination=PaginationSpec(next="li.next a"))
    config = dataclasses.replace(config, changes=dataclasses.replace(config.changes, key=("url",)))
    result = Scraper(config, _fetcher(config, session, clock)).run()
    assert result.stats.list_pages == 1
    assert session.urls().count(f"{BOOKS}/catalogue/page-1.html") == 1


def test_empty_first_page_reports_selector_problem(books_config: SiteConfig, clock: FakeClock) -> None:
    session = FakeSession(
        {
            f"{BOOKS}/robots.txt": html_response("", status=404),
            f"{BOOKS}/catalogue/page-1.html": html_response("<html><body>Сайт на обслуживании</body></html>"),
        }
    )
    result = Scraper(books_config, _fetcher(books_config, session, clock)).run()
    assert result.items == []
    assert "нет карточек по селектору" in result.errors[0]
