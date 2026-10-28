"""§5.4 Responses API — минимальный рабочий адаптер."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


TOOLS_CHAT = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "w",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }},
]
TOOLS_RESP = [
    {"type": "function", "name": "get_weather", "description": "w",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
]


class _ScriptedSession:
    def __init__(self, texts):
        self._texts = list(texts)
        self.prompts = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.prompts.append(prompt)
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r", text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _Reg:
    def __init__(self, session):
        self._s = session
    async def acquire(self, sid):
        return self._s


def _client(texts, status="available"):
    s = _ScriptedSession(texts)
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", status)
    app = FastAPI()
    attach_openai_routes(app, catalog, _Reg(s))
    return TestClient(app), s


# ---------------- basic text ----------------

def test_responses_input_string():
    client, session = _client(["Привет"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hello",
    })
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    # output: один message с output_text
    assert len(body["output"]) == 1
    item = body["output"][0]
    assert item["type"] == "message"
    assert item["role"] == "assistant"
    assert item["content"][0]["type"] == "output_text"
    assert item["content"][0]["text"] == "Привет"
    # prompt был ровно "hello"
    assert session.prompts[0] == "hello"


def test_responses_instructions_prepended():
    client, session = _client(["ok"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hello",
        "instructions": "Be brief",
    })
    assert r.status_code == 200
    assert session.prompts[0].startswith("[system] Be brief")
    assert "hello" in session.prompts[0]


def test_responses_input_items():
    client, session = _client(["ok"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
        ],
    })
    assert r.status_code == 200
    assert "hi" in session.prompts[0]


def test_responses_function_call_output_item():
    client, session = _client(["after tool"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "weather?"}]},
            {"type": "function_call_output", "call_id": "call_1",
             "output": '{"temp": 5}'},
        ],
    })
    assert r.status_code == 200, r.text[:300]
    assert "tool_output id=call_1" in session.prompts[0]
    assert "temp" in session.prompts[0]


def test_responses_unsupported_input_type():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": [{"type": "image", "url": "http://x"}],
    })
    assert r.status_code == 400
    assert "not supported" in r.text


# ---------------- validation ----------------

def test_responses_missing_model():
    client, _ = _client([])
    r = client.post("/v1/responses", json={"input": "x"})
    assert r.status_code == 400
    assert "model is required" in r.text


def test_responses_unknown_model():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-unknown", "input": "x",
    })
    assert r.status_code == 404


def test_responses_implemented_model_rejected():
    client, _ = _client([])
    from tools.gateway import ModelCatalog, attach_openai_routes
    catalog = ModelCatalog(manager_provider=lambda: None)
    # implemented by default
    app = FastAPI()
    attach_openai_routes(app, catalog, _Reg(_ScriptedSession(["x"])))
    client = TestClient(app)
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "x",
    })
    assert r.status_code == 503
    assert "verified" in r.text


def test_responses_invalid_json():
    client, _ = _client([])
    r = client.post("/v1/responses", content="not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "invalid JSON" in r.text


# ---------------- function calling non-stream ----------------

def test_responses_function_call_output():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>',
    ])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "weather?",
        "tools": TOOLS_RESP,
    })
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    fc_items = [it for it in body["output"] if it["type"] == "function_call"]
    assert len(fc_items) == 1
    fc = fc_items[0]
    assert fc["name"] == "get_weather"
    assert fc["call_id"].startswith("call_")
    assert fc["id"].startswith("fc_")
    assert json.loads(fc["arguments"]) == {"city": "M"}


def test_responses_chat_style_tools_accepted():
    """Клиент передаёт tools в Chat-формате (function wrapper) — должно работать."""
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>',
    ])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "weather?",
        "tools": TOOLS_CHAT,
    })
    assert r.status_code == 200, r.text[:400]
    fc = [it for it in r.json()["output"] if it["type"] == "function_call"]
    assert len(fc) == 1
    assert fc[0]["name"] == "get_weather"


# ---------------- stream ----------------

def test_responses_stream_events():
    client, _ = _client(["Hello"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "stream": True,
    })
    assert r.status_code == 200
    # SSE-события
    events = []
    for chunk in r.text.split("\n\n"):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        evt_type = None
        data = None
        for line in lines:
            if line.startswith("event: "):
                evt_type = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if evt_type and data is not None:
            events.append((evt_type, data))

    types = [t for t, _ in events]
    assert "response.created" in types
    assert "response.output_item.added" in types
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.completed" in types

    # delta содержит текст
    deltas = [d["delta"] for t, d in events if t == "response.output_text.delta"]
    assert "".join(deltas) == "Hello"

    # final completed
    done = [d for t, d in events if t == "response.completed"]
    assert done[-1]["response"]["status"] == "completed"


def test_responses_stream_function_call_events():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>',
    ])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "weather?",
        "tools": TOOLS_RESP,
        "stream": True,
    })
    assert r.status_code == 200
    body = r.text
    assert "response.function_call_arguments.done" in body
    # call_id в payload
    assert '"call_id": "call_' in body
    assert "response.completed" in body


def test_responses_stream_upstream_error():
    class _FailSession:
        async def send_message(self, *a, **kw):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    error="boom", is_finished=True)
        async def cancel_current(self):
            return None
    class _FailReg:
        async def acquire(self, sid): return _FailSession()

    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _FailReg())
    client = TestClient(app)
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "x", "stream": True,
    })
    assert r.status_code == 200
    # Upstream error → response.failed (не completed/failed)
    assert "response.failed" in r.text
    assert "response.completed" not in r.text
