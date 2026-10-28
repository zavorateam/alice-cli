"""§5.4 previous_response_id + response_format."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes, ResponseStore


class _ScriptedSession:
    def __init__(self, texts):
        self._texts = list(texts)
        self.dialog_id = None
        self.prompts = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.prompts.append(prompt)
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id=self.dialog_id or "d",
                                request_id="r", text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _Reg:
    def __init__(self, session):
        self._s = session
    async def acquire(self, sid):
        return self._s


def _client(texts):
    s = _ScriptedSession(texts)
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _Reg(s))
    return TestClient(app), s


# ---------------- ResponseStore ----------------

def test_response_store_put_get_lru():
    store = ResponseStore(ttl_seconds=10, max_entries=2)
    store.put("r1", "d1", "alice-base", "s1")
    store.put("r2", "d2", "alice-base", "s2")
    assert store.get("r1")["dialog_id"] == "d1"
    # r1 теперь MRU; r2 — oldest
    store.put("r3", "d3", "alice-base", "s3")
    assert store.get("r2") is None, "r2 должен быть вытеснен (LRU)"
    assert store.get("r1") is not None
    assert store.get("r3") is not None


def test_response_store_ttl():
    import time
    store = ResponseStore(ttl_seconds=0.05)
    store.put("r1", "d1", "alice-base", "s1")
    assert store.get("r1") is not None
    time.sleep(0.1)
    assert store.get("r1") is None, "TTL не сработал"


# ---------------- previous_response_id ----------------

def test_previous_response_not_found():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "previous_response_id": "resp_does_not_exist",
    })
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "previous_response_not_found"


def test_previous_response_id_continues_dialog():
    """После 1-го ответа передаём previous_response_id → тот же dialog_id."""
    client, session = _client(["first answer", "second answer"])

    r1 = client.post("/v1/responses", json={
        "model": "alice-base", "input": "first",
        "store": True,
    })
    assert r1.status_code == 200
    rid = r1.json()["id"]
    assert rid.startswith("resp_")

    r2 = client.post("/v1/responses", json={
        "model": "alice-base", "input": "second",
        "previous_response_id": rid,
    })
    assert r2.status_code == 200, r2.text[:400]


def test_store_false_no_save():
    client, _ = _client(["answer"])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "store": False,
    })
    assert r.status_code == 200
    rid = r.json()["id"]
    # Этого response_id в store нет — значит previous_response_id → 400
    r2 = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "previous_response_id": rid,
    })
    assert r2.status_code == 400
    assert r2.json()["error"]["type"] == "previous_response_not_found"


# ---------------- response_format ----------------

def test_response_format_text_no_op():
    client, session = _client(["x"])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "response_format": {"type": "text"},
    })
    assert r.status_code == 200
    assert "answer ONLY" not in session.prompts[0]


def test_response_format_json_object():
    client, session = _client(['{"ok": true}'])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "give json",
        "response_format": {"type": "json_object"},
    })
    assert r.status_code == 200
    assert "valid JSON" in session.prompts[0]


def test_response_format_json_schema():
    client, session = _client(['{"a": 1}'])
    schema = {"type": "object", "properties": {"a": {"type": "integer"}},
              "required": ["a"]}
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "give json",
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "X", "schema": schema, "strict": True},
        },
    })
    assert r.status_code == 200
    p = session.prompts[0]
    assert "Schema" in p
    assert '"type": "object"' in p
    assert "strictly valid" in p


def test_response_format_unknown_type_400():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "response_format": {"type": "xml"},
    })
    assert r.status_code == 400
    assert "not supported" in r.text


def test_response_format_json_schema_missing_schema():
    client, _ = _client([])
    r = client.post("/v1/responses", json={
        "model": "alice-base", "input": "hi",
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "X"}},
    })
    assert r.status_code == 400
    assert "schema" in r.text.lower()
