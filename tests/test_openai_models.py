from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class FakeManager:
    async def send_message(self, *a, **kw):
        from agents.base import AliceTurnResponse
        yield AliceTurnResponse(dialog_id="d", request_id="r", text="ok", is_finished=True)


def _client(catalog=None):
    if catalog is None:
        catalog = ModelCatalog(manager_provider=lambda: FakeManager())
    app = FastAPI()
    attach_openai_routes(app, catalog, session_registry=None)
    return catalog, TestClient(app)


def test_diagnostics_includes_all_implemented():
    catalog, client = _client()
    r = client.get("/v1/models/diagnostics")
    assert r.status_code == 200
    ids = [m["model_id"] for m in r.json()["data"]]
    assert "alice-pro" in ids
    assert "alice-base" in ids
    assert "alice-expert" in ids
    assert all(m["status"] == "implemented" for m in r.json()["data"])


def test_models_list_empty_before_healthcheck():
    catalog, client = _client()
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_models_list_contains_available_only():
    catalog, client = _client()
    catalog.set_status("alice-base", "available")
    catalog.set_status("alice-pro", "degraded", reason="Pro -> Base")
    catalog.set_status("alice-expert", "unavailable", reason="trial exhausted")
    r = client.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "alice-base" in ids
    assert "alice-pro" in ids
    assert "alice-expert" not in ids


def test_healthcheck_no_effective_marks_degraded():
    import asyncio
    catalog = ModelCatalog(manager_provider=lambda: FakeManager())
    state = asyncio.run(catalog.healthcheck("alice-pro"))
    # FakeManager не кладёт effective → честный degraded, не available
    assert state.status == "degraded"
    assert "no effective settings" in (state.reason or "")
