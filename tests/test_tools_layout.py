"""Структура tools/: 7 модулей, без циклов с alice_server."""
from pathlib import Path

EXPECTED_MODULES = [
    "__init__.py",
    "constants.py",
    "effective.py",
    "gateway.py",
    "manager.py",
    "parser.py",
    "transport.py",
]

CYCLE_FORBIDDEN = ("constants.py", "parser.py", "transport.py", "effective.py")


def test_tools_package_layout():
    root = Path(__file__).resolve().parent.parent / "tools"
    assert root.is_dir()
    files = sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix == ".py")
    assert files == EXPECTED_MODULES, f"got {files}"


def test_no_cycles_back_to_alice_server():
    """constants/parser/transport/effective не должны импортировать alice_server — это цикл."""
    root = Path(__file__).resolve().parent.parent / "tools"
    for name in CYCLE_FORBIDDEN:
        src = (root / name).read_text()
        assert "from alice_server import" not in src, f"{name}: cyclic import"
        assert "import alice_server" not in src, f"{name}: cyclic import"


def test_constants_module_is_only_constants():
    """tools/constants.py — только константы + uuid7, без классов и менеджеров."""
    root = Path(__file__).resolve().parent.parent / "tools"
    src = (root / "constants.py").read_text()
    assert "class " not in src, "constants.py не должен содержать классов"
    # менеджеры/серверы/движки — не в constants
    for forbidden in ("AliceAgentManager", "AliceMcpServer", "AliceWsEngine",
                      "AliceRpcClient", "SessionRegistry", "ChatSession"):
        assert forbidden not in src, f"{forbidden} не должен быть в constants.py"


def test_mcp_inside_manager():
    """AliceMcpServer доступен из tools/manager.py."""
    from tools.manager import AliceMcpServer  # noqa: F401
