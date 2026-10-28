"""CLI --verbose должен активировать INFO-логи WS-транспорта."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_verbose_enables_info_logging():
    """В коде async_main есть basicConfig при args.verbose."""
    src = (ROOT / "alice_cli.py").read_text()
    # Проверяем через AST, что basicConfig вызывается при args.verbose
    import ast
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_main":
            target = node
            break
    assert target is not None
    body_src = ast.get_source_segment(src, target) or ""
    assert "logging.basicConfig" in body_src, (
        "async_main не вызывает logging.basicConfig — --verbose не работает"
    )
    assert "args.verbose" in body_src
    # Убедимся, что basicConfig не в main() вне async_main
    main_target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_target = node
            break
    assert main_target is not None
    main_src = ast.get_source_segment(src, main_target) or ""
    assert "logging.basicConfig" not in main_src, (
        "basicConfig в main() — должно быть в async_main, под args.verbose"
    )


def test_cli_help_still_works():
    """--help не ломается basicConfig."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "alice_cli.py"), "--help"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0
    assert "--verbose" in r.stdout
    assert "--thinking" in r.stdout
