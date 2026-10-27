"""§4: контракт AliceWsEngine.

Все тесты — unit + fake WS, без реальной сети. Проверяют:
  - delta-нормализация в _listen_loop (compute_delta используется внутри);
  - start() идемпотентен;
  - провал _synchronize_state закрывает ws;
  - backoff на повторных ошибках recv;
  - API-режим держит full-content для всех активных диалогов;
  - run_stdio использует get_running_loop.
"""
import asyncio
import inspect
import json
import time
from pathlib import Path
from typing import Optional

import pytest
import websockets

from websockets.exceptions import ConnectionClosed
from alice_auth import AliceCredentials
from alice_server import AliceWsEngine
from tools.constants import uuid7


class _FakeWS:
    def __init__(self, recv_script):
        self._script = list(recv_script)
        self.sent = []
    async def recv(self):
        if not self._script:
            raise ConnectionClosed(None, None)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
    async def send(self, data):
        self.sent.append(data)
    async def close(self):
        return None


def _creds():
    return AliceCredentials(auth_type="token", token="y0_test")


def _deferred_frame(ref: str, text: str, is_last: bool = False) -> str:
    return json.dumps({
        "directive": {
            "header": {"name": "DeferredAliceResponse",
                       "refMessageId": ref, "messageId": uuid7()},
            "payload": {
                "json_response": {
                    "is_last": is_last,
                    "base_response": {
                        "cards": [{"neuro_structured_blocks_card": {"plain_text": text}}]
                    },
                }
            },
        }
    })


async def _drain_queue(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def _run_listener(recv_script, ref="r1"):
    eng = AliceWsEngine(_creds())
    q = asyncio.Queue()
    eng._pending_responses[ref] = q
    from agents import registry, ensure_agents_registered
    ensure_agents_registered()
    eng._active_agents_by_req[ref] = registry.get("pro")
    eng.ws = _FakeWS(recv_script + [ConnectionClosed(None, None)])
    eng.is_connected = True
    await eng._listen_loop()
    return await _drain_queue(q), eng


# ---------- delta normalisation ----------

def test_listener_accumulates_snapshot():
    """Накопительные снимки → потребитель видит накопительный текст."""
    async def run():
        turns, _ = await _run_listener([
            _deferred_frame("r1", "Hel", is_last=False),
            _deferred_frame("r1", "Hello", is_last=False),
            _deferred_frame("r1", "Hello world", is_last=True),
        ])
        assert [t.text for t in turns] == ["Hel", "Hello", "Hello world"]
        assert turns[-1].is_finished is True
    asyncio.run(run())


def test_listener_accumulates_delta():
    """Настоящие дельты → потребитель всё равно видит накопительный текст."""
    async def run():
        turns, _ = await _run_listener([
            _deferred_frame("r1", "Hel", is_last=False),
            _deferred_frame("r1", "lo", is_last=False),
            _deferred_frame("r1", " world", is_last=True),
        ])
        assert [t.text for t in turns] == ["Hel", "Hello", "Hello world"], (
            f"listener не нормализует дельты: {[t.text for t in turns]}"
        )
    asyncio.run(run())


def test_listener_handles_regress_length():
    """Регресс: новый кадр короче предыдущего — не теряем уже собранное."""
    async def run():
        turns, _ = await _run_listener([
            _deferred_frame("r1", "Hello world", is_last=False),
            _deferred_frame("r1", "Hello", is_last=True),
        ])
        assert turns[0].text == "Hello world"
        assert turns[-1].text == "Hello world", (
            f"регресс текста не обработан: {turns[-1].text!r}"
        )
        assert turns[-1].is_finished is True
    asyncio.run(run())


def test_listener_isolates_refs():
    """Два ref параллельно — разные аккумуляторы."""
    async def run():
        eng = AliceWsEngine(_creds())
        qa, qb = asyncio.Queue(), asyncio.Queue()
        eng._pending_responses["A"] = qa
        eng._pending_responses["B"] = qb
        from agents import registry
        eng._active_agents_by_req["A"] = registry.get("pro")
        eng._active_agents_by_req["B"] = registry.get("pro")
        eng.ws = _FakeWS([
            _deferred_frame("A", "A1", is_last=False),
            _deferred_frame("B", "B1", is_last=False),
            _deferred_frame("A", "A2", is_last=False),
            _deferred_frame("B", "B2", is_last=True),
            _deferred_frame("A", "A3", is_last=True),
            ConnectionClosed(None, None),
        ])
        eng.is_connected = True
        await eng._listen_loop()
        a_turns = await _drain_queue(qa)
        b_turns = await _drain_queue(qb)
        assert [t.text for t in a_turns] == ["A1", "A1A2", "A1A2A3"]
        assert [t.text for t in b_turns] == ["B1", "B1B2"]
    asyncio.run(run())


# ---------- start() idempotency ----------

def test_ws_start_idempotent():
    async def run():
        eng = AliceWsEngine(_creds())
        open_calls = {"n": 0}
        sync_calls = {"n": 0}

        async def fake_open(self):
            open_calls["n"] += 1
            self.is_connected = True
            self.ws = _FakeWS([])
        async def fake_sync(self):
            sync_calls["n"] += 1
            self._sync_event.set()
        async def fake_listener(self):
            while self.is_connected:
                await asyncio.sleep(0.02)
        async def fake_supervise(self):
            while self._should_run:
                await asyncio.sleep(0.02)

        orig = (AliceWsEngine._open, AliceWsEngine._synchronize_state,
                AliceWsEngine._listen_loop, AliceWsEngine._supervise)
        AliceWsEngine._open = fake_open
        AliceWsEngine._synchronize_state = fake_sync
        AliceWsEngine._listen_loop = fake_listener
        AliceWsEngine._supervise = fake_supervise
        try:
            await eng.start()
            await eng.start()   # повторный вызов не должен повторно открывать
            await eng.disconnect()
        finally:
            (AliceWsEngine._open, AliceWsEngine._synchronize_state,
             AliceWsEngine._listen_loop, AliceWsEngine._supervise) = orig

        assert open_calls["n"] == 1, f"start() не идемпотентен: _open вызван {open_calls['n']} раз"
        assert sync_calls["n"] == 1
    asyncio.run(run())


def test_ws_sync_failure_closes_connection():
    """Если _synchronize_state бросает — is_connected=False, ws=None."""
    async def run():
        eng = AliceWsEngine(_creds())

        async def fake_open(self):
            self.is_connected = True
            self.ws = _FakeWS([])
        async def fake_sync(self):
            raise RuntimeError("sync failed")
        async def fake_listener(self):
            while self.is_connected:
                await asyncio.sleep(0.02)
        async def fake_supervise(self):
            while self._should_run:
                await asyncio.sleep(0.02)

        orig = (AliceWsEngine._open, AliceWsEngine._synchronize_state,
                AliceWsEngine._listen_loop, AliceWsEngine._supervise)
        AliceWsEngine._open = fake_open
        AliceWsEngine._synchronize_state = fake_sync
        AliceWsEngine._listen_loop = fake_listener
        AliceWsEngine._supervise = fake_supervise
        try:
            with pytest.raises(RuntimeError):
                await eng.start()
            assert eng.is_connected is False, "sync failure не закрывает is_connected"
            assert eng.ws is None, "sync failure не сбрасывает ws"
        finally:
            (AliceWsEngine._open, AliceWsEngine._synchronize_state,
             AliceWsEngine._listen_loop, AliceWsEngine._supervise) = orig
            try:
                await eng.disconnect()
            except Exception:
                pass
    asyncio.run(run())


# ---------- error backoff ----------

def test_listener_backoff_on_repeated_errors(monkeypatch):
    """Неожиданные ошибки recv не должны крутиться busy-loop; нужен sleep."""
    sleeps = []
    orig_sleep = asyncio.sleep

    async def fake_sleep(d, *a, **kw):
        sleeps.append(d)
        await orig_sleep(0)

    async def run():
        eng = AliceWsEngine(_creds())
        q = asyncio.Queue()
        eng._pending_responses["r1"] = q
        from agents import registry
        eng._active_agents_by_req["r1"] = registry.get("pro")
        eng.ws = _FakeWS([
            RuntimeError("bad frame 1"),
            RuntimeError("bad frame 2"),
            RuntimeError("bad frame 3"),
            ConnectionClosed(None, None),
        ])
        eng.is_connected = True
        await eng._listen_loop()

    import alice_server as srv
    monkeypatch.setattr(srv.asyncio, "sleep", fake_sleep)
    asyncio.run(run())
    assert len(sleeps) >= 3, (
        f"_listen_loop не делает backoff при ошибках (sleeps={sleeps}). "
        f"busy-loop при персистентной ошибке recv."
    )


# ---------- subscriptions in API mode ----------

def test_api_mode_keeps_all_dialogs_full_content():
    """В API-режиме все активные диалоги получают full-content, не только активный."""
    async def run():
        eng = AliceWsEngine(_creds())
        eng.enable_api_mode()  # метод, который надо ввести
        eng._sync_event.set()
        # подписки отправляются в WS; подменим _send_json чтобы поймать payload
        sent = []
        async def fake_send(self, data):
            sent.append(data)
        orig_send = AliceWsEngine._send_json
        AliceWsEngine._send_json = fake_send
        try:
            await eng.update_subscriptions("d1", {"d1", "d2", "d3"})
        finally:
            AliceWsEngine._send_json = orig_send
        assert "d1" in eng._full_content_dialogs
        assert "d2" in eng._full_content_dialogs, (
            f"API-режим: d2 не в full_content ({eng._full_content_dialogs})"
        )
        assert "d3" in eng._full_content_dialogs
    asyncio.run(run())


# ---------- run_stdio event loop ----------

def test_run_stdio_uses_get_running_loop():
    """Не должно быть asyncio.get_event_loop() в run_stdio — deprecated в 3.14.

    run_stdio физически в tools/manager.py (AliceMcpServer переехал).
    Проверяем через AST для надёжности.
    """
    import ast

    # Найти файл, где физически определён AliceMcpServer
    from tools import manager as mgr_mod
    file_path = Path(mgr_mod.__file__)
    text = file_path.read_text()

    tree = ast.parse(text)
    target = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AliceMcpServer":
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == "run_stdio":
                    target = sub
                    break
            break

    assert target is not None, f"run_stdio не найден в {file_path}"
    body_src = ast.get_source_segment(text, target) or ""
    assert "get_event_loop()" not in body_src, (
        f"run_stdio в {file_path} использует asyncio.get_event_loop() — "
        f"Python 3.14 deprecation, надо get_running_loop()"
    )
