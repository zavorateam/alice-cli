"""Iteration 4b: warm_up awaited, shutdown order."""
import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


# ---------- warm_up ----------

class _HealthSession:
    dialog_id = "d"
    _active = 0
    def __init__(self):
        self.calls = []
    async def create_new_dialog(self, agent_id="pro", is_ephemeral=False):
        return f"hc-{agent_id}"
    async def send_message(self, text, agent_id="pro", dialog_id=None,
                            is_suggest=False):
        from agents.base import AliceTurnResponse
        self.calls.append((agent_id, text))
        # Возвращаем "ok" и effective_mode = Base для Pro (симуляция degraded)
        eff = type("E", (), {"effective_mode": "Base",
                             "effective_preset": None,
                             "trial_left": None,
                             "trial_limit": None,
                             "degraded": agent_id == "pro",
                             "requested_mode": "Pro" if agent_id == "pro" else "Base"})()
        yield AliceTurnResponse(dialog_id=dialog_id or "d",
                                request_id="r",
                                text="ok",
                                is_finished=True,
                                effective=eff)
    async def _on_dialog_closed(self, d): pass
    async def shutdown(self): pass
    @property
    def rpc(self):
        class _Rpc:
            async def delete_dialog(self, *a, **kw): return (200, "")
        return _Rpc()


def test_warm_up_is_async_coroutine():
    """warm_up должен быть корутиной, а не возвращать Task."""
    import inspect
    from tools.gateway import ModelCatalog
    assert inspect.iscoroutinefunction(ModelCatalog.warm_up), (
        "warm_up должен быть `async def` — иначе await catalog.warm_up() падает"
    )


def test_warm_up_awaits_both_base_and_pro():
    s = _HealthSession()
    cat = ModelCatalog(manager_provider=lambda: s)

    async def run():
        await cat.warm_up()
    asyncio.run(run())

    # base available, pro degraded
    base = cat.get_state("alice-base")
    pro = cat.get_state("alice-pro")
    assert base.status == "available"
    assert pro.status == "degraded", f"pro.status={pro.status}"
    # healthcheck вызвал send_message для base и pro
    agents = [c[0] for c in s.calls]
    assert "base" in agents
    assert "pro" in agents


def test_warm_up_completes_before_serve():
    """После await warm_up каталог готов — /v1/models не пуст."""
    s = _HealthSession()
    cat = ModelCatalog(manager_provider=lambda: s)

    async def run():
        await cat.warm_up()
    asyncio.run(run())

    app = FastAPI()
    class _R:
        async def acquire(self, sid): return s
    attach_openai_routes(app, cat, _R())
    client = TestClient(app)
    r = client.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert "alice-base" in ids
    assert "alice-pro" in ids


def test_warm_up_errors_do_not_raise():
    """Ошибка healthcheck не должна валить warm_up."""
    class _FailSession:
        dialog_id = "d"
        _active = 0
        async def create_new_dialog(self, **kw): return "d"
        async def send_message(self, *a, **kw):
            raise RuntimeError("boom")
            yield
        @property
        def rpc(self):
            class _Rpc:
                async def delete_dialog(self, *a, **kw): return (0, "")
            return _Rpc()
    cat = ModelCatalog(manager_provider=lambda: _FailSession())

    async def run():
        await cat.warm_up()  # не должно бросить
    asyncio.run(run())
    assert cat.get_state("alice-base").status == "unavailable"


# ---------- shutdown order ----------

class _OrderedManager:
    """Fake manager, записывает порядок вызовов shutdown/close."""
    def __init__(self, events):
        self._events = events
    async def shutdown(self):
        self._events.append("manager.shutdown")


class _OrderedChatSession:
    def __init__(self, sid, events):
        self.session_id = sid
        self._events = events
    async def close(self, delete_dialog=True):
        self._events.append("chat.close")


def test_shutdown_order_registry_then_manager():
    """SessionRegistry.close() должен идти до manager.shutdown().

    Иначе manager.shutdown() удалит ephemeral-диалоги, а ChatSession.close()
    потом попытается обратиться к уже удалённым dialog_id через RPC.
    """
    from tools.manager import SessionRegistry

    events = []
    mgr = _OrderedManager(events)

    async def factory():
        return mgr

    reg = SessionRegistry(manager_factory=factory)

    # Вручную добавим ChatSession в registry
    reg._sessions["S1"] = _OrderedChatSession("S1", events)
    reg._last_used["S1"] = time.time()

    async def run_shutdown():
        await reg.close()
        await mgr.shutdown()

    asyncio.run(run_shutdown())

    assert events == ["chat.close", "manager.shutdown"], (
        f"неверный порядок shutdown: {events}"
    )
