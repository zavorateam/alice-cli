"""Phase D: реальный маршрут tool_calls → tool result → продолжение.

Проверяет не только _extract_tool_calls, но и:
  - stream выдаёт tool_calls через delta.tool_calls
  - finish_reason="tool_calls" в stream
  - история с assistant.tool_calls + tool ролью доходит до промпта
  - tool_choice=none отключает tools
  - неизвестный tool не превращается в tool_call
"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


TOOLS_DEF = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}},
                       "required": ["city"]},
    }}
]


class _FakeSession:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.history = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.history.append(prompt)
        text = self._scripted.pop(0) if self._scripted else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r", text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _FakeRegistry:
    def __init__(self, session):
        self._session = session
    async def acquire(self, sid):
        return self._session


def _client(session):
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _FakeRegistry(session))
    return TestClient(app)


def test_non_stream_returns_tool_calls():
    session = _FakeSession([
        'Sure. <tool_call>{"tool": "get_weather", "args": {"city": "Moscow"}}</tool_call>'
    ])
    client = _client(session)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "weather in Moscow?"}],
        "tools": TOOLS_DEF,
    })
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    msg = choice["message"]
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "Moscow"}


def test_continuation_with_tool_result():
    """assistant.tool_calls → tool result → продолжение нормально в _flatten_messages."""
    session = _FakeSession(["Weather in Moscow: +5C"])
    client = _client(session)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "get_weather",
                                          "arguments": '{"city":"Moscow"}'}}]},
            {"role": "tool", "tool_call_id": "call_1",
             "content": '{"temp": 5}'},
        ],
        "tools": TOOLS_DEF,
    })
    assert r.status_code == 200, r.text[:300]
    # Проверим, что history получил все 3 роли
    prompt = session.history[0]
    assert "weather?" in prompt
    assert "get_weather" in prompt or "call_1" in prompt, (
        f"_flatten_messages потерял tool_calls: prompt={prompt[:500]!r}"
    )
    assert "temp" in prompt, f"_flatten_messages потерял tool result: {prompt[:500]!r}"


def test_tool_choice_none_disables():
    session = _FakeSession([
        '<tool_call>{"tool": "get_weather", "args": {"city": "Moscow"}}</tool_call>'
    ])
    client = _client(session)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS_DEF,
        "tool_choice": "none",
    })
    body = r.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    # текст сохранён (без исполнения), tool_calls нет
    assert "tool_calls" not in choice["message"]
    assert choice["message"]["content"] is not None


def test_unknown_tool_ignored():
    session = _FakeSession([
        '<tool_call>{"tool": "malicious", "args": {}}</tool_call>'
    ])
    client = _client(session)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS_DEF,
    })
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "tool_calls" not in choice["message"]


def test_stream_emits_tool_calls():
    """Stream с tool_calls — finish_reason="tool_calls", delta.tool_calls."""
    session = _FakeSession([
        '<tool_call>{"tool": "get_weather", "args": {"city": "Moscow"}}</tool_call>'
    ])
    client = _client(session)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": TOOLS_DEF,
        "stream": True,
    })
    assert r.status_code == 200, r.text[:500]
    chunks = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert chunks[-1].strip() == "data: [DONE]"
    saw_tool_call = False
    saw_finish_tool_calls = False
    for ln in chunks[:-1]:
        obj = json.loads(ln[len("data: "):])
        ch = obj["choices"][0]
        if ch.get("finish_reason") == "tool_calls":
            saw_finish_tool_calls = True
        delta = ch.get("delta") or {}
        if "tool_calls" in delta:
            saw_tool_call = True
            tc = delta["tool_calls"][0]
            assert tc["type"] == "function"
            assert tc["function"]["name"] == "get_weather"
    assert saw_tool_call, f"stream не выдал delta.tool_calls: {chunks[:3]}"
    assert saw_finish_tool_calls, "stream не выдал finish_reason=tool_calls"
