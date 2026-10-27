"""§11: main() не должен глотать SystemExit."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_alice_server_main_propagates_systemexit():
    """Если main_async сделает sys.exit(2), процесс должен завершиться с кодом 2."""
    # Запускаем alice_server.py с невалидным аргументом, чтобы argparse сделал exit(2)
    r = subprocess.run(
        [sys.executable, str(ROOT / "alice_server.py"), "--nonexistent-flag"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    assert r.returncode != 0, "main() не должен возвращать 0 при ошибке парсинга"


def test_alice_cli_main_propagates_systemexit():
    r = subprocess.run(
        [sys.executable, str(ROOT / "alice_cli.py"), "--nonexistent-flag"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    assert r.returncode != 0
