"""Отмена стрима: сервер не должен висеть после disconnect, и is_finished → закрытие."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class SlowSession:
    def __init__(self):
        self.cancelled = False
        self.chunks_sent = 0

    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        for i in range(50):
            if self.cancelled:
                return
            self.chunks_sent += 1
            yield AliceTurnResponse(
                dialog_id="d", request_id=f"r{i}",
                text="x" * (i + 1),
                is_finished=(i == 49),
            )
            await asyncio.sleep(0.01)

    async def cancel_current(self):
        self.cancelled = True


class Reg:
    def __init__(self, s):
        self.s = s
    async def acquire(self, sid):
        return self.s


def test_stream_closes_on_is_finished():
    session = SlowSession()
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, Reg(session))
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
    assert session.chunks_sent == 50
    assert not session.cancelled
