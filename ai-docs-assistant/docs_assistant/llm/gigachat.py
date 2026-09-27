"""GigaChat (Сбер).

Авторизация по документации (developers.sber.ru/docs/ru/gigachat/api/authorization):

1. ``POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth`` с заголовками
   ``Authorization: Basic <ключ авторизации>``, ``RqUID: <uuid4>`` и телом
   ``scope=GIGACHAT_API_PERS`` (физлица) / ``GIGACHAT_API_B2B`` / ``GIGACHAT_API_CORP``;
2. в ответе ``access_token`` (живёт 30 минут) и ``expires_at``;
3. запросы к модели — ``Authorization: Bearer <access_token>`` на
   ``https://api.giga.chat/v1`` (с 17.07.2026; прежний адрес
   ``https://gigachat.devices.sberbank.ru/api/v1`` документация называет
   по-прежнему доступным). Формат ``/chat/completions`` совместим с OpenAI.

TLS-сертификаты серверов GigaChat выпущены НУЦ Минцифры: без корневого
сертификата (https://www.gosuslugi.ru/crt) проверка TLS не проходит —
путь к нему задаётся в ``GIGACHAT_CA_BUNDLE``. Отключать проверку сертификата
мы сознательно не даём.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

import httpx

from docs_assistant.config import GigaChatSettings
from docs_assistant.llm.base import LLMError
from docs_assistant.llm.http import JsonHttpClient, ssl_context
from docs_assistant.llm.openai_compat import OpenAICompatibleEmbeddings, OpenAICompatibleProvider
from docs_assistant.security import REGISTRY, Secret

TOKEN_LIFETIME_FALLBACK = 30 * 60  # секунд, по документации
_REFRESH_MARGIN = 60  # обновляем токен за минуту до истечения


class GigaChatAuth:
    """Получает и кэширует токен доступа; потокобезопасен."""

    refreshable = True

    def __init__(
        self,
        *,
        auth_key: Secret,
        scope: str,
        oauth_url: str,
        http: JsonHttpClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not auth_key:
            raise LLMError("gigachat: не задан ключ авторизации GIGACHAT_AUTH_KEY")
        REGISTRY.add(auth_key)
        self._auth_key = auth_key
        self._scope = scope
        self._oauth_url = oauth_url
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _get_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - _REFRESH_MARGIN:
                return self._token
            try:
                data = self._http.post_json(
                    self._oauth_url,
                    headers={
                        "Authorization": f"Basic {self._auth_key.get()}",
                        "RqUID": str(uuid.uuid4()),
                        "Accept": "application/json",
                    },
                    form={"scope": self._scope},
                )
            except LLMError as error:
                # Без статуса: 401 от сервиса авторизации не должен запускать «обновить токен и повторить».
                raise LLMError(str(error), retryable=error.retryable) from None
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                raise LLMError("gigachat: сервис авторизации не вернул токен доступа")
            REGISTRY.discard(self._token)
            REGISTRY.add(token)
            self._token = token
            self._expires_at = parse_expires_at(data.get("expires_at"), now)
            return token


def parse_expires_at(value: object, now: float) -> float:
    """Время истечения токена в секундах Unix.

    В документации пример в секундах, а сервис исторически отдавал миллисекунды —
    принимаем оба варианта. Нет поля — считаем по документированным 30 минутам.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return now + TOKEN_LIFETIME_FALLBACK
    if number > 1e11:  # миллисекунды
        number /= 1000.0
    return number


def create_gigachat_provider(
    settings: GigaChatSettings,
    *,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> OpenAICompatibleProvider:
    verify = ssl_context(settings.ca_bundle)
    auth = _auth(settings, timeout=timeout, verify=verify, transport=transport, sleep=sleep)
    return OpenAICompatibleProvider(
        name="gigachat",
        base_url=settings.base_url,
        model=settings.model,
        auth=auth,
        timeout=timeout,
        verify=verify,
        transport=transport,
        sleep=sleep,
    )


def create_gigachat_embeddings(
    settings: GigaChatSettings,
    *,
    model: str,
    batch_size: int = 16,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> OpenAICompatibleEmbeddings:
    verify = ssl_context(settings.ca_bundle)
    auth = _auth(settings, timeout=timeout, verify=verify, transport=transport, sleep=time.sleep)
    return OpenAICompatibleEmbeddings(
        name="gigachat-embeddings",
        base_url=settings.base_url,
        model=model,
        auth=auth,
        batch_size=batch_size,
        timeout=timeout,
        verify=verify,
        transport=transport,
    )


def _auth(
    settings: GigaChatSettings,
    *,
    timeout: float,
    verify: object,
    transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> GigaChatAuth:
    oauth_http = JsonHttpClient(
        provider="gigachat-oauth",
        timeout=timeout,
        verify=verify,  # type: ignore[arg-type]
        transport=transport,
        sleep=sleep,
    )
    return GigaChatAuth(
        auth_key=settings.auth_key, scope=settings.scope, oauth_url=settings.oauth_url, http=oauth_http
    )
