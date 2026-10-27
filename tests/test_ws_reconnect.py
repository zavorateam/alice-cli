"""После обрыва WS supervisor переподключается; listener не запускает второй reader."""
import asyncio

import websockets


def test_listener_sets_ws_none_on_connectionclosed():
    """_listen_loop при ConnectionClosed должен выставить ws=None и выйти."""
    from alice_auth import AliceCredentials
    from alice_server import AliceWsEngine

    async def run():
        eng = AliceWsEngine(AliceCredentials(auth_type="token", token="y0_x"))

        class _FakeWS:
            async def recv(self):
                raise websockets.exceptions.ConnectionClosed(None, None)
            async def close(self):
                return None

        eng.ws = _FakeWS()
        eng.is_connected = True
        await eng._listen_loop()
        assert eng.ws is None
        assert eng.is_connected is False

    asyncio.run(run())


def test_disconnect_idempotent():
    from alice_auth import AliceCredentials
    from alice_server import AliceWsEngine

    async def run():
        eng = AliceWsEngine(AliceCredentials(auth_type="token", token="y0_x"))
        await eng.disconnect()
        await eng.disconnect()

    asyncio.run(run())
