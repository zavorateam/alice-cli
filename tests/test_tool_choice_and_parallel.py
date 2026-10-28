"""§6.2 tool_choice + §6.3 parallel_tool_calls."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes, _extract_tool_calls


TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "w",
                                       "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_time", "description": "t",
                                       "parameters": {"type": "object", "properties": {}}}},
]


class _ScriptedSession:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.calls += 1
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r", text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _Reg:
    def __init__(self, session):
        self._s = session
    async def acquire(self, sid):
        return self._s


def _client(scripted_texts):
    session = _ScriptedSession(scripted_texts)
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _Reg(session))
    return TestClient(app), session


# ---------------- _extract_tool_calls ----------------

def test_extract_filters_to_known_tools():
    text = '<tool_call>{"tool": "unknown", "args": {}}</tool_call>'
    assert _extract_tool_calls(text, TOOLS) == []


def test_extract_forced_function_only():
    text = ('<tool_call>{"tool": "get_weather", "args": {}}</tool_call>'
            '<tool_call>{"tool": "get_time", "args": {}}</tool_call>')
    forced = {"type": "function", "function": {"name": "get_time"}}
    calls = _extract_tool_calls(text, TOOLS, forced)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "get_time"


def test_extract_auto_returns_all():
    text = ('<tool_call>{"tool": "get_weather", "args": {}}</tool_call>'
            '<tool_call>{"tool": "get_time", "args": {}}</tool_call>')
    calls = _extract_tool_calls(text, TOOLS, "auto")
    assert len(calls) == 2


# ---------------- validation ----------------

def test_validation_empty_tools_with_required():
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": "required",
    })
    assert r.status_code == 400
    assert "tools is empty" in r.text


def test_validation_duplicate_function_name():
    client, _ = _client(["x"])
    dup = TOOLS + [TOOLS[0]]
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": dup,
    })
    assert r.status_code == 400
    assert "duplicate" in r.text


def test_validation_unknown_forced_function():
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "nope"}},
    })
    assert r.status_code == 400
    assert "unknown function" in r.text


# ---------------- required: retry ----------------

def test_required_no_call_returns_422_after_retry():
    """Модель дважды ответила текстом — 422 с tool_choice_required_no_call."""
    client, session = _client(["plain text 1", "plain text 2"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": "required",
    })
    assert r.status_code == 422, r.text[:300]
    body = r.json()
    assert body["error"]["type"] == "tool_choice_required_no_call"
    assert session.calls == 2, f"expected 2 sends (initial + retry), got {session.calls}"


def test_required_retry_success():
    """Первая итерация — текст, retry — tool_call → 200, finish_reason=tool_calls."""
    client, session = _client([
        "plain text",
        '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": "required",
    })
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert session.calls == 2


def test_required_first_call_success_no_retry():
    client, session = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": "required",
    })
    assert r.status_code == 200
    assert session.calls == 1  # без retry


# ---------------- parallel_tool_calls=false ----------------

def test_parallel_false_rejects_multiple():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>'
        '<tool_call>{"tool": "get_time", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "parallel_tool_calls": False,
    })
    assert r.status_code == 400
    assert "parallel_tool_calls" in r.text


def test_parallel_true_allows_multiple():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>'
        '<tool_call>{"tool": "get_time", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "parallel_tool_calls": True,
    })
    assert r.status_code == 200
    calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 2


# ---------------- forced function ----------------

def test_forced_function_filters_wrong_call():
    """tool_choice задаёт get_time, модель вызвала get_weather → 0 вызовов."""
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    })
    assert r.status_code == 200
    body = r.json()
    # Не tool_calls, потому что отфильтровано; контент — исходный текст
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in body["choices"][0]["message"]


def test_forced_function_passes_correct_call():
    client, _ = _client([
        '<tool_call>{"tool": "get_time", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    })
    body = r.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_time"


# ---------------- stream + required ----------------

def test_stream_required_no_call_emits_error():
    client, session = _client(["plain text", "plain text again"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": "required",
        "stream": True,
    })
    assert r.status_code == 200  # SSE уже начат
    body = r.text
    # В SSE видим error event с нашим type
    assert "tool_choice_required_no_call" in body
    assert "data: [DONE]" in body


def test_stream_parallel_false_rejects():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>'
        '<tool_call>{"tool": "get_time", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "parallel_tool_calls": False,
        "stream": True,
    })
    assert "parallel_tool_calls_not_supported" in r.text
