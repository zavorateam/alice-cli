from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class FakeSession:
    def __init__(self, texts):
        self._texts = texts

    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        for i, t in enumerate(self._texts):
            yield AliceTurnResponse(
                dialog_id="d", request_id=f"r{i}",
                text=t, is_finished=(i == len(self._texts) - 1),
            )

    async def cancel_current(self):
        return None


class FakeSessionRegistry:
    def __init__(self, texts):
        self._texts = texts

    async def acquire(self, sid):
        return FakeSession(self._texts)


def _client(texts, status="available"):
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", status)
    app = FastAPI()
    attach_openai_routes(app, catalog, FakeSessionRegistry(texts))
    return TestClient(app), app


def test_route_registered_as_raw():
    """POST /v1/chat/completions зарегистрирован (Starlette Route, не APIRoute)."""
    _, app = _client([])
    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert "/v1/chat/completions" in paths


def test_non_stream():
    client, _ = _client(["Hello", "Hello world"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello world"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_stream_deltas_and_done():
    client, _ = _client(["Hel", "Hello world"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
    chunks = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert chunks[-1].strip() == "data: [DONE]"
    contents = []
    for ln in chunks[:-1]:
        payload = ln[len("data: "):]
        import json as _j
        obj = _j.loads(payload)
        delta = obj["choices"][0]["delta"]
        if "content" in delta and delta["content"]:
            contents.append(delta["content"])
    assert contents == ["Hel", "lo world"]


def test_stream_finish_reason_stop():
    client, _ = _client(["hello"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:500]}"
    chunks = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    import json as _j
    finals = [_j.loads(ln[len("data: "):]) for ln in chunks[:-1]]
    finishes = [f["choices"][0]["finish_reason"] for f in finals if f["choices"][0]["finish_reason"]]
    assert finishes == ["stop"]


def test_unknown_model_returns_404():
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-unknown",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 404, f"got {r.status_code}: {r.text[:500]}"
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_missing_model_returns_400():
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:500]}"


def test_old_chat_endpoint_still_works():
    from fastapi.responses import PlainTextResponse

    app = FastAPI()

    @app.post("/chat")
    async def chat():
        return PlainTextResponse("ok\n")

    client = TestClient(app)
    r = client.post("/chat")
    assert r.status_code == 200
    assert r.text == "ok\n"
