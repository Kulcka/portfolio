"""Провайдеры LLM без сети: запросы перехватывает httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from docs_assistant.config import GigaChatSettings, Settings, YandexSettings
from docs_assistant.llm.base import ChatMessage, LLMError
from docs_assistant.llm.factory import create_embeddings, create_llm
from docs_assistant.llm.gigachat import GigaChatAuth, create_gigachat_provider, parse_expires_at
from docs_assistant.llm.http import JsonHttpClient
from docs_assistant.llm.openai_compat import OpenAICompatibleProvider, bearer_auth
from docs_assistant.llm.yandexgpt import create_yandex_provider
from docs_assistant.security import Secret

KEY = "sk-test-0123456789abcdefghijklmnop"
MESSAGES = [ChatMessage("system", "правила"), ChatMessage("user", "вопрос")]


def _chat_ok(text: str = "Ответ [a.pdf, стр. 1]") -> dict:
    return {
        "model": "m",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def test_openai_compatible_request_and_parse() -> None:
    rec = Recorder(httpx.Response(200, json=_chat_ok("<think>скрыто</think>Ответ [a.pdf, стр. 1]")))
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1/", model="qwen2.5:7b", auth=bearer_auth(Secret(KEY)),
        transport=rec.transport,
    )
    response = provider.complete(MESSAGES, temperature=0.2, max_tokens=100)
    request = rec.requests[0]
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b" and body["max_tokens"] == 100 and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "правила"}
    assert response.text == "Ответ [a.pdf, стр. 1]"  # рассуждения reasoning-модели вырезаны
    assert response.usage and response.usage.total_tokens == 15


def test_local_model_without_key_sends_no_auth_header() -> None:
    rec = Recorder(httpx.Response(200, json=_chat_ok()))
    OpenAICompatibleProvider(base_url="http://localhost:1234/v1", model="local", transport=rec.transport).complete(
        MESSAGES
    )
    assert "authorization" not in rec.requests[0].headers


def test_retry_on_429_then_success() -> None:
    sleeps: list[float] = []
    rec = Recorder(
        httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "rate limit"}}),
        httpx.Response(200, json=_chat_ok()),
    )
    provider = OpenAICompatibleProvider(
        base_url="https://api.example/v1", model="m", transport=rec.transport, sleep=sleeps.append
    )
    assert provider.complete(MESSAGES).text.startswith("Ответ")
    assert sleeps == [2.0]


def test_errors_never_contain_the_key(caplog: pytest.LogCaptureFixture) -> None:
    echo = {"error": {"message": f"Incorrect API key provided: {KEY}"}}
    rec = Recorder(httpx.Response(401, json=echo))
    provider = OpenAICompatibleProvider(
        base_url="https://api.example/v1", model="m", auth=bearer_auth(Secret(KEY)), transport=rec.transport
    )
    with pytest.raises(LLMError) as info:
        provider.complete(MESSAGES)
    error = info.value
    assert error.status == 401 and not error.retryable
    assert KEY not in str(error) and "***" in str(error)
    # исходное исключение HTTP-клиента не прицеплено — его текст не попадёт в трассировку
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_network_error_message_hides_query_string() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = JsonHttpClient(provider="x", transport=httpx.MockTransport(fail), retries=0)
    with pytest.raises(LLMError) as info:
        client.post_json("https://user:pass@api.example/v1/x?key=SECRETVALUE", headers={}, json_body={})
    assert "SECRETVALUE" not in str(info.value) and "pass" not in str(info.value)
    assert "https://api.example/v1/x" in str(info.value)


def test_yandex_native_request() -> None:
    rec = Recorder(
        httpx.Response(
            200,
            json={
                "result": {
                    "alternatives": [{"message": {"role": "assistant", "text": "Ответ"}, "status": "ALTERNATIVE_STATUS_FINAL"}],
                    "usage": {"inputTextTokens": "12", "completionTokens": "3", "totalTokens": "15"},
                    "modelVersion": "23.10.2024",
                }
            },
        )
    )
    settings = YandexSettings(api_key=Secret("AQVN-yandex-key-123"), folder_id="b1gfolder", model="yandexgpt-lite")
    provider = create_yandex_provider(settings, transport=rec.transport)
    response = provider.complete(MESSAGES, max_tokens=300)
    request = rec.requests[0]
    assert str(request.url) == "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    assert request.headers["authorization"] == "Api-Key AQVN-yandex-key-123"
    assert request.headers["x-data-logging-enabled"] == "false"
    body = json.loads(request.content)
    assert body["modelUri"] == "gpt://b1gfolder/yandexgpt-lite"
    assert body["completionOptions"]["maxTokens"] == "300"
    assert body["messages"][1] == {"role": "user", "text": "вопрос"}
    assert response.text == "Ответ" and response.usage and response.usage.total_tokens == 15


def test_yandex_openai_mode_and_content_filter() -> None:
    rec = Recorder(httpx.Response(200, json=_chat_ok()))
    settings = YandexSettings(
        api_key=Secret("AQVN-yandex-key-123"), folder_id="b1gfolder", model="qwen3-235b-a22b-fp8/latest",
        api_mode="openai",
    )
    create_yandex_provider(settings, transport=rec.transport).complete(MESSAGES)
    request = rec.requests[0]
    assert str(request.url) == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer AQVN-yandex-key-123"
    assert request.headers["openai-project"] == "b1gfolder"
    assert json.loads(request.content)["model"] == "gpt://b1gfolder/qwen3-235b-a22b-fp8/latest"

    filtered = Recorder(httpx.Response(200, json={"result": {"alternatives": [
        {"message": {"role": "assistant", "text": ""}, "status": "ALTERNATIVE_STATUS_CONTENT_FILTER"}]}}))
    native = create_yandex_provider(
        YandexSettings(iam_token=Secret("t1.iam-token-value"), folder_id="b1g"), transport=filtered.transport
    )
    with pytest.raises(LLMError, match="фильтром"):
        native.complete(MESSAGES)
    assert filtered.requests[0].headers["x-folder-id"] == "b1g"


def test_yandex_requires_folder_and_credentials() -> None:
    with pytest.raises(LLMError, match="YANDEX_FOLDER_ID"):
        create_yandex_provider(YandexSettings(api_key=Secret("AQVN-key-12345")))
    with pytest.raises(LLMError, match="YANDEX_API_KEY"):
        create_yandex_provider(YandexSettings(folder_id="b1g"))


def test_gigachat_oauth_token_cached_and_refreshed_on_401() -> None:
    now = [1_000_000.0]
    oauth_calls: list[httpx.Request] = []
    chat_calls: list[httpx.Request] = []
    chat_statuses = [200, 401, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth":
            oauth_calls.append(request)
            token = f"giga-access-token-{len(oauth_calls)}"
            return httpx.Response(200, json={"access_token": token, "expires_at": int((now[0] + 1800) * 1000)})
        chat_calls.append(request)
        status = chat_statuses.pop(0)
        return httpx.Response(status, json=_chat_ok() if status == 200 else {"message": "Token has expired"})

    settings = GigaChatSettings(auth_key=Secret("Z2lnYS1hdXRoLWtleQ=="))
    provider = create_gigachat_provider(settings, transport=httpx.MockTransport(handler))
    provider._client.auth._clock = lambda: now[0]  # noqa: SLF001 - управляем временем в тесте

    provider.complete(MESSAGES)
    first_oauth = oauth_calls[0]
    assert str(first_oauth.url) == "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    assert first_oauth.headers["authorization"] == "Basic Z2lnYS1hdXRoLWtleQ=="
    assert len(first_oauth.headers["rquid"]) == 36
    assert first_oauth.content == b"scope=GIGACHAT_API_PERS"
    assert str(chat_calls[0].url) == "https://api.giga.chat/v1/chat/completions"
    assert chat_calls[0].headers["authorization"] == "Bearer giga-access-token-1"
    assert json.loads(chat_calls[0].content)["model"] == "GigaChat-2"

    provider.complete(MESSAGES)  # 401 → новый токен → повтор
    assert len(oauth_calls) == 2
    assert chat_calls[-1].headers["authorization"] == "Bearer giga-access-token-2"


def test_gigachat_token_expiry_parsing() -> None:
    assert parse_expires_at(1_700_000_000_000, 0) == 1_700_000_000
    assert parse_expires_at(1_700_000_000, 0) == 1_700_000_000
    assert parse_expires_at(None, 100.0) == 100.0 + 1800


def test_gigachat_bad_auth_key_error_is_clean() -> None:
    rec = Recorder(httpx.Response(401, json={"code": 6, "message": "credentials doesn't match db data"}))
    http = JsonHttpClient(provider="gigachat-oauth", transport=rec.transport)
    auth = GigaChatAuth(auth_key=Secret("Z2lnYS1iYWQta2V5"), scope="GIGACHAT_API_PERS",
                        oauth_url="https://ngw.example/api/v2/oauth", http=http)
    with pytest.raises(LLMError) as info:
        auth.headers()
    assert "Z2lnYS1iYWQta2V5" not in str(info.value) and "HTTP 401" in str(info.value)
    assert info.value.status is None  # 401 авторизации не запускает цикл «обновить токен»


def test_gigachat_missing_ca_bundle_is_reported(tmp_path) -> None:
    settings = GigaChatSettings(auth_key=Secret("Z2lnYS1rZXk="), ca_bundle=tmp_path / "nope.crt")
    with pytest.raises(LLMError, match="сертификата не найден"):
        create_gigachat_provider(settings)


def test_factory_builds_each_provider(caplog: pytest.LogCaptureFixture) -> None:
    env = {
        "OPENAI_API_KEY": KEY, "OPENAI_BASE_URL": "https://openrouter.ai/api/v1", "OPENAI_MODEL": "x/y",
        "YANDEX_API_KEY": "AQVN-yandex-key-123", "YANDEX_FOLDER_ID": "b1g",
        "GIGACHAT_AUTH_KEY": "Z2lnYS1rZXk=",
        "EMBEDDINGS_MODEL": "text-embedding-3-small",
    }
    names = {}
    for provider in ("extractive", "fake", "openai", "yandexgpt", "gigachat"):
        settings = Settings.from_env({**env, "LLM_PROVIDER": provider})
        llm = create_llm(settings)
        names[provider] = llm.name
        llm.close()
    assert names == {"extractive": "extractive", "fake": "fake", "openai": "openai",
                     "yandexgpt": "yandexgpt", "gigachat": "gigachat"}
    embeddings = create_embeddings(Settings.from_env(env))
    assert embeddings is not None and "text-embedding-3-small" in embeddings.model_id
    assert create_embeddings(Settings.from_env({})) is None
    assert KEY not in caplog.text


def test_embeddings_request_batches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(inputs)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    from docs_assistant.llm.openai_compat import OpenAICompatibleEmbeddings

    emb = OpenAICompatibleEmbeddings(base_url="https://e.example/v1", model="m", batch_size=2,
                                     transport=httpx.MockTransport(handler))
    assert emb.embed_documents(["a", "bb", "ccc"]) == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
    assert emb.embed_query("abcd") == [4.0, 1.0]
