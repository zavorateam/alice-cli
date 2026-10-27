"""§3: физический перенос в tools/ (Phase B1)."""
from pathlib import Path


def test_constants_module_exists():
    from tools.constants import uuid7, _FRAME_SEQ, WEBSOCKET_URI
    assert callable(uuid7)
    assert isinstance(_FRAME_SEQ, dict)
    assert WEBSOCKET_URI.startswith("wss://")


def test_uuid7_is_re_exported():
    import alice_server
    import tools.constants
    assert alice_server.uuid7 is tools.constants.uuid7


def test_parse_nsb_card_physically_moved():
    import alice_server
    import tools.parser
    assert alice_server._parse_nsb_card is tools.parser._parse_nsb_card
    src = Path("tools/parser.py").read_text()
    assert "def _parse_nsb_card(" in src


def test_rpc_client_physically_moved():
    import alice_server
    import tools.transport
    assert alice_server.AliceRpcClient is tools.transport.AliceRpcClient
    src = Path("tools/transport.py").read_text()
    assert "class AliceRpcClient" in src


def test_no_cycle_in_constants():
    src = Path("tools/constants.py").read_text()
    assert "alice_server" not in src
    assert "tools.manager" not in src
    assert "tools.transport" not in src


def test_no_cycle_in_parser():
    src = Path("tools/parser.py").read_text()
    assert "from alice_server" not in src
    assert "import alice_server" not in src


def test_transport_does_not_import_alice_server():
    src = Path("tools/transport.py").read_text()
    assert "from alice_server" not in src, (
        "tools/transport.py не должен импортировать alice_server — "
        "это цикл (alice_server → tools.transport → alice_server)"
    )


def test_alice_server_still_reexports_ws_engine():
    """AliceWsEngine пока не перенесён (B2), но должен быть доступен из alice_server."""
    from alice_server import AliceWsEngine
    assert AliceWsEngine is not None


def test_uuid7_shape():
    from tools.constants import uuid7
    u = uuid7()
    assert len(u) == 36
    assert u[14] == "7"   # version 7


def test_fresh_import_no_exceptions():
    """Импорт alice_server с нуля не должен падать."""
    import importlib, sys
    for m in list(sys.modules):
        if m in ("alice_server",) or m.startswith(("tools.", "tools")):
            sys.modules.pop(m, None)
    import alice_server
    import tools.constants, tools.parser, tools.transport
    assert alice_server.uuid7 is tools.constants.uuid7
