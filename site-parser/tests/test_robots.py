"""robots.txt: запреты, отсутствие файла, закрытый сайт, сбой сервера, Crawl-delay."""

from __future__ import annotations

import pytest
import requests

from site_parser.config import RequestSettings
from site_parser.fetcher import Fetcher, RobotsDisallowed
from site_parser.robots import RobotsPolicy, robots_url_for
from tests.conftest import FakeClock, FakeSession, fixture_bytes, html_response

SITE = "https://shop.test"
UA = "SiteParserDemo/1.0 (+test)"


def make_fetcher(session: FakeSession, clock: FakeClock, **overrides: object) -> Fetcher:
    params: dict[str, object] = {"delay": 1.0, "user_agent": UA, "respect_robots": True, "retries": 1, "backoff": 1.0}
    params.update(overrides)
    return Fetcher(RequestSettings(**params), session=session, sleep=clock.sleep, clock=clock.time)  # type: ignore[arg-type]


def robots_session(status: int = 200, body: bytes | None = None) -> FakeSession:
    return FakeSession(
        {
            f"{SITE}/robots.txt": html_response(
                body if body is not None else fixture_bytes("robots.txt"), status=status, content_type="text/plain"
            )
        },
        default=html_response("<p>ok</p>"),
    )


def test_disallowed_path_is_not_requested(clock: FakeClock) -> None:
    session = robots_session()
    fetcher = make_fetcher(session, clock)
    with pytest.raises(RobotsDisallowed):
        fetcher.get(f"{SITE}/cart/checkout")
    assert session.urls() == [f"{SITE}/robots.txt"]  # сама страница не запрашивалась
    assert fetcher.stats.robots_blocked == 1


def test_allowed_paths_and_allow_override(clock: FakeClock) -> None:
    session = robots_session()
    fetcher = make_fetcher(session, clock)
    fetcher.get(f"{SITE}/catalog/page-1.html")
    fetcher.get(f"{SITE}/search/help")  # Allow внутри запрещённого /search/
    with pytest.raises(RobotsDisallowed):
        fetcher.get(f"{SITE}/search?q=phone")
    assert session.urls().count(f"{SITE}/robots.txt") == 1  # robots.txt читается один раз


def test_specific_user_agent_group(clock: FakeClock) -> None:
    session = robots_session()
    fetcher = make_fetcher(session, clock, user_agent="BadBot/1.0")
    with pytest.raises(RobotsDisallowed):
        fetcher.get(f"{SITE}/catalog/page-1.html")


def test_crawl_delay_raises_pause(clock: FakeClock) -> None:
    session = robots_session()
    fetcher = make_fetcher(session, clock, delay=1.0)
    fetcher.get(f"{SITE}/catalog/1")
    fetcher.get(f"{SITE}/catalog/2")
    # robots.txt → страница 1 → страница 2: паузы по Crawl-delay (3 с), а не по конфигу (1 с)
    assert clock.sleeps == [3.0, 3.0]


def test_missing_robots_allows_everything(clock: FakeClock) -> None:
    session = robots_session(status=404, body=b"not found")
    make_fetcher(session, clock).get(f"{SITE}/cart/checkout")
    assert session.urls()[-1] == f"{SITE}/cart/checkout"


@pytest.mark.parametrize("status", [401, 403])
def test_forbidden_robots_blocks_site(clock: FakeClock, status: int) -> None:
    session = robots_session(status=status, body=b"")
    with pytest.raises(RobotsDisallowed):
        make_fetcher(session, clock).get(f"{SITE}/catalog/1")


def test_server_error_on_robots_blocks_site(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/robots.txt": [html_response("", status=503), html_response("", status=503)]})
    with pytest.raises(RobotsDisallowed):
        make_fetcher(session, clock).get(f"{SITE}/catalog/1")


def test_network_error_on_robots_blocks_site(clock: FakeClock) -> None:
    session = FakeSession({f"{SITE}/robots.txt": [requests.ConnectionError("x"), requests.ConnectionError("x")]})
    with pytest.raises(RobotsDisallowed):
        make_fetcher(session, clock).get(f"{SITE}/catalog/1")


def test_robots_can_be_disabled(clock: FakeClock) -> None:
    session = robots_session()
    make_fetcher(session, clock, respect_robots=False).get(f"{SITE}/cart/checkout")
    assert f"{SITE}/robots.txt" not in session.urls()


def test_policy_helpers() -> None:
    assert robots_url_for("https://Shop.test/a/b?c=1") == "https://shop.test/robots.txt"
    policy = RobotsPolicy(UA, lambda url: (200, "User-agent: *\nDisallow: /private\nCrawl-delay: 5\n"))
    assert policy.can_fetch(f"{SITE}/public") is True
    assert policy.can_fetch(f"{SITE}/private/x") is False
    assert policy.can_fetch(f"{SITE}/robots.txt") is True
    assert policy.crawl_delay(f"{SITE}/x") == 5.0
