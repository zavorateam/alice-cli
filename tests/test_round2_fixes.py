"""Регрессии round2: lifecycle cleanup, sync failure, MCP EOF shutdown, /v1/models/{id}."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------- §3.1 cleanup структур manager ----------------

def test_on_dialog_closed_cleans_all_structures():
    from tools.manager import AliceAgentManager
    from alice_auth import AliceCredentials

    mgr = AliceAgentManager(AliceCredentials(auth_type="token", token="y0_x"))
    did = "dialog-X"
    mgr.known_dialogs.add(did)
    mgr.ephemeral_dialogs.add(did)
    mgr.dialog_prev_req_ids[did] = "req-prev"
    mgr.active_dialog_id = did

    class _FakeWs:
        async def update_subscriptions(self, *a, **kw):
            return None
    mgr.ws = _FakeWs()

    asyncio.run(mgr._on_dialog_closed(did))
    assert did not in mgr.known_dialogs
    assert did not in mgr.ephemeral_dialogs
    assert did not in mgr.dialog_prev_req_ids


def test_chat_session_close_calls_on_dialog_closed():
    from tools.manager import ChatSession

    calls = []
    class _FakeRpc:
        async def delete_dialog(self, d):
            calls.append(("rpc_delete", d))
    class _FakeMgr:
        rpc = _FakeRpc()
        active_dialog_id = "d1"
        known_dialogs = {"d1"}
        async def _on_dialog_closed(self, d):
            calls.append(("on_closed", d))

    session = ChatSession("sid", _FakeMgr())
    session.dialog_id = "d1"
    asyncio.run(session.close(delete_dialog=True))
    assert ("rpc_delete", "d1") in calls
    assert ("on_closed", "d1") in calls
    assert session.dialog_id is None


# ---------------- §4 sync failure cleanup ----------------

def test_supervise_sync_failure_resets_state():
    """После провала _synchronize_state в supervisor is_connected=False и ws=None."""
    from tools.transport import AliceWsEngine
    from alice_auth import AliceCredentials

    eng = AliceWsEngine(AliceCredentials(auth_type="token", token="y0_x"))

    open_calls = {"n": 0}
    async def fake_open(self):
        open_calls["n"] += 1
        self.is_connected = True
        self.ws = object()
    async def fake_sync(self):
        raise RuntimeError("sync boom")
    async def fake_flush(self):
        return None

    orig = (AliceWsEngine._open, AliceWsEngine._synchronize_state, AliceWsEngine._flush_subscriptions)
    AliceWsEngine._open = fake_open
    AliceWsEngine._synchronize_state = fake_sync
    AliceWsEngine._flush_subscriptions = fake_flush
    try:
        async def run():
            eng._should_run = True
            # Один цикл supervisor с ранним выходом: патчим sleep
            real_sleep = asyncio.sleep
            async def fake_sleep(d, *a, **kw):
                await real_sleep(0)
                eng._should_run = False
            orig_sleep = asyncio.sleep
            # Monkey-patch через модуль
            import tools.transport as tr
            tr_asyncio_sleep = tr.asyncio.sleep
            tr.asyncio.sleep = fake_sleep
            try:
                await eng._supervise()
            finally:
                tr.asyncio.sleep = tr_asyncio_sleep
        asyncio.run(run())
    finally:
        (AliceWsEngine._open, AliceWsEngine._synchronize_state, AliceWsEngine._flush_subscriptions) = orig

    assert open_calls["n"] >= 1
    assert eng.is_connected is False, "sync failure не сбросил is_connected"
    assert eng.ws is None, "sync failure не сбросил ws"


# ---------------- §7 MCP: _shutdown_all при EOF ----------------

def test_run_stdio_shutdown_all_on_eof():
    """EOF в run_stdio → _shutdown_all вызывается, менеджеры закрываются."""
    from tools.manager import AliceMcpServer

    closed = []
    class _Mgr:
        def __init__(self, name):
            self.name = name
        async def shutdown(self):
            closed.append(self.name)

    mcp = AliceMcpServer(manager=None)
    mcp._managers_cache = {"a": _Mgr("A"), "b": _Mgr("B")}
    import time
    mcp._last_used = {"a": time.time(), "b": time.time()}

    # Проверяем наличие _shutdown_all в run_stdio (AST-инвариант)
    src = Path("tools/manager.py").read_text()
    import ast
    tree = ast.parse(src)
    mcp_node = next(n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "AliceMcpServer")
    rs = next(s for s in mcp_node.body
              if isinstance(s, ast.AsyncFunctionDef) and s.name == "run_stdio")
    body = ast.get_source_segment(src, rs)
    assert "_shutdown_all" in body, "run_stdio не вызывает _shutdown_all"
    assert "finally" in body, "run_stdio без try/finally"

    asyncio.run(mcp._shutdown_all())
    assert set(closed) == {"A", "B"}
    assert mcp._managers_cache == {}


# ---------------- §5.3 GET /v1/models/{id} ----------------

def _client_with_status(model_id, status):
    from tools.gateway import ModelCatalog, attach_openai_routes

    class _R:
        async def acquire(self, sid): return None

    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status(model_id, status)
    app = FastAPI()
    attach_openai_routes(app, catalog, _R())
    return TestClient(app)


def test_get_model_by_id_available():
    client = _client_with_status("alice-base", "available")
    r = client.get("/v1/models/alice-base")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "alice-base"
    assert body["object"] == "model"


def test_get_model_by_id_unknown_returns_404():
    client = _client_with_status("alice-base", "available")
    r = client.get("/v1/models/nonexistent")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_get_model_by_id_implemented_still_returns():
    """Diagnostics-подобное поведение: implemented модель доступна через {id}, но
    не через /v1/models. Это контракт: {id} — для отладки, список — для клиентов."""
    from tools.gateway import ModelCatalog, attach_openai_routes
    class _R:
        async def acquire(self, sid): return None
    catalog = ModelCatalog(manager_provider=lambda: None)
    # по умолчанию status="implemented"
    app = FastAPI()
    attach_openai_routes(app, catalog, _R())
    client = TestClient(app)
    r = client.get("/v1/models/alice-expert")
    assert r.status_code == 200
    assert r.json()["alice"]["status"] == "implemented"
