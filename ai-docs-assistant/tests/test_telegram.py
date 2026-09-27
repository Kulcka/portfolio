"""Обработчики Telegram-бота через подмену сообщения — без сети и без токена."""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Any

from docs_assistant.llm.fake import FakeLLMProvider
from docs_assistant.telegram_bot import (
    NOT_ADMIN_TEXT,
    RATE_LIMIT_TEXT,
    TELEGRAM_LIMIT,
    BotHandlers,
    create_router,
    split_message,
)

ADMIN_ID = 1001
USER_ID = 2002


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeMessage:
    text: str | None
    from_user: FakeUser | None
    sent: list[str] = field(default_factory=list)

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.sent.append(text)


def _handlers(make_assistant, llm=None, **settings_overrides) -> BotHandlers:
    assistant = make_assistant(llm or FakeLLMProvider())
    settings = dataclasses.replace(
        assistant.settings, telegram_admin_ids=frozenset({ADMIN_ID}), **settings_overrides
    )
    return BotHandlers(assistant, settings, clock=lambda: 0.0)


def test_question_gets_answer_with_sources(make_assistant) -> None:
    handlers = _handlers(make_assistant)
    message = FakeMessage("Сколько стоит застраховать посылку?", FakeUser(USER_ID))
    asyncio.run(handlers.question(message))
    assert len(message.sent) == 1
    assert "Источники:" in message.sent[0] and "[tarify-2026.pdf, стр. 2]" in message.sent[0]


def test_off_topic_question_refused(make_assistant) -> None:
    handlers = _handlers(make_assistant)
    message = FakeMessage("Как приготовить борщ?", FakeUser(USER_ID))
    asyncio.run(handlers.question(message))
    assert message.sent[0].startswith("В документах нет ответа")


def test_reindex_only_for_admin(make_assistant) -> None:
    handlers = _handlers(make_assistant)
    user_message = FakeMessage("/reindex", FakeUser(USER_ID))
    asyncio.run(handlers.reindex(user_message))
    assert user_message.sent == [NOT_ADMIN_TEXT]

    admin_message = FakeMessage("/reindex", FakeUser(ADMIN_ID))
    asyncio.run(handlers.reindex(admin_message))
    assert admin_message.sent[0].startswith("Обновляю")
    assert "Файлов в индексе: 7" in admin_message.sent[1]

    stats = FakeMessage("/stats", FakeUser(ADMIN_ID))
    asyncio.run(handlers.stats(stats))
    assert "В базе документов: 7" in stats.sent[0]


def test_rate_limit(make_assistant) -> None:
    handlers = _handlers(make_assistant, telegram_rate_limit_seconds=3.0)
    first = FakeMessage("Сколько стоит застраховать посылку?", FakeUser(USER_ID))
    second = FakeMessage("Как отследить посылку?", FakeUser(USER_ID))
    other_user = FakeMessage("Как отследить посылку?", FakeUser(USER_ID + 1))
    asyncio.run(handlers.question(first))
    asyncio.run(handlers.question(second))
    asyncio.run(handlers.question(other_user))
    assert second.sent == [RATE_LIMIT_TEXT]
    assert other_user.sent and other_user.sent[0] != RATE_LIMIT_TEXT


def test_unexpected_error_hidden_from_user(make_assistant) -> None:
    handlers = _handlers(make_assistant, llm=FakeLLMProvider([RuntimeError("stack trace with internals")]))
    message = FakeMessage("Сколько стоит застраховать посылку?", FakeUser(USER_ID))
    asyncio.run(handlers.question(message))
    assert "internals" not in message.sent[0]


def test_start_and_empty_message(make_assistant) -> None:
    handlers = _handlers(make_assistant)
    start = FakeMessage("/start", FakeUser(USER_ID))
    asyncio.run(handlers.start(start))
    assert "Ромашка-Логистик" in start.sent[0]
    empty = FakeMessage(None, FakeUser(USER_ID))
    asyncio.run(handlers.question(empty))
    assert empty.sent == ["Пришлите вопрос текстом."]


def test_split_long_message() -> None:
    text = "\n".join(f"строка {i} " + "x" * 90 for i in range(100))
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= TELEGRAM_LIMIT for p in parts)
    assert "\n".join(parts) == text
    assert split_message("y" * (TELEGRAM_LIMIT + 10)) == ["y" * TELEGRAM_LIMIT, "y" * 10]


def test_router_registers_handlers(make_assistant) -> None:
    router = create_router(_handlers(make_assistant))
    assert len(router.message.handlers) == 5
