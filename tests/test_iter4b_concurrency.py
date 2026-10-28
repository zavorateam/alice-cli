"""Iteration 4b: warm_up awaited, ChatSession serialization, shutdown order."""
import asyncio
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class _RecordingSession:
    def __init__(self, texts, delay=0.0):
        self._texts = list(texts)
        self.dialog_id = "d"
        self._active = 0
        self._send_lock = asyncio.Lock()
        self._current_req_id = None
        self._delay = delay
        self.prompts = []
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        self.prompts.append(prompt)
        if self._delay:
            await asyncio.sleep(self._delay)
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r",
                                text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _R:
    def __init__(self, s): self._s = s
    async def acquire(self, sid): return self._s


def _client(texts, delay=0.0):
    s = _RecordingSession(texts, delay=delay)
    c = ModelCatalog(manager_provider=lambda: None)
    c.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, c, _R(s))
    return TestClient(app), s


# ---------- ChatSession serialization ----------

def test_chatsession_rejects_concurrent_sends():
    """Второй параллельный запрос к той же ChatSession → SessionConflictError.

    Не сериализация: сессия связана с одним dialog_id, параллельные ходы
    смешивают историю и ломают отмену. Модель — отказ.
    """
    from tools.manager import ChatSession, SessionConflictError

    class _Mgr:
        active_dialog_id = "d"
        _active = 0
        dialog_id = "d"
        def __init__(self):
            self._events = []
        async def create_new_dialog(self, **kw): return "d"
        async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
            from agents.base import AliceTurnResponse
            self._events.append(("start", text, time.monotonic()))
            await asyncio.sleep(0.15)
            self._events.append(("end", text, time.monotonic()))
            yield AliceTurnResponse(dialog_id="d", request_id=f"r-{text}",
                                    text=text, is_finished=True)
        @property
        def rpc(self):
            class _R:
                async def delete_dialog(self, *a, **kw): return (200, "")
            return _R()
        async def _on_dialog_closed(self, d): pass

    mgr = _Mgr()
    session = ChatSession("sid", mgr)

    errors = []

    async def _consume(s, text):
        try:
            async for _turn in s.send_message(text):
                pass
        except SessionConflictError as e:
            errors.append(str(e))

    async def run():
        t1 = asyncio.create_task(_consume(session, "A"))
        await asyncio.sleep(0.02)   # A точно захватил _active
        t2 = asyncio.create_task(_consume(session, "B"))
        await asyncio.gather(t1, t2)

    asyncio.run(run())

    # A выполнился полностью, B отказан
    assert any(e == "A" for e in [ev[1] for ev in mgr._events if ev[0] == "start"]), \
        f"A не выполнился: {mgr._events}"
    assert not any(ev[1] == "B" for ev in mgr._events), \
        f"B не должен дойти до manager.send_message: {mgr._events}"
    assert len(errors) == 1, f"ожидалась одна ошибка конфликта, got {errors}"
    assert "already has an active request" in errors[0]


def test_chatsession_allows_sequential_sends():
    """После завершения первого запроса — второй разрешён."""
    from tools.manager import ChatSession

    class _Mgr:
        active_dialog_id = "d"
        _active = 0
        dialog_id = "d"
        async def create_new_dialog(self, **kw): return "d"
        async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id=f"r-{text}",
                                    text=text, is_finished=True)
        @property
        def rpc(self):
            class _R:
                async def delete_dialog(self, *a, **kw): return (200, "")
            return _R()
        async def _on_dialog_closed(self, d): pass

    mgr = _Mgr()
    session = ChatSession("sid", mgr)

    async def run():
        async for _ in session.send_message("A"):
            pass
        async for _ in session.send_message("B"):
            pass
        return True

    assert asyncio.run(run()) is True


def test_chatsession_close_waits_for_active():
    """close() при активном запросе: cancel + ожидание _active == 0."""
    from tools.manager import ChatSession

    class _Mgr:
        active_dialog_id = "d"
        _active = 0
        dialog_id = "d"
        _pending_responses = {}
        async def create_new_dialog(self, **kw): return "d"
        async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
            from agents.base import AliceTurnResponse
            await asyncio.sleep(0.3)
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text=text, is_finished=True)
        @property
        def rpc(self):
            class _R:
                async def delete_dialog(self, *a, **kw): return (200, "")
            return _R()
        async def _on_dialog_closed(self, d): pass
        @property
        def ws(self):
            return None

    mgr = _Mgr()
    session = ChatSession("sid", mgr)
    session.dialog_id = "d"

    async def run():
        async def consume():
            async for _ in session.send_message("A"):
                pass
        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # consume точно в _active=1
        assert session._active == 1
        await session.close(delete_dialog=False)
        # close дождался завершения (или таймаута 5с)
        assert session._active == 0, f"_active={session._active} после close"
        try:
            await asyncio.wait_for(consumer, timeout=2.0)
        except asyncio.TimeoutError:
            consumer.cancel()
            raise AssertionError("consumer не завершился после close")

    asyncio.run(run())


def test_chatsession_active_count_decrements():
    from tools.manager import ChatSession

    class _Mgr:
        active_dialog_id = "d"
        dialog_id = "d"
        async def create_new_dialog(self, **kw): return "d"
        async def send_message(self, text, agent_id="pro", dialog_id=None, is_suggest=False):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text="x", is_finished=True)
        @property
        def rpc(self):
            class _R:
                async def delete_dialog(self, *a, **kw): return (200, "")
            return _R()
        async def _on_dialog_closed(self, d): pass

    mgr = _Mgr()
    session = ChatSession("sid", mgr)

    async def run():
        assert session._active == 0
        async for _ in session.send_message("hi"):
            # В процессе — active > 0
            assert session._active == 1
        # После — active вернулся в 0
        assert session._active == 0

    asyncio.run(run())
