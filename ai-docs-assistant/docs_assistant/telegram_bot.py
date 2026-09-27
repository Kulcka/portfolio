"""Telegram-бот на aiogram 3.

Команды:

* ``/start``, ``/help`` — приветствие и примеры вопросов;
* ``/reindex`` — пересобрать индекс (только для id из ``TELEGRAM_ADMIN_IDS``);
* ``/stats`` — сколько документов и фрагментов в базе (только администраторы);
* любой текст — вопрос; ответ приходит со списком источников.

Обработчики собраны в класс :class:`BotHandlers`, чтобы их можно было
проверять в тестах без сети: достаточно подставить объект с методом
``answer`` вместо сообщения Telegram. Поиск и модель работают синхронно,
поэтому вызываются в отдельном потоке (``asyncio.to_thread``) и не
блокируют приём других сообщений.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from docs_assistant.assistant import DocsAssistant
from docs_assistant.config import ConfigError, Settings
from docs_assistant.index import IndexBuildError

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096

START_TEXT = (
    "Здравствуйте! Я консультант {company}.\n"
    "Отвечаю на вопросы по документам компании и указываю, откуда взят ответ.\n\n"
    "Например:\n"
    "• Сколько стоит доставка посылки до 5 кг?\n"
    "• Как вернуть товар и в какой срок?\n"
    "• Во сколько работает поддержка в субботу?"
)
NOT_ADMIN_TEXT = "Эта команда доступна только администратору."
RATE_LIMIT_TEXT = "Слишком часто. Подождите пару секунд и повторите вопрос."
REINDEX_START_TEXT = "Обновляю базу документов…"
REINDEX_FAIL_TEXT = "Не удалось обновить базу документов. Подробности — в журнале сервера."
BUSY_TEXT = "Что-то пошло не так. Попробуйте ещё раз чуть позже."


class _User(Protocol):
    id: int


class IncomingMessage(Protocol):
    """То, что нужно обработчикам от сообщения Telegram."""

    text: str | None
    from_user: _User | None

    async def answer(self, text: str, **kwargs: Any) -> Any: ...


class BotHandlers:
    def __init__(
        self,
        assistant: DocsAssistant,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.assistant = assistant
        self.settings = settings
        self._clock = clock
        self._last_question: dict[int, float] = {}
        self._reindex_lock = asyncio.Lock()

    def is_admin(self, message: IncomingMessage) -> bool:
        user = message.from_user
        return user is not None and user.id in self.settings.telegram_admin_ids

    async def start(self, message: IncomingMessage) -> None:
        await message.answer(START_TEXT.format(company=self.settings.company_name))

    async def reindex(self, message: IncomingMessage) -> None:
        if not self.is_admin(message):
            await message.answer(NOT_ADMIN_TEXT)
            return
        if self._reindex_lock.locked():
            await message.answer("Обновление уже идёт.")
            return
        async with self._reindex_lock:
            await message.answer(REINDEX_START_TEXT)
            try:
                report = await asyncio.to_thread(self.assistant.reindex)
            except (IndexBuildError, OSError) as exc:
                logger.error("Переиндексация не удалась: %s", exc)
                await message.answer(REINDEX_FAIL_TEXT)
                return
            await _send_long(message, "Готово.\n" + report.summary())

    async def stats(self, message: IncomingMessage) -> None:
        if not self.is_admin(message):
            await message.answer(NOT_ADMIN_TEXT)
            return
        stats = await asyncio.to_thread(self.assistant.stats)
        await message.answer(f"В базе документов: {stats['documents']}, фрагментов: {stats['chunks']}.")

    async def question(self, message: IncomingMessage) -> None:
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пришлите вопрос текстом.")
            return
        user_id = message.from_user.id if message.from_user else 0
        if not self._allow(user_id):
            await message.answer(RATE_LIMIT_TEXT)
            return
        if self.settings.log_questions:
            logger.info("Вопрос от %s: %s", user_id, text[:200])
        try:
            answer = await asyncio.to_thread(self.assistant.ask, text)
        except Exception:  # noqa: BLE001 - пользователь не должен видеть трассировку
            logger.exception("Ошибка при ответе на вопрос")
            await message.answer(BUSY_TEXT)
            return
        logger.info("Ответ пользователю %s: статус %s (%s)", user_id, answer.status, answer.reason)
        await _send_long(message, answer.render())

    def _allow(self, user_id: int) -> bool:
        limit = self.settings.telegram_rate_limit_seconds
        if limit <= 0:
            return True
        now = self._clock()
        last = self._last_question.get(user_id)
        if last is not None and now - last < limit:
            return False
        self._last_question[user_id] = now
        return True


async def _send_long(message: IncomingMessage, text: str) -> None:
    """Отправить текст частями не длиннее лимита Telegram, по границам строк."""
    for part in split_message(text):
        await message.answer(part, disable_web_page_preview=True)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    return parts


def create_router(handlers: BotHandlers) -> Any:
    from aiogram import F, Router
    from aiogram.filters import Command, CommandStart

    router = Router(name="docs_assistant")
    router.message.register(handlers.start, CommandStart())
    router.message.register(handlers.start, Command("help"))
    router.message.register(handlers.reindex, Command("reindex"))
    router.message.register(handlers.stats, Command("stats"))
    router.message.register(handlers.question, F.text & ~F.text.startswith("/"))
    return router


def run_bot(settings: Settings) -> None:
    if not settings.telegram_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN: задайте токен бота от @BotFather")
    if not settings.telegram_admin_ids:
        logger.warning("TELEGRAM_ADMIN_IDS не задан — команда /reindex никому не доступна")
    asyncio.run(_run(settings))


async def _run(settings: Settings) -> None:
    from aiogram import Bot, Dispatcher

    assistant = DocsAssistant.from_settings(settings)
    report = await asyncio.to_thread(assistant.reindex)
    logger.info("База документов: %d файлов, %d фрагментов", report.total_files, report.total_chunks)

    bot = Bot(token=settings.telegram_token.get())
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(BotHandlers(assistant, settings)))
    logger.info("Бот запущен, провайдер модели: %s", settings.llm_provider)
    try:
        await dispatcher.start_polling(bot, handle_signals=True)
    finally:
        await bot.session.close()
        assistant.close()
