"""End-to-end: create_curl_http_app + attach_openai_routes в одной FastAPI-сборке.

Проверяет, что /chat (старый) и /v1/chat/completions (новый) сосуществуют,
и что реальный create_curl_http_app не конфликтует с роутами gateway.
"""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from alice_server import create_curl_http_app
from tools.gateway import ModelCatalog, attach_openai_routes


class FakeManager:
    """Минимальный manager для /chat и /v1/chat/completions."""
    def __init__(self):
        self.active_dialog_id = "dialog-test"

    async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
        from agents.base import AliceTurnResponse
        yield AliceTurnResponse(
            dialog_id=dialog_id or self.active_dialog_id,
            request_id="r1",
            text=f"echo:{text}",
            is_finished=True,
        )


class FakeSession:
    def __init__(self, manager):
        self.manager = manager

    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        async for t in self.manager.send_message(prompt, agent_id=agent_id):
            yield t

    async def cancel_current(self):
        return None


class FakeRegistry:
    def __init__(self, manager):
        self.manager = manager

    async def acquire(self, sid):
        return FakeSession(self.manager)


def _build_app():
    manager = FakeManager()
    catalog = ModelCatalog(manager_provider=lambda: manager)
    catalog.set_status("alice-base", "available")
    app = create_curl_http_app(manager)
    attach_openai_routes(app, catalog, FakeRegistry(manager))
    return app, catalog


def test_old_chat_endpoint_still_works():
    app, _ = _build_app()
    client = TestClient(app)
    r = client.post("/chat", content="hello")
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
    assert r.text.strip() == "echo:hello"


def test_new_v1_chat_completions_works():
    app, _ = _build_app()
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "echo:hi"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_models_endpoint_visible():
    app, _ = _build_app()
    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "alice-base" in ids


def test_route_registry_no_conflict():
    """GET/POST /v1/* не должны перекрывать /chat."""
    app, _ = _build_app()
    paths = [(getattr(r, "path", None), tuple(getattr(r, "methods", None) or ())) for r in app.router.routes]
    chat_paths = [p for p, m in paths if p == "/chat"]
    v1_paths = [p for p, m in paths if p and p.startswith("/v1/")]
    assert "/chat" in chat_paths
    assert "/v1/chat/completions" in v1_paths
    assert "/v1/models" in v1_paths


def test_stream_end_to_end():
    app, _ = _build_app()
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
    chunks = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert chunks[-1].strip() == "data: [DONE]"
    import json as _j
    deltas = []
    for ln in chunks[:-1]:
        obj = _j.loads(ln[len("data: "):])
        d = obj["choices"][0]["delta"]
        if "content" in d and d["content"]:
            deltas.append(d["content"])
    assert "".join(deltas) == "echo:hi"
