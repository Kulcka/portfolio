"""Соблюдение robots.txt.

Правила читаются один раз на сайт (схема + хост) за прогон. Как трактуются
ответы сервера на запрос ``/robots.txt`` (по мотивам RFC 9309 и практики
поисковиков):

* 200 — разбираем правила;
* 401, 403 — сайт закрыт от роботов целиком;
* прочие 4xx (обычно 404) — ограничений нет;
* 5xx и сетевая ошибка — осторожно считаем, что всё запрещено: лучше не
  собрать данные, чем нагрузить сайт, который просил этого не делать.

``Crawl-delay`` из robots.txt учитывается: пауза между запросами берётся
как максимум из конфига и robots.txt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)

# fetch(url) -> (HTTP-статус, текст). Бросает исключение при сетевой ошибке.
RobotsFetch = Callable[[str], tuple[int, str]]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def robots_url_for(url: str) -> str:
    return origin_of(url) + "/robots.txt"


@dataclass(frozen=True)
class _SiteRules:
    """Правила одного сайта: разобранный файл или «всё можно» / «всё нельзя»."""

    parser: RobotFileParser | None = None
    allow_all: bool = False

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.parser is None:
            return self.allow_all
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None


ALLOW_ALL = _SiteRules(allow_all=True)
DENY_ALL = _SiteRules(allow_all=False)


class RobotsPolicy:
    """Проверка разрешений robots.txt для одного User-Agent."""

    def __init__(self, user_agent: str, fetch: RobotsFetch) -> None:
        self.user_agent = user_agent
        self._fetch = fetch
        self._rules: dict[str, _SiteRules] = {}

    def _rules_for(self, url: str) -> _SiteRules:
        origin = origin_of(url)
        rules = self._rules.get(origin)
        if rules is None:
            rules = self._load(origin)
            self._rules[origin] = rules
        return rules

    def _load(self, origin: str) -> _SiteRules:
        robots_url = origin + "/robots.txt"
        try:
            status, text = self._fetch(robots_url)
        except Exception as exc:  # сеть, таймаут, исчерпаны повторы
            log.warning("robots.txt %s недоступен (%s) — сайт считаем закрытым для обхода", robots_url, exc)
            return DENY_ALL
        if status in (401, 403):
            log.warning("robots.txt %s: доступ запрещён (HTTP %s) — обход не ведём", robots_url, status)
            return DENY_ALL
        if 400 <= status < 500:
            log.info("robots.txt %s: файла нет (HTTP %s) — ограничений нет", robots_url, status)
            return ALLOW_ALL
        if status >= 500 or status < 200:
            log.warning("robots.txt %s: ошибка сервера (HTTP %s) — сайт считаем закрытым", robots_url, status)
            return DENY_ALL
        parser = RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        log.info("robots.txt %s прочитан", robots_url)
        return _SiteRules(parser=parser)

    def can_fetch(self, url: str) -> bool:
        """Разрешён ли адрес для нашего User-Agent."""
        if urlsplit(url).path == "/robots.txt":
            return True
        return self._rules_for(url).can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """``Crawl-delay`` для нашего User-Agent, если сайт его задал."""
        return self._rules_for(url).crawl_delay(self.user_agent)
