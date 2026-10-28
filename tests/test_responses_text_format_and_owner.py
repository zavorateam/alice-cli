"""Iteration 4a: text.format для Responses + previous_response_id owner check."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class _S:
    def __init__(self, texts):
        self._texts = list(texts)
        self.dialog_id = "d"
        self._active = 0
        self.prompts = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.prompts.append(prompt)
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


# ---------- text.format ----------

def test_text_format_text():
    client, session = _client(["ok"])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "text": {"format": {"type": "text"}},
    })
    assert r.status_code == 200
    assert "valid JSON" not in session.prompts[0]


def test_text_format_json_object():
    client, session = _client(['{"a": 1}'])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "text": {"format": {"type": "json_object"}},
    })
    assert r.status_code == 200
    assert "valid JSON" in session.prompts[0]


def test_text_format_json_schema():
    client, session = _client(['{"temp": 5}'])
    schema = {
        "type": "object",
        "properties": {"temp": {"type": "integer"}},
        "required": ["temp"],
    }
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "weather",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "weather",
                "schema": schema,
                "strict": True,
            },
        },
    })
    assert r.status_code == 200
    p = session.prompts[0]
    assert "Schema" in p
    assert '"type": "object"' in p
    assert "strictly valid" in p


def test_text_format_json_schema_missing_schema():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "text": {"format": {"type": "json_schema", "name": "x"}},
    })
    assert r.status_code == 400
    assert "schema" in r.text.lower()


def test_text_format_unknown_type():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "text": {"format": {"type": "xml"}},
    })
    assert r.status_code == 400
    assert "not supported" in r.text


def test_text_field_wrong_type():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "text": "not-an-object",
    })
    assert r.status_code == 400


def test_chat_style_response_format_still_works():
    """Для совместимости с Chat-style response_format в Responses endpoint."""
    client, session = _client(['{"x": 1}'])
    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "response_format": {"type": "json_object"},
    })
    assert r.status_code == 200
    assert "valid JSON" in session.prompts[0]


# ---------- previous_response_id owner check ----------

def test_previous_response_with_matching_session_ok():
    client, _ = _client(["first", "second"])
    r1 = client.post("/v1/responses",
                     headers={"X-Alice-Session-ID": "S1"},
                     json={"model": "alice-base", "input": "hi", "store": True})
    assert r1.status_code == 200
    rid = r1.json()["id"]

    r2 = client.post("/v1/responses",
                     headers={"X-Alice-Session-ID": "S1"},
                     json={"model": "alice-base", "input": "next",
                           "previous_response_id": rid})
    assert r2.status_code == 200, r2.text[:300]


def test_previous_response_wrong_session_403():
    """Клиент B не может продолжить response клиента A."""
    client, _ = _client(["first", "should-not-be-called"])
    r1 = client.post("/v1/responses",
                     headers={"X-Alice-Session-ID": "S-A"},
                     json={"model": "alice-base", "input": "hi", "store": True})
    rid = r1.json()["id"]

    r2 = client.post("/v1/responses",
                     headers={"X-Alice-Session-ID": "S-B"},
                     json={"model": "alice-base", "input": "steal",
                           "previous_response_id": rid})
    assert r2.status_code == 403, r2.text[:300]
    assert r2.json()["error"]["type"] == "previous_response_not_owned"


def test_previous_response_no_session_in_current_uses_prev():
    """Если клиент не передал X-Alice-Session-ID — восстанавливаем из store.

    Это легально: клиент хранит response_id как capability, если он его знает —
    он и есть владелец. X-Alice-Session-ID в этом случае не нужен.
    """
    client, _ = _client(["first", "second"])
    r1 = client.post("/v1/responses",
                     headers={"X-Alice-Session-ID": "S-X"},
                     json={"model": "alice-base", "input": "hi", "store": True})
    rid = r1.json()["id"]

    r2 = client.post("/v1/responses",
                     json={"model": "alice-base", "input": "next",
                           "previous_response_id": rid})
    assert r2.status_code == 200, r2.text[:300]
