from pathlib import Path

def test_tools_package_layout():
    """tools/ должен содержать ровно 6 модулей + __init__ (без дробления на подпакеты)."""
    root = Path(__file__).resolve().parent.parent / "tools"
    assert root.is_dir()
    files = sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix == ".py")
    expected = ["__init__.py", "effective.py", "gateway.py", "manager.py",
                "parser.py", "transport.py"]
    assert files == expected, f"got {files}"


def test_mcp_inside_manager():
    """AliceMcpServer должен жить в tools/manager.py."""
    from tools.manager import AliceMcpServer  # noqa: F401
