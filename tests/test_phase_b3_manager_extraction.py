"""Phase B3+: GenericAppAgent, AliceAgentManager, AliceMcpServer в tools/manager.py."""
from pathlib import Path


def test_classes_physically_in_manager():
    mgr = Path("tools/manager.py").read_text()
    srv = Path("alice_server.py").read_text()
    for cls in ("GenericAppAgent", "AliceAgentManager", "AliceMcpServer"):
        assert f"class {cls}" in mgr, f"{cls} нет в manager.py"
        assert f"class {cls}" not in srv, f"{cls} всё ещё в alice_server.py"


def test_identity_via_reexport():
    import alice_server
    import tools.manager
    for cls in ("GenericAppAgent", "AliceAgentManager", "AliceMcpServer"):
        a = getattr(alice_server, cls)
        b = getattr(tools.manager, cls)
        assert a is b, f"{cls}: re-export не совпадает"


def test_manager_has_no_cycle():
    mgr = Path("tools/manager.py").read_text()
    assert "from alice_server import" not in mgr
    assert "import alice_server" not in mgr


def test_manager_imports_transport():
    mgr = Path("tools/manager.py").read_text()
    assert "from tools.transport import AliceWsEngine, AliceRpcClient" in mgr


def test_manager_methods_intact():
    from tools.manager import (
        GenericAppAgent, AliceAgentManager, AliceMcpServer,
        ChatSession, SessionRegistry,
    )
    # GenericAppAgent
    assert hasattr(GenericAppAgent, "__init__")
    # AliceAgentManager
    for m in ("initialize", "create_new_dialog", "cleanup_ephemeral_dialogs",
              "send_message", "shutdown"):
        assert hasattr(AliceAgentManager, m), f"AliceAgentManager.{m} отсутствует"
    # AliceMcpServer
    for m in ("_get_tools_definition", "_get_manager_for_call",
              "handle_tool_call", "run_stdio"):
        assert hasattr(AliceMcpServer, m), f"AliceMcpServer.{m} отсутствует"
    # ChatSession
    for m in ("ensure_dialog", "send_message", "cancel_current", "close"):
        assert hasattr(ChatSession, m), f"ChatSession.{m} отсутствует"
    # SessionRegistry
    for m in ("acquire", "close"):
        assert hasattr(SessionRegistry, m), f"SessionRegistry.{m} отсутствует"


def test_manager_handle_tool_call_without_auth():
    """_get_manager_for_call без auth и без manager возвращает ошибку."""
    from tools.manager import AliceMcpServer
    import asyncio
    mcp = AliceMcpServer(manager=None)
    result = asyncio.run(mcp.handle_tool_call("alice_ask_pro", {"message": "hi"}))
    assert result["isError"] is True
    assert "Авторизация" in result["content"][0]["text"]


def test_agent_manager_uses_transport_types():
    """AliceAgentManager.rpc/ws должны иметь правильные типы."""
    from tools.manager import AliceAgentManager
    from tools.transport import AliceWsEngine, AliceRpcClient
    from alice_auth import AliceCredentials
    creds = AliceCredentials(auth_type="token", token="y0_test")
    mgr = AliceAgentManager(creds)
    assert isinstance(mgr.ws, AliceWsEngine)
    assert isinstance(mgr.rpc, AliceRpcClient)


def test_fresh_import_no_exceptions():
    import importlib, sys
    for m in list(sys.modules):
        if m == "alice_server" or m.startswith(("tools.", "tools")):
            sys.modules.pop(m, None)
    import alice_server
    import tools.manager
    import tools.transport
    assert alice_server.AliceAgentManager is tools.manager.AliceAgentManager
