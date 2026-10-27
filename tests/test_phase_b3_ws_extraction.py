"""Phase B3: AliceWsEngine физически в tools/transport.py."""
from pathlib import Path


def test_ws_engine_physically_in_transport():
    tr = Path("tools/transport.py").read_text()
    srv = Path("alice_server.py").read_text()
    assert "class AliceWsEngine" in tr
    assert "class AliceWsEngine" not in srv


def test_ws_engine_identity():
    import alice_server
    import tools.transport
    assert alice_server.AliceWsEngine is tools.transport.AliceWsEngine


def test_transport_has_no_cycle():
    tr = Path("tools/transport.py").read_text()
    assert "from alice_server import" not in tr
    assert "import alice_server" not in tr


def test_transport_still_has_rpc_client():
    tr = Path("tools/transport.py").read_text()
    assert "class AliceRpcClient" in tr
    assert "class AliceWsEngine" in tr


def test_ws_engine_uses_connection_closed():
    tr = Path("tools/transport.py").read_text()
    assert "from websockets.exceptions import ConnectionClosed" in tr
    assert "except ConnectionClosed" in tr


def test_ws_engine_has_all_methods():
    from tools.transport import AliceWsEngine
    for name in (
        "start", "connect", "disconnect", "_open", "_supervise",
        "_listen_loop", "_synchronize_state", "_flush_subscriptions",
        "update_subscriptions", "add_full_content", "remove_full_content",
        "send_text_input", "upload_file", "enable_api_mode",
        "_handle_vins_response", "_icookie",
    ):
        assert hasattr(AliceWsEngine, name), f"метод {name} отсутствует"


def test_alice_server_imports_ok():
    import importlib, sys
    for m in list(sys.modules):
        if m == "alice_server" or m.startswith("tools."):
            sys.modules.pop(m, None)
    importlib.import_module("alice_server")
