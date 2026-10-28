"""Session conflict → 409 (Chat) / response.failed (Responses stream).

TestClient (Starlette) использует единый anyio.BlockingPortal — два
client.post из разных потоков сериализуются, не давая настоящей
конкуренции. Поэтому здесь — реальный uvicorn на свободном порту,
клиенты через httpx.
"""
import asyncio
import socket
import threading
import time

import pytest
import httpx
import uvicorn
from fastapi import FastAPI

from tools.gateway import ModelCatalog, attach_openai_routes


class _SlowSession:
    """Fake AliceAgentManager: все методы, которые ChatSession вызывает."""
    active_dialog_id = "d"
    dialog_id = "d"
    def __init__(self):
        self._dialog_seq = 0

    async def create_new_dialog(self, agent_id="pro", is_ephemeral=False):
        self._dialog_seq += 1
        d = f"d-{self._dialog_seq}"
        self.active_dialog_id = d
        self.dialog_id = d
        return d

    async def send_message(self, text, agent_id="pro", dialog_id=None,
                            is_suggest=False, attachments=None):
        from agents.base import AliceTurnResponse
        await asyncio.sleep(0.25)
        yield AliceTurnResponse(
            dialog_id=dialog_id or self.dialog_id,
            request_id="r",
            text="ok",
            is_finished=True,
        )

    async def _on_dialog_closed(self, dialog_id):
        return None

    @property
    def rpc(self):
        class _Rpc:
            async def delete_dialog(self, *a, **kw):
                return (200, "ok")
        return _Rpc()

    @property
    def ws(self):
        # ChatSession.cancel_current обращается к ws._pending_responses
        class _Ws:
            _pending_responses = {}
        return _Ws()


def _start_server():
    """Поднимает uvicorn на свободном порту, возвращает (port, stop_fn).

    Используем настоящий SessionRegistry с manager_factory → _SlowSession.
    SessionRegistry создаёт ChatSession(sid, mgr) и хранит её в _sessions[sid].
    Второй acquire("S1") вернёт тот же ChatSession → его _active защищает.
    """
    from tools.manager import SessionRegistry

    session = _SlowSession()
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")

    async def _factory():
        return session

    registry = SessionRegistry(manager_factory=_factory)

    app = FastAPI()
    attach_openai_routes(app, catalog, registry)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()

    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn не поднялся")

    def stop():
        server.should_exit = True
        thread.join(timeout=5)

    return port, stop


def _parallel_post(port, path, body, headers, delay=0.06):
    """Два httpx-запроса параллельно. Возвращает [(status, body), ...]."""
    results = [None, None]

    def _do(idx):
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(f"http://127.0.0.1:{port}{path}",
                           json=body, headers=headers)
                results[idx] = (r.status_code, r.text)
        except Exception as e:
            results[idx] = ("exc", repr(e))

    t1 = threading.Thread(target=_do, args=(0,))
    t1.start()
    time.sleep(delay)  # первый гарантированно вошёл в handler
    t2 = threading.Thread(target=_do, args=(1,))
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    return results


# ---------- Chat ----------

def test_chat_second_concurrent_request_409():
    """Два параллельных Chat-запроса к одной сессии → [200, 409]."""
    port, stop = _start_server()
    try:
        results = _parallel_post(port, "/v1/chat/completions",
                                 body={"model": "alice-base",
                                       "messages": [{"role": "user",
                                                     "content": "hi"}]},
                                 headers={"X-Alice-Session-ID": "S1"})
    finally:
        stop()

    codes = sorted(s for s, _ in results)
    assert codes == [200, 409], f"got {codes}: {results}"
    conflict = next(b for s, b in results if s == 409)
    assert "session_conflict" in conflict


# ---------- Responses ----------

def test_responses_second_concurrent_409():
    port, stop = _start_server()
    try:
        results = _parallel_post(port, "/v1/responses",
                                 body={"model": "alice-base",
                                       "input": "hi"},
                                 headers={"X-Alice-Session-ID": "S1"})
    finally:
        stop()

    codes = sorted(s for s, _ in results)
    assert codes == [200, 409], f"got {codes}: {results}"
    conflict = next(b for s, b in results if s == 409)
    assert "session_conflict" in conflict
