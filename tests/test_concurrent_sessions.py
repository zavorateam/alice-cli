"""Две сессии не смешивают историю и dialog_id.

Использует реальный SessionRegistry — там sid=None превращается в
новый uuid, что и является контрактом 'без заголовка → изолированная сессия'.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes
from tools.manager import SessionRegistry


class _FakeMgr:
    def __init__(self, sid):
        self.sid = sid
        self.history = []
        self.active_dialog_id = f"dialog-{sid}"

    async def create_new_dialog(self, agent_id="pro", is_ephemeral=False):
        return self.active_dialog_id

    async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
        from agents.base import AliceTurnResponse
        self.history.append(text)
        yield AliceTurnResponse(
            dialog_id=dialog_id or self.active_dialog_id,
            request_id=f"r{len(self.history)}",
            text=f"{self.sid}:{len(self.history)}:{text[-20:]}",
            is_finished=True,
        )

    async def rpc_delete_dialog(self, *a, **kw):
        return None

    @property
    def rpc(self):
        class _R:
            async def delete_dialog(self, *a, **kw): return None
        return _R()


class _TrackingRegistry(SessionRegistry):
    """SessionRegistry, который создаёт _FakeMgr и запоминает их по sid."""
    def __init__(self):
        self.created = {}
        super().__init__(manager_factory=self._make)

    async def _make(self):
        sid = f"mgr-{len(self.created)}"
        m = _FakeMgr(sid)
        self.created[sid] = m
        return m


def _app(registry):
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, registry)
    return TestClient(app)


def test_two_sessions_isolated():
    reg = _TrackingRegistry()
    client = _app(reg)

    r1 = client.post("/v1/chat/completions",
                     headers={"X-Alice-Session-ID": "S1"},
                     json={"model": "alice-base",
                           "messages": [{"role": "user", "content": "first"}]})
    r2 = client.post("/v1/chat/completions",
                     headers={"X-Alice-Session-ID": "S2"},
                     json={"model": "alice-base",
                           "messages": [{"role": "user", "content": "second"}]})
    r1b = client.post("/v1/chat/completions",
                      headers={"X-Alice-Session-ID": "S1"},
                      json={"model": "alice-base",
                            "messages": [{"role": "user", "content": "third"}]})

    s1 = reg._sessions["S1"].manager
    s2 = reg._sessions["S2"].manager
    assert s1.history == ["first", "third"]
    assert s2.history == ["second"]
    assert s1 is not s2


def test_no_header_creates_isolated_session():
    """Без X-Alice-Session-ID каждый запрос = новая сессия (uuid генерируется)."""
    reg = _TrackingRegistry()
    client = _app(reg)
    for _ in range(3):
        r = client.post("/v1/chat/completions",
                        json={"model": "alice-base",
                              "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 200
    # 3 разных sid (uuid4) → 3 сессии, 3 manager
    assert len(reg._sessions) == 3
    assert len(reg.created) == 3
