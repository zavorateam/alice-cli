"""§8 безопасные дефолты + §3.5 active request protection + §5.3 usage."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


# ---------------- §8: files.root ----------------

def test_files_root_default_is_home(tmp_path):
    """Если в config нет files.root — используется '~', не '/'."""
    from local_tools import load_tools_from_config_async
    cfg = tmp_path / "tools.json"
    cfg.write_text(json.dumps({
        "shell": {"enabled": False},
        "files": {"read_enabled": True, "write_enabled": False},
    }), encoding="utf-8")
    import asyncio
    reg = asyncio.run(load_tools_from_config_async(str(cfg)))
    # ReadFileTool должен иметь root = home, не "/"
    rf = reg.tools.get("read_file")
    assert rf is not None
    import os
    home = os.path.realpath(os.path.expanduser("~"))
    assert rf.root == home, f"files.root={rf.root!r}, ожидалось {home!r}"


def test_files_root_explicit_slash_warns_and_falls_back(tmp_path, caplog):
    """files.root='/' → warning + fallback на '~'."""
    import logging
    from local_tools import load_tools_from_config_async
    cfg = tmp_path / "tools.json"
    cfg.write_text(json.dumps({
        "shell": {"enabled": False},
        "files": {"root": "/", "read_enabled": True, "write_enabled": False},
    }), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        reg = asyncio.run(load_tools_from_config_async(str(cfg)))
    text = "\n".join(r.message for r in caplog.records)
    assert "files.root" in text
    rf = reg.tools.get("read_file")
    import os
    home = os.path.realpath(os.path.expanduser("~"))
    assert rf.root == home, f"root={rf.root!r}, но warning+fallback ожидался"


def test_shell_strict_false_warns(tmp_path, caplog):
    import logging
    from local_tools import load_tools_from_config_async
    cfg = tmp_path / "tools.json"
    cfg.write_text(json.dumps({
        "shell": {"enabled": True, "strict": False},
        "files": {},
    }), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        asyncio.run(load_tools_from_config_async(str(cfg)))
    text = "\n".join(r.message for r in caplog.records)
    assert "shell.strict=false" in text


# ---------------- §3.5: активные не вытесняются ----------------

def test_session_registry_skips_active_on_ttl():
    from tools.manager import SessionRegistry
    closed = []
    mgr = _FakeMgr()
    async def factory(): return mgr
    reg = SessionRegistry(manager_factory=factory, ttl_seconds=0.01)
    client = _client_with(reg)
    # создаём одну сессию
    r = client.post("/v1/chat/completions",
                    headers={"X-Alice-Session-ID": "A"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    sess_a = reg._sessions["A"]
    # эмулируем активный запрос
    sess_a._active = 1
    time.sleep(0.05)  # TTL истёк
    # следующий acquire должен попытаться вытеснить A, но не сможет
    r = client.post("/v1/chat/completions",
                    headers={"X-Alice-Session-ID": "B"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200, r.text[:300]
    # A всё ещё в registry (был активен)
    assert "A" in reg._sessions
    assert "B" in reg._sessions


def test_session_registry_backpressure_when_all_active():
    from tools.manager import SessionRegistry
    async def factory(): return _FakeMgr()
    reg = SessionRegistry(manager_factory=factory, max_sessions=1)
    client = _client_with(reg)
    r = client.post("/v1/chat/completions",
                    headers={"X-Alice-Session-ID": "A"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    sess_a = reg._sessions["A"]
    sess_a._active = 1  # занята

    # При max=1 и занятой A — B не должна создаться
    async def direct():
        try:
            await reg.acquire("B")
            return None
        except RuntimeError as e:
            return str(e)
    err = asyncio.run(direct())
    assert err is not None and "capacity" in err.lower()


def test_session_registry_evicts_when_not_active():
    from tools.manager import SessionRegistry
    async def factory(): return _FakeMgr()
    reg = SessionRegistry(manager_factory=factory, max_sessions=1)
    client = _client_with(reg)
    r = client.post("/v1/chat/completions",
                    headers={"X-Alice-Session-ID": "A"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    # A не активна (запрос завершён) — вытеснение сработает
    r = client.post("/v1/chat/completions",
                    headers={"X-Alice-Session-ID": "B"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert "A" not in reg._sessions
    assert "B" in reg._sessions


# ---------------- §5.3: usage ----------------

class _FakeMgr:
    """Минимальный production-подобный manager для SessionRegistry-тестов.

    Имеет send_message, create_new_dialog, rpc.delete_dialog, _on_dialog_closed —
    всё, что ChatSession.close() и send_message() вызывают.
    """
    def __init__(self):
        self.active_dialog_id = "d-fake"
        self.dialog_seq = 0
        self.history = []

    async def create_new_dialog(self, agent_id="pro", is_ephemeral=False):
        self.dialog_seq += 1
        did = f"d-{self.dialog_seq}"
        self.active_dialog_id = did
        return did

    async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
        from agents.base import AliceTurnResponse
        target = dialog_id or self.active_dialog_id
        self.history.append((target, text))
        yield AliceTurnResponse(dialog_id=target, request_id=f"r-{len(self.history)}",
                                text="ok", is_finished=True)

    async def _on_dialog_closed(self, dialog_id):
        return None

    @property
    def rpc(self):
        class _Rpc:
            async def delete_dialog(self, *a, **kw):
                return (200, "ok")
        return _Rpc()


class _ScriptedSession:
    def __init__(self, texts):
        self._texts = list(texts)
        self.dialog_id = "d"
        self._active = 0
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r",
                                text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _Reg:
    def __init__(self, s): self._s = s
    async def acquire(self, sid): return self._s


def _client_with(reg):
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, reg)
    return TestClient(app)


def _client(texts):
    return _client_with(_Reg(_ScriptedSession(texts)))


def test_usage_null_in_non_stream():
    client = _client(["hello"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
    })
    body = r.json()
    assert "usage" in body
    assert body["usage"] is None


def test_stream_options_include_usage_adds_final_chunk():
    client = _client(["hello"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    })
    assert r.status_code == 200
    chunks = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    # ищем chunk с choices=[] и usage
    found_usage = False
    for ln in chunks:
        payload = ln[len("data: "):]
        if payload.strip() == "[DONE]":
            break
        obj = json.loads(payload)
        if obj.get("choices") == [] and "usage" in obj:
            found_usage = True
    assert found_usage, "нет final chunk с usage при include_usage=true"


def test_stream_options_without_stream_400():
    client = _client([])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream_options": {"include_usage": True},
    })
    assert r.status_code == 400
    assert "stream_options" in r.text


def test_stream_options_invalid_type_400():
    client = _client([])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": "not-an-object",
    })
    assert r.status_code == 400
