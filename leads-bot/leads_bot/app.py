"""Запуск бота: один процесс, одна база, транспорты Telegram и/или MAX.

Каждый транспорт работает в своей задаче под присмотром: сбой одного
(например, сеть до MAX) не останавливает другой. Неверный токен или
непроверяемый сертификат — ошибка настройки: такой транспорт выключается
с понятным сообщением, а если выключились все — процесс завершается с кодом 78,
и systemd не перезапускает его впустую.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import partial

import aiohttp
from aiogram.exceptions import TelegramUnauthorizedError

from .config import BotConfig, ConfigError, Settings
from .core.service import LeadService
from .core.storage import Storage
from .transports.common import AdminAccess
from .transports.max_api import MaxApiClient, MaxAuthError
from .transports.max_bot import MaxTransport
from .transports.telegram import TelegramTransport

log = logging.getLogger(__name__)

EXIT_CONFIG_ERROR = 78  # EX_CONFIG: в systemd-юните стоит RestartPreventExitStatus=78
MAINTENANCE_INTERVAL = 3600.0

Runner = Callable[[], Awaitable[None]]


class TransportsStopped(Exception):
    """Все транспорты выключены из-за ошибок настройки."""


async def _supervise(name: str, run: Runner, *, first_delay: float = 5.0, max_delay: float = 300.0) -> None:
    delay = first_delay
    while True:
        try:
            await run()
            log.warning("%s: транспорт остановился, перезапуск через %.0f с", name, delay)
        except (TelegramUnauthorizedError, MaxAuthError):
            log.critical("%s: токен не принят. Проверьте токен в .env — транспорт выключен", name)
            return
        except aiohttp.ClientConnectorCertificateError:
            log.critical(
                "%s: сертификат сервера не прошёл проверку. Для MAX добавьте корневой сертификат Минцифры "
                "(Russian Trusted Root CA) в доверенные или укажите путь к нему в MAX_CA_BUNDLE — транспорт выключен",
                name,
            )
            return
        except Exception:
            log.exception("%s: сбой транспорта, перезапуск через %.0f с", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def _maintenance_loop(service: LeadService) -> None:
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL)
        try:
            service.run_maintenance()
        except Exception:
            log.exception("Ошибка плановой очистки базы")


async def run_bot(settings: Settings, config: BotConfig, *, only: frozenset[str] | None = None) -> None:
    def wanted(name: str) -> bool:
        return only is None or name in only

    if not ((settings.telegram_token and wanted("telegram")) or (settings.max_token and wanted("max"))):
        raise ConfigError("Не задан ни TELEGRAM_BOT_TOKEN, ни MAX_BOT_TOKEN — запускать нечего")

    storage = Storage(settings.database_path)
    service = LeadService(config, storage)
    runners: list[tuple[str, Runner]] = []
    closers: list[Callable[[], Awaitable[None]]] = []

    try:
        if settings.telegram_token and wanted("telegram"):
            telegram = TelegramTransport(
                settings.telegram_token,
                service,
                AdminAccess(settings.telegram_admin_chat_id, settings.telegram_admin_user_ids),
            )
            notifier = telegram.notifier()
            if notifier is not None:
                service.add_notifier(notifier)
            else:
                log.warning("TELEGRAM_ADMIN_CHAT_ID не задан — уведомления о заявках в Telegram не придут")
            runners.append(("telegram", telegram.run))
            closers.append(telegram.close)

        if settings.max_token and wanted("max"):
            api = MaxApiClient(settings.max_token, base_url=settings.max_api_url, ca_bundle=settings.max_ca_bundle)
            max_transport = MaxTransport(
                api,
                service,
                AdminAccess(settings.max_admin_chat_id, settings.max_admin_user_ids),
                webhook_secret=settings.max_webhook_secret if settings.max_mode == "webhook" else None,
            )
            max_notifier = max_transport.notifier()
            if max_notifier is not None:
                service.add_notifier(max_notifier)
            else:
                log.warning("MAX_ADMIN_CHAT_ID не задан — уведомления о заявках в MAX не придут")
            if settings.max_mode == "webhook":
                assert settings.max_webhook_url is not None
                runner: Runner = partial(
                    max_transport.run_webhook,
                    public_url=settings.max_webhook_url,
                    path=settings.max_webhook_path,
                    host=settings.max_webhook_host,
                    port=settings.max_webhook_port,
                )
            else:
                runner = max_transport.run_polling
            runners.append(("max", runner))
            closers.append(api.close)

        service.run_maintenance()
        maintenance = asyncio.create_task(_maintenance_loop(service), name="maintenance")
        try:
            await asyncio.gather(*(_supervise(name, run) for name, run in runners))
        finally:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
        raise TransportsStopped("все транспорты выключены из-за ошибок настройки")
    finally:
        for close in closers:
            with contextlib.suppress(Exception):
                await close()
        storage.close()
        log.info("Бот остановлен")
