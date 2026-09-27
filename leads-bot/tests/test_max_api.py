"""HTTP-клиент MAX через подменённую aiohttp-сессию — без сети."""

from __future__ import annotations

import json
from typing import Any

import aiohttp
import pytest

from leads_bot.transports.max_api import MaxApiClient, MaxApiError, MaxAuthError

TOKEN = "max-fake-token-for-tests-0123456789"


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        status, body = self.responses.pop(0)
        return FakeResponse(status, body)

    async def close(self) -> None:
        pass


def client(session: FakeSession) -> MaxApiClient:
    return MaxApiClient(TOKEN, session=session, retry_delay=0)  # type: ignore[arg-type]


async def test_get_updates_request() -> None:
    session = FakeSession((200, {"updates": [{"update_type": "bot_started"}], "marker": 42}))
    updates, marker = await client(session).get_updates(marker=41, timeout=30, types=("message_created", "bot_started"))
    assert updates == [{"update_type": "bot_started"}] and marker == 42
    [call] = session.calls
    assert (call["method"], call["url"]) == ("GET", "https://platform-api2.max.ru/updates")
    assert call["headers"] == {"Authorization": TOKEN}
    assert call["params"] == {"marker": 41, "timeout": 30, "limit": 100, "types": "message_created,bot_started"}
    assert TOKEN not in call["url"] and TOKEN not in str(call["params"])


async def test_send_message_request() -> None:
    session = FakeSession((200, {"message": {"body": {"mid": "mid.1"}}}))
    keyboard = [{"type": "inline_keyboard", "payload": {"buttons": [[{"type": "callback", "text": "Да", "payload": "yes"}]]}}]
    result = await client(session).send_message(text="<b>Привет</b>", user_id=2002, attachments=keyboard)
    assert result["message"]["body"]["mid"] == "mid.1"
    [call] = session.calls
    assert (call["method"], call["url"]) == ("POST", "https://platform-api2.max.ru/messages")
    assert call["params"] == {"user_id": 2002}
    assert call["json"] == {"text": "<b>Привет</b>", "attachments": keyboard, "format": "html"}


async def test_send_message_needs_exactly_one_recipient() -> None:
    api = client(FakeSession())
    with pytest.raises(ValueError):
        await api.send_message(text="x")
    with pytest.raises(ValueError):
        await api.send_message(text="x", chat_id=1, user_id=2)


async def test_auth_error_without_token_in_text() -> None:
    session = FakeSession((401, {"code": "verify.token", "message": f"Invalid access_token {TOKEN}"}))
    with pytest.raises(MaxAuthError) as info:
        await client(session).get_me()
    assert info.value.status == 401 and info.value.code == "verify.token"
    assert TOKEN not in str(info.value) and "***" in str(info.value)


async def test_attachment_not_ready_is_retried() -> None:
    session = FakeSession(
        (400, {"code": "attachment.not.ready", "message": "Key: errors.process.attachment.file.not.processed"}),
        (200, {"message": {"body": {"mid": "mid.2"}}}),
    )
    result = await client(session).send_message(text="файл", chat_id=-777, attachments=[{"type": "file", "payload": {"token": "t"}}])
    assert result["message"]["body"]["mid"] == "mid.2" and len(session.calls) == 2


async def test_other_errors_are_not_retried() -> None:
    session = FakeSession((403, {"code": "chat.denied", "message": "bot is not a member"}))
    with pytest.raises(MaxApiError, match="chat.denied"):
        await client(session).send_message(text="x", chat_id=-777)
    assert len(session.calls) == 1


async def test_upload_file_flow() -> None:
    session = FakeSession(
        (200, {"url": "https://upload.example/u?id=1", "token": "file-token"}),
        (200, "<retval>1</retval>"),  # ответ сервера загрузки не обязан быть JSON
    )
    token = await client(session).upload_file("leads.xlsx", b"PK\x03\x04")
    assert token == "file-token"
    first, second = session.calls
    assert (first["url"], first["params"]) == ("https://platform-api2.max.ru/uploads", {"type": "file"})
    assert second["url"] == "https://upload.example/u?id=1"
    assert second["headers"] == {}  # токен бота на сторонний адрес загрузки не уходит
    assert isinstance(second["data"], aiohttp.FormData)


async def test_upload_token_from_upload_response() -> None:
    session = FakeSession((200, {"url": "https://upload.example/u"}), (200, {"token": "from-upload"}))
    assert await client(session).upload_file("leads.xlsx", b"x") == "from-upload"


async def test_answer_callback_and_commands() -> None:
    session = FakeSession((200, {"success": True}), (200, {"commands": []}))
    api = client(session)
    await api.answer_callback("cb-1", notification="Принято")
    await api.set_commands([("start", "Оставить заявку")])
    answer, commands = session.calls
    assert answer["params"] == {"callback_id": "cb-1"} and answer["json"] == {"notification": "Принято"}
    assert (commands["method"], commands["json"]) == ("PATCH", {"commands": [{"name": "start", "description": "Оставить заявку"}]})


def test_repr_hides_token() -> None:
    assert TOKEN not in repr(MaxApiClient(TOKEN))
