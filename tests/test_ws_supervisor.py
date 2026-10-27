"""Ровно один reader на соединение, переподключение — supervisor, не listener."""
import asyncio
import inspect

import pytest

from alice_auth import AliceCredentials


def _engine():
    from alice_server import AliceWsEngine
    return AliceWsEngine(AliceCredentials(auth_type="token", token="y0_test"))


def test_listener_does_not_call_connect():
    """_listen_loop не должен вызывать connect(): это создаёт второй reader."""
    from alice_server import AliceWsEngine
    src = inspect.getsource(AliceWsEngine._listen_loop)
    assert "self.connect()" not in src, (
        "_listen_loop вызывает self.connect() — это создаёт второй reader "
        "при ConnectionClosed. Переподключением должен заниматься supervisor."
    )


def test_supervisor_exists():
    """Должен существовать отдельный supervisor-метод."""
    from alice_server import AliceWsEngine
    assert hasattr(AliceWsEngine, "_supervise"), (
        "AliceWsEngine._supervise отсутствует — нечем переподключаться после обрыва"
    )


def test_supervisor_task_field_exists():
    eng = _engine()
    assert hasattr(eng, "_supervisor_task"), (
        "AliceWsEngine не хранит _supervisor_task"
    )


def test_disconnect_cancels_supervisor():
    """После disconnect() supervisor-таск должен быть отменён."""
    async def run():
        from alice_server import AliceWsEngine
        # Патчим _open, чтобы не лезть в сеть
        async def _fake_open(self):
            self.is_connected = True
            self.ws = object()
        async def _fake_sync(self):
            self._sync_event.set()
        orig_open = AliceWsEngine._open
        orig_sync = AliceWsEngine._synchronize_state
        AliceWsEngine._open = _fake_open
        AliceWsEngine._synchronize_state = _fake_sync
        try:
            eng = _engine()
            await eng.start()
            sup = eng._supervisor_task
            assert sup is not None and not sup.done()
            await eng.disconnect()
            assert sup.done(), "supervisor не завершился после disconnect"
        finally:
            AliceWsEngine._open = orig_open
            AliceWsEngine._synchronize_state = orig_sync

    asyncio.run(run())
