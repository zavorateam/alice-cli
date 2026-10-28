"""§10 OpenAI SDK integration — реальный HTTP через uvicorn + openai SDK.

Запускает uvicorn.Server в threading.Thread на свободном порту, подсовывает
fake SessionRegistry с детерминированными ответами, и подключается
официальным openai.OpenAI(base_url=..., api_key="test").

Проверяет, что наш gateway выдаёт объекты/события, которые SDK способен
распарсить — а не просто «в JSON есть нужные ключи».
"""
import asyncio
import contextlib
import socket
import threading
import time

import pytest

# Skip если openai SDK не установлен
openai_sdk = pytest.importorskip("openai")


# ---------------- infrastructure ----------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ScriptedSession:
    """Детерминированная сессия: следующий send_message → следующий текст."""
    def __init__(self, texts, dialog_id="d-sdk"):
        self._texts = list(texts)
        self.dialog_id = dialog_id
        self.prompts = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.prompts.append(prompt)
        text = self._texts.pop(0) if self._texts else ""
        # эмулируем поток: отдаём кусками, потом is_finished
        if text:
            # 3 куска
            n = max(1, len(text) // 3)
            chunks = [text[i:i + n] for i in range(0, len(text), n)]
            accum = ""
            for ch in chunks:
                accum += ch
                yield AliceTurnResponse(
                    dialog_id=self.dialog_id, request_id="r",
                    text=accum, is_finished=False,
                )
            yield AliceTurnResponse(
                dialog_id=self.dialog_id, request_id="r",
                text=text, is_finished=True,
            )
        else:
            yield AliceTurnResponse(
                dialog_id=self.dialog_id, request_id="r",
                text="", is_finished=True,
            )
    async def cancel_current(self):
        return None


class _SessionRegistry:
    def __init__(self, session):
        self._s = session
    async def acquire(self, sid):
        return self._s
    async def close(self):
        return None


@contextlib.contextmanager
def running_gateway(texts, model_status="available"):
    """Поднимает uvicorn в фоне, yield base_url."""
    import uvicorn
    from fastapi import FastAPI
    from tools.gateway import ModelCatalog, attach_openai_routes

    session = _ScriptedSession(texts)
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", model_status)
    catalog.set_status("alice-pro", "degraded",
                       reason="Pro -> Base", effective_mode="Base")
    app = FastAPI()
    attach_openai_routes(app, catalog, _SessionRegistry(session))

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    def _run():
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    # ждём, пока порт займётся
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn не поднялся")

    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, session
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _client(base_url):
    return openai_sdk.OpenAI(base_url=base_url + "/v1", api_key="test")


# ---------------- tests ----------------

def test_sdk_models_list():
    with running_gateway([]) as (url, _):
        client = _client(url)
        models = client.models.list()
        ids = [m.id for m in models.data]
        assert "alice-base" in ids
        assert "alice-pro" in ids
        # SDK types check: у m есть object == "model"
        for m in models.data:
            assert m.object == "model"
            assert m.owned_by == "alice-cli"


def test_sdk_chat_completion_non_stream():
    with running_gateway(["Hello from Alice"]) as (url, session):
        client = _client(url)
        resp = client.chat.completions.create(
            model="alice-base",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp.choices[0].message.content == "Hello from Alice"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.id.startswith("chatcmpl-")
        assert resp.model == "alice-base"


def test_sdk_chat_completion_stream():
    with running_gateway(["Hello streaming"]) as (url, _):
        client = _client(url)
        stream = client.chat.completions.create(
            model="alice-base",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        collected = []
        finish_reason = None
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                collected.append(delta.content)
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
        assert finish_reason == "stop"
        assert "".join(collected) == "Hello streaming"


def test_sdk_tools_returns_tool_calls():
    tool_call_text = '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>'
    with running_gateway([tool_call_text]) as (url, _):
        client = _client(url)
        resp = client.chat.completions.create(
            model="alice-base",
            messages=[{"role": "user", "content": "weather?"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }],
        )
        choice = resp.choices[0]
        assert choice.finish_reason == "tool_calls"
        tc = choice.message.tool_calls[0]
        assert tc.type == "function"
        assert tc.function.name == "get_weather"
        import json
        assert json.loads(tc.function.arguments) == {"city": "M"}
        assert tc.id.startswith("call_")


def test_sdk_tool_continuation():
    """SDK: assistant.tool_calls → tool result → continuation."""
    tool_call_text = '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>'
    final_text = "Weather is fine"
    with running_gateway([tool_call_text, final_text]) as (url, session):
        client = _client(url)
        tools = [{
            "type": "function",
            "function": {"name": "get_weather", "description": "W",
                         "parameters": {"type": "object",
                                        "properties": {"city": {"type": "string"}}}},
        }]
        # 1-й turn: модель → tool_calls
        first = client.chat.completions.create(
            model="alice-base",
            messages=[{"role": "user", "content": "weather in Moscow?"}],
            tools=tools,
        )
        tc = first.choices[0].message.tool_calls[0]

        # 2-й turn: клиент вернул tool result
        second = client.chat.completions.create(
            model="alice-base",
            messages=[
                {"role": "user", "content": "weather in Moscow?"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{
                     "id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments},
                 }]},
                {"role": "tool", "tool_call_id": tc.id, "content": '{"temp": 5}'},
            ],
            tools=tools,
        )
        assert second.choices[0].message.content == final_text
        assert second.choices[0].finish_reason == "stop"


def test_sdk_responses_create_non_stream():
    with running_gateway(["Response API answer"]) as (url, _):
        client = _client(url)
        resp = client.responses.create(
            model="alice-base",
            input="hello",
        )
        assert resp.id.startswith("resp_")
        assert resp.status == "completed"
        # output_text должен быть доступен (SDK conveniences)
        text = resp.output_text
        assert "Response API answer" in text


def test_sdk_responses_stream():
    with running_gateway(["Streamed response"]) as (url, _):
        client = _client(url)
        stream = client.responses.create(
            model="alice-base",
            input="hello",
            stream=True,
        )
        deltas = []
        completed = None
        for event in stream:
            et = getattr(event, "type", None)
            if et == "response.output_text.delta":
                deltas.append(event.delta)
            elif et == "response.completed":
                completed = event
        assert "".join(deltas) == "Streamed response"
        assert completed is not None


def test_sdk_error_404_unknown_model():
    with running_gateway([]) as (url, _):
        client = _client(url)
        with pytest.raises(openai_sdk.NotFoundError) as exc_info:
            client.chat.completions.create(
                model="alice-unknown",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert "not found" in str(exc_info.value)


def test_sdk_error_503_implemented_model():
    """alice-expert — implemented, но не verified → 503 model_not_verified."""
    with running_gateway([]) as (url, _):
        client = _client(url)
        with pytest.raises(openai_sdk.APIStatusError) as exc_info:
            client.chat.completions.create(
                model="alice-expert",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert exc_info.value.status_code == 503


def test_sdk_error_400_invalid_json():
    with running_gateway([]) as (url, _):
        client = _client(url)
        # SDK сериализует dict; чтобы передать сырой невалидный JSON — только
        # через обход SDK. Проверяем 400 через невалидное сообщение.
        with pytest.raises(openai_sdk.BadRequestError):
            client.chat.completions.create(
                model="alice-base",
                messages=[],
            )
