"""Bug: cancel_current + MCP _managers_cache eviction."""
import asyncio
import time
from pathlib import Path

import pytest


# ---------------- cancel_current ----------------

def test_cancel_current_wakes_pending_request():
    """cancel_current кладёт error в очередь, чтобы send_text_input вышел."""
    from agents.base import AliceTurnResponse
    from tools.manager import ChatSession

    class _FakeWs:
        def __init__(self):
            self._pending_responses = {}
            self.req_id = "req-1"

    class _FakeMgr:
        def __init__(self):
            self.ws = _FakeWs()
            self.active_dialog_id = "d1"

    mgr = _FakeMgr()
    session = ChatSession("sid", mgr)
    session.dialog_id = "d1"
    session._current_req_id = "req-1"

    queue = asyncio.Queue()
    mgr.ws._pending_responses["req-1"] = queue

    asyncio.run(session.cancel_current())

    assert not queue.empty(), "cancel_current не положил error в очередь"
    turn = queue.get_nowait()
    assert turn.error == "cancelled by client"
    assert turn.is_finished is True


def test_cancel_current_noop_without_request():
    from tools.manager import ChatSession

    class _FakeWs:
        _pending_responses = {}

    class _FakeMgr:
        ws = _FakeWs()
        active_dialog_id = "d1"

    session = ChatSession("sid", _FakeMgr())
    # Нет current_request_id — no-op, не падает
    asyncio.run(session.cancel_current())


def test_cancel_current_noop_when_queue_missing():
    from tools.manager import ChatSession

    class _FakeWs:
        _pending_responses = {}

    class _FakeMgr:
        ws = _FakeWs()
        active_dialog_id = "d1"

    session = ChatSession("sid", _FakeMgr())
    session._current_req_id = "req-missing"
    asyncio.run(session.cancel_current())  # не падает


def test_send_text_input_finally_clears_all_after_cancel():
    """После cancel_current pending_responses и другие словари очищаются."""
    from agents.base import AliceTurnResponse
    from tools.manager import ChatSession

    queue = asyncio.Queue()

    class _FakeWs:
        def __init__(self):
            self._pending_responses = {"req-1": queue}
            self._active_agents_by_req = {"req-1": object()}
            self.requested_mode_by_req = {"req-1": "Pro"}
            self.effective_by_req = {"req-1": object()}

    class _FakeMgr:
        def __init__(self):
            self.ws = _FakeWs()
            self.active_dialog_id = "d1"

        async def create_new_dialog(self, **kw):
            return "d1"

        async def send_message(self, text, agent_id, dialog_id, is_suggest):
            # имитируем send_text_input: yield из queue с finally-cleanup
            try:
                while True:
                    resp = await queue.get()
                    yield resp
                    if resp.is_finished:
                        break
            finally:
                self.ws._pending_responses.pop("req-1", None)
                self.ws._active_agents_by_req.pop("req-1", None)
                self.ws.requested_mode_by_req.pop("req-1", None)
                self.ws.effective_by_req.pop("req-1", None)

    mgr = _FakeMgr()
    session = ChatSession("sid", mgr)
    session.dialog_id = "d1"
    session._current_req_id = "req-1"

    async def run():
        # Кладём cancel-сигнал через cancel_current
        await session.cancel_current()
        # Generator должен проснуться, отдать turn, выйти, очистить всё
        turns = []
        async for t in session.send_message("hi", agent_id="pro"):
            turns.append(t)
            if t.is_finished:
                break
        return turns

    turns = asyncio.run(run())
    assert any(t.error == "cancelled by client" for t in turns)
    assert mgr.ws._pending_responses == {}
    assert mgr.ws._active_agents_by_req == {}
    assert mgr.ws.requested_mode_by_req == {}
    assert mgr.ws.effective_by_req == {}


# ---------------- AliceMcpServer._managers_cache ----------------

def test_mcp_managers_cache_max_limit():
    """_max=2: при добавлении 3-го auth первый вытесняется и закрывается."""
    from tools.manager import AliceMcpServer

    closed = []

    class _FakeMgr:
        def __init__(self, creds, verbose=False, **kwargs):
            self.creds = creds
            self.verbose = verbose
        async def initialize(self):
            return None
        async def shutdown(self):
            closed.append(self.creds.token if hasattr(self.creds, "token") else "?")

    mcp = AliceMcpServer(manager=None, max_managers=2)

    # Патчим resolve_auth_source и verify_credentials
    import tools.manager as mgr_mod
    orig_resolve = mgr_mod.resolve_auth_source
    orig_verify = mgr_mod.verify_credentials
    orig_mgr_cls = mgr_mod.AliceAgentManager

    class _Creds:
        def __init__(self, name):
            self.token = name
    def fake_resolve(auth):
        return _Creds(auth)
    async def fake_verify(creds):
        return True, "ok"

    mgr_mod.resolve_auth_source = fake_resolve
    mgr_mod.verify_credentials = fake_verify
    mgr_mod.AliceAgentManager = _FakeMgr
    try:
        async def run():
            await mcp._get_manager_for_call("A")
            await mcp._get_manager_for_call("B")
            await mcp._get_manager_for_call("C")
            return dict(mcp._managers_cache)
        cache = asyncio.run(run())
    finally:
        mgr_mod.resolve_auth_source = orig_resolve
        mgr_mod.verify_credentials = orig_verify
        mgr_mod.AliceAgentManager = orig_mgr_cls

    assert set(cache.keys()) == {"B", "C"}, f"got {set(cache.keys())}"
    assert closed == ["A"], f"evicted: {closed}"


def test_mcp_managers_cache_ttl_eviction():
    from tools.manager import AliceMcpServer

    closed = []

    class _FakeMgr:
        def __init__(self, creds, verbose=False, **kwargs):
            self.creds = creds
            self.verbose = verbose
        async def initialize(self):
            return None
        async def shutdown(self):
            closed.append(self.creds.token)

    mcp = AliceMcpServer(manager=None, ttl_seconds=0.05)

    import tools.manager as mgr_mod
    orig_resolve = mgr_mod.resolve_auth_source
    orig_verify = mgr_mod.verify_credentials
    orig_mgr_cls = mgr_mod.AliceAgentManager

    class _Creds:
        def __init__(self, name):
            self.token = name
    def fake_resolve(auth):
        return _Creds(auth)
    async def fake_verify(creds):
        return True, "ok"

    mgr_mod.resolve_auth_source = fake_resolve
    mgr_mod.verify_credentials = fake_verify
    mgr_mod.AliceAgentManager = _FakeMgr
    try:
        async def run():
            await mcp._get_manager_for_call("A")
            await asyncio.sleep(0.1)  # > TTL
            # Следующий acquire вызовет _evict_ttl
            await mcp._get_manager_for_call("B")
            return dict(mcp._managers_cache)
        cache = asyncio.run(run())
    finally:
        mgr_mod.resolve_auth_source = orig_resolve
        mgr_mod.verify_credentials = orig_verify
        mgr_mod.AliceAgentManager = orig_mgr_cls

    assert "A" not in cache, f"TTL не сработал: {cache.keys()}"
    assert "B" in cache
    assert closed == ["A"], f"shutdown вызван: {closed}"


def test_mcp_managers_cache_reuse_updates_last_used():
    from tools.manager import AliceMcpServer

    class _FakeMgr:
        def __init__(self, creds, verbose=False, **kwargs):
            self.creds = creds
            self.verbose = verbose
        async def initialize(self):
            return None
        async def shutdown(self):
            return None

    mcp = AliceMcpServer(manager=None)

    import tools.manager as mgr_mod
    orig_resolve = mgr_mod.resolve_auth_source
    orig_verify = mgr_mod.verify_credentials
    orig_mgr_cls = mgr_mod.AliceAgentManager

    class _Creds:
        def __init__(self, name):
            self.token = name
    def fake_resolve(auth):
        return _Creds(auth)
    async def fake_verify(creds):
        return True, "ok"

    mgr_mod.resolve_auth_source = fake_resolve
    mgr_mod.verify_credentials = fake_verify
    mgr_mod.AliceAgentManager = _FakeMgr
    try:
        async def run():
            await mcp._get_manager_for_call("A")
            first_ts = mcp._last_used["A"]
            await asyncio.sleep(0.01)
            await mcp._get_manager_for_call("A")
            return first_ts, mcp._last_used["A"]
        first_ts, second_ts = asyncio.run(run())
    finally:
        mgr_mod.resolve_auth_source = orig_resolve
        mgr_mod.verify_credentials = orig_verify
        mgr_mod.AliceAgentManager = orig_mgr_cls

    assert second_ts > first_ts, "reuse не обновил last_used"
