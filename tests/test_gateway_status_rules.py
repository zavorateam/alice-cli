"""§5: implemented недоступен в /v1/models и chat_completions."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class _FakeSession:
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        yield AliceTurnResponse(dialog_id="d", request_id="r", text="ok", is_finished=True)
    async def cancel_current(self):
        return None


class _FakeRegistry:
    async def acquire(self, sid):
        return _FakeSession()


def _client():
    catalog = ModelCatalog(manager_provider=lambda: None)
    app = FastAPI()
    attach_openai_routes(app, catalog, _FakeRegistry())
    return catalog, TestClient(app)


def test_implemented_not_in_models_list():
    catalog, client = _client()
    # все по умолчанию implemented → список пустой
    r = client.get("/v1/models")
    assert r.json()["data"] == []


def test_available_appears():
    catalog, client = _client()
    catalog.mark_available("alice-base", effective_mode="Base")
    r = client.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "alice-base" in ids


def test_unavailable_returns_503():
    catalog, client = _client()
    catalog.mark_unavailable("alice-base", "test failure")
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 503, f"got {r.status_code}: {r.text[:200]}"
    assert "unavailable" in r.text.lower() or "503" in r.text


def test_pro_degraded_when_effective_base():
    catalog, client = _client()
    catalog.mark_available("alice-pro", effective_mode="Base")
    s = catalog.get_state("alice-pro")
    assert s.status == "degraded"
    assert "Base" in (s.reason or "")


def test_pro_available_when_effective_pro():
    catalog, client = _client()
    catalog.mark_available("alice-pro", effective_mode="Pro")
    s = catalog.get_state("alice-pro")
    assert s.status == "available"
