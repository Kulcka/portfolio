"""Сетевой слой: повторы с экспоненциальной паузой, пауза между запросами, User-Agent, кэш, кодировка."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from site_parser.config import RequestSettings
from site_parser.fetcher import Fetcher, FetchError, NotFound, PageCache, charset_from_content_type
from tests.conftest import FakeClock, FakeResponse, FakeSession, html_response

SITE = "https://shop.test"


def make_fetcher(session: FakeSession, clock: FakeClock, **overrides: object) -> Fetcher:
    params: dict[str, object] = {"delay": 1.0, "retries": 3, "backoff": 2.0, "respect_robots": False}
    params.update(overrides)
    return Fetcher(RequestSettings(**params), session=session, sleep=clock.sleep, clock=clock.time)  # type: ignore[arg-type]


def test_retries_with_exponential_backoff_then_success(clock: FakeClock) -> None:
    session = FakeSession(
        {
            f"{SITE}/a": [
                html_response("busy", status=503),
                requests.ConnectionError("reset"),
                html_response("slow", status=502),
                html_response("<p>ok</p>"),
            ]
        }
    )
    page = make_fetcher(session, clock).get(f"{SITE}/a")
    assert page.status == 200 and b"ok" in page.content
    assert clock.sleeps == [2.0, 4.0, 8.0]  # backoff * 2**попытка
    assert len(session.calls) == 4


def test_gives_up_after_retries(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/a": [html_response("", status=500)] * 3})
    with pytest.raises(FetchError, match="HTTP 500") as info:
        make_fetcher(session, clock, retries=2).get(f"{SITE}/a")
    assert info.value.status == 500
    assert len(session.calls) == 3


def test_network_error_after_retries(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/a": [requests.Timeout("t")] * 2})
    with pytest.raises(FetchError, match="таймаут"):
        make_fetcher(session, clock, retries=1).get(f"{SITE}/a")


def test_backoff_is_capped(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/a": [html_response("", status=503)] * 4 + [html_response("ok")]})
    make_fetcher(session, clock, retries=4, backoff=10.0, backoff_max=25.0).get(f"{SITE}/a")
    assert clock.sleeps == [10.0, 20.0, 25.0, 25.0]


def test_retry_after_header_is_respected(clock: FakeClock) -> None:
    limited = html_response("slow down", status=429)
    limited.headers["Retry-After"] = "7"
    session = FakeSession({f"{SITE}/a": [limited, html_response("ok")]})
    make_fetcher(session, clock).get(f"{SITE}/a")
    assert clock.sleeps == [7.0]


def test_404_is_not_retried(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/a": html_response("", status=404)})
    with pytest.raises(NotFound):
        make_fetcher(session, clock).get(f"{SITE}/a")
    assert len(session.calls) == 1
    assert clock.sleeps == []


def test_delay_between_requests_to_same_host(clock: FakeClock) -> None:
    session = FakeSession(default=html_response("ok"))
    fetcher = make_fetcher(session, clock, delay=1.5)
    fetcher.get(f"{SITE}/1")
    fetcher.get(f"{SITE}/2")
    clock.now += 0.5  # полсекунды «работы» между запросами
    fetcher.get(f"{SITE}/3")
    fetcher.get("https://other.test/1")  # другой хост — без паузы
    assert clock.sleeps == [1.5, 1.0]


def test_user_agent_and_custom_headers(clock: FakeClock) -> None:
    session = FakeSession(default=html_response("ok"))
    make_fetcher(session, clock, user_agent="MyBot/2.0", headers={"Cookie": "region=77"}).get(f"{SITE}/a")
    headers = session.calls[0][1]["headers"]
    assert headers["User-Agent"] == "MyBot/2.0"
    assert headers["Cookie"] == "region=77"
    assert session.calls[0][1]["timeout"] == 20.0


def test_cache_avoids_second_request(tmp_path: Path, clock: FakeClock) -> None:
    session = FakeSession(default=html_response("<p>£1</p>", content_type="text/html; charset=utf-8"))
    fetcher = make_fetcher(session, clock)
    fetcher.cache = PageCache(tmp_path)
    first = fetcher.get(f"{SITE}/a")
    second = fetcher.get(f"{SITE}/a")
    assert len(session.calls) == 1
    assert second.from_cache and not first.from_cache
    assert second.content == first.content and second.encoding == "utf-8"
    assert fetcher.stats.cache_hits == 1


def test_cache_ttl_expires(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = PageCache(tmp_path, ttl_hours=1)
    session = FakeSession(default=html_response("ok"))
    fetcher = make_fetcher(session, clock)
    fetcher.cache = cache
    fetcher.get(f"{SITE}/a")
    import site_parser.fetcher as module

    real_time = module.time.time
    monkeypatch.setattr(module.time, "time", lambda: real_time() + 7200)
    fetcher.get(f"{SITE}/a")
    assert len(session.calls) == 2


def test_encoding_from_meta_when_header_has_no_charset(clock: FakeClock) -> None:
    body = '<html><head><meta charset="utf-8"></head><body><p class="p">£51.77 — цена</p></body></html>'
    session = FakeSession(default=html_response(body.encode("utf-8"), content_type="text/html"))
    page = make_fetcher(session, clock).get(f"{SITE}/a")
    assert page.encoding is None
    assert page.soup().select_one("p.p").get_text() == "£51.77 — цена"  # type: ignore[union-attr]


def test_windows_1251_page(clock: FakeClock) -> None:
    body = "<html><body><h1>Цена 1 299 руб.</h1></body></html>".encode("cp1251")
    session = FakeSession(default=html_response(body, content_type="text/html; charset=windows-1251"))
    page = make_fetcher(session, clock).get(f"{SITE}/a")
    assert page.soup().h1.get_text() == "Цена 1 299 руб."  # type: ignore[union-attr]


def test_charset_parsing() -> None:
    assert charset_from_content_type("text/html; charset=UTF-8") == "utf-8"
    assert charset_from_content_type('text/html; charset="windows-1251"') == "windows-1251"
    assert charset_from_content_type("text/html") is None


def test_binary_download_uses_image_accept(clock: FakeClock) -> None:
    image = FakeResponse(200, b"\x89PNG", CaseInsensitiveDict({"Content-Type": "image/png"}))
    session = FakeSession({f"{SITE}/i.png": image})
    page = make_fetcher(session, clock).get_binary(f"{SITE}/i.png")
    assert page.content_type == "image/png"
    assert session.calls[0][1]["headers"]["Accept"].startswith("image/")
