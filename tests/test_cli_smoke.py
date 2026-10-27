"""CLI не падает на --help и импортируется без TTY."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(script, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / script), *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=20,
    )


def test_alice_cli_help():
    r = _run("alice_cli.py", "--help")
    assert r.returncode == 0, r.stderr[:500]
    assert "Alice" in r.stdout
    assert "--token" in r.stdout
    assert "--yolo" in r.stdout
    assert "--thinking" in r.stdout
    assert "--tools-config" in r.stdout
    assert "--no-tools" in r.stdout
    assert "--no-meeseeks" in r.stdout


def test_alice_server_help():
    r = _run("alice_server.py", "--help")
    assert r.returncode == 0, r.stderr[:500]
    assert "--http" in r.stdout
