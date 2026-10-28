"""Iteration 3: JSON Schema validation tool arguments."""
import json

import pytest

jsonschema = pytest.importorskip("jsonschema")  # skip если не установлен

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import (
    ModelCatalog, attach_openai_routes,
    _validate_tool_args, _validate_tool_calls_args, _validate_tool_request,
)


class _S:
    def __init__(self, texts):
        self._texts = list(texts)
        self.dialog_id = "d"
        self._active = 0
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r",
                                text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _R:
    def __init__(self, s): self._s = s
    async def acquire(self, sid): return self._s


def _client(texts):
    s = _S(texts)
    c = ModelCatalog(manager_provider=lambda: None)
    c.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, c, _R(s))
    return TestClient(app), s


SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "units": {"type": "string", "enum": ["c", "f"]},
        "days": {"type": "integer", "minimum": 1, "maximum": 14},
    },
    "required": ["city"],
    "additionalProperties": False,
}

TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "W",
    "parameters": SCHEMA,
}}


# ---------- _validate_tool_args ----------

def test_valid_args():
    parsed, err = _validate_tool_args('{"city": "Moscow", "units": "c"}', SCHEMA)
    assert err is None
    assert parsed["city"] == "Moscow"


def test_invalid_json():
    parsed, err = _validate_tool_args('{bad json', SCHEMA)
    assert err is not None
    assert "invalid JSON" in err


def test_missing_required():
    parsed, err = _validate_tool_args('{"units": "c"}', SCHEMA)
    assert err is not None
    assert "city" in err


def test_wrong_type():
    parsed, err = _validate_tool_args('{"city": 42}', SCHEMA)
    assert err is not None
    assert "type" in err.lower()


def test_enum_violation():
    parsed, err = _validate_tool_args('{"city": "M", "units": "k"}', SCHEMA)
    assert err is not None


def test_additional_properties_false():
    parsed, err = _validate_tool_args(
        '{"city": "M", "extra_field": "x"}', SCHEMA)
    assert err is not None


def test_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        "required": ["user"],
    }
    p, err = _validate_tool_args('{"user": {"name": "Alice"}}', schema)
    assert err is None
    p, err = _validate_tool_args('{"user": {}}', schema)
    assert err is not None


def test_arrays():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["tags"],
    }
    p, err = _validate_tool_args('{"tags": ["a", "b"]}', schema)
    assert err is None
    p, err = _validate_tool_args('{"tags": ["a", 42]}', schema)
    assert err is not None


def test_empty_schema_skips_validation():
    parsed, err = _validate_tool_args('{"anything": 1}', {})
    assert err is None
    assert parsed == {"anything": 1}


# ---------- _validate_tool_request schema well-formedness ----------

def test_invalid_json_schema_rejected():
    bad_tool = {"type": "function", "function": {
        "name": "x",
        "parameters": {"type": "not-a-real-type"},  # невалидный type
    }}
    err = _validate_tool_request([bad_tool], "auto", None)
    assert err is not None
    assert "JSON Schema" in err["error"]["message"]


def test_valid_schema_accepted():
    err = _validate_tool_request([TOOL], "auto", None)
    assert err is None


# ---------- End-to-end: valid args → tool_calls ----------

def test_chat_valid_args_returns_tool_calls():
    text = '<tool_call>{"tool": "get_weather", "args": {"city": "M", "units": "c"}}</tool_call>'
    client, _ = _client([text])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "w?"}],
        "tools": [TOOL],
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"


# ---------- End-to-end: invalid args → retry → success ----------

def test_chat_invalid_args_retry_success():
    bad = '<tool_call>{"tool": "get_weather", "args": {"city": 42}}</tool_call>'
    good = '<tool_call>{"tool": "get_weather", "args": {"city": "M"}}</tool_call>'
    client, session = _client([bad, good])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "w?"}],
        "tools": [TOOL],
    })
    assert r.status_code == 200, r.text[:300]
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"


# ---------- End-to-end: invalid args → retry → still invalid → 422 ----------

def test_chat_invalid_args_retry_fail_422():
    bad1 = '<tool_call>{"tool": "get_weather", "args": {"city": 42}}</tool_call>'
    bad2 = '<tool_call>{"tool": "get_weather", "args": {"city": 42}}</tool_call>'
    client, session = _client([bad1, bad2])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "w?"}],
        "tools": [TOOL],
    })
    assert r.status_code == 422, r.text[:300]
    assert r.json()["error"]["type"] == "tool_arguments_invalid"


# ---------- Responses non-stream: invalid → 422 ----------

def test_responses_non_stream_invalid_args_422():
    bad = '<tool_call>{"tool": "get_weather", "args": {"units": "k"}}</tool_call>'
    client, _ = _client([bad, bad])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "w?",
        "tools": [{"type": "function", "name": "get_weather",
                   "parameters": SCHEMA}],
    })
    assert r.status_code == 422, r.text[:300]
    assert r.json()["error"]["type"] == "tool_arguments_invalid"


# ---------- Chat stream: invalid → SSE error ----------

def test_chat_stream_invalid_args_sse_error():
    bad = '<tool_call>{"tool": "get_weather", "args": {"units": "k"}}</tool_call>'
    client, _ = _client([bad])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "w?"}],
        "tools": [TOOL],
        "stream": True,
    })
    assert r.status_code == 200
    assert "tool_arguments_invalid" in r.text
    assert "data: [DONE]" in r.text


# ---------- Responses stream: invalid → response.failed ----------

def test_responses_stream_invalid_args_failed():
    bad = '<tool_call>{"tool": "get_weather", "args": {"units": "k"}}</tool_call>'
    client, _ = _client([bad])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "w?",
        "tools": [{"type": "function", "name": "get_weather",
                   "parameters": SCHEMA}],
        "stream": True,
    })
    assert r.status_code == 200
    assert "response.failed" in r.text
    assert "tool_arguments_invalid" in r.text


# ---------- jsonschema установлен ----------

def test_jsonschema_available():
    """В окружении должен быть jsonschema — он в requirements.txt."""
    import jsonschema as _j
    assert _j is not None
