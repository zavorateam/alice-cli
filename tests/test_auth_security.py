"""§2: безопасность credentials и файлов."""
import os
import stat
import subprocess
from pathlib import Path

import pytest


def test_gitignore_covers_auth_files():
    gi = Path(".gitignore").read_text()
    for name in ("token.txt", ".alice_token", ".alice_cookies", "cookies.txt",
                 ".alice_device_uuid"):
        assert name in gi, f"{name} не в .gitignore"


def test_safe_write_file_permissions(tmp_path):
    from alice_auth import _safe_write_file
    p = tmp_path / "secret.txt"
    assert _safe_write_file(str(p), "y0_test")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, f"mode={oct(mode)}"


def test_safe_write_file_atomic(tmp_path):
    """При повторной записи файл остаётся валидным и с правами 0600."""
    from alice_auth import _safe_write_file
    p = tmp_path / "t.txt"
    _safe_write_file(str(p), "first")
    _safe_write_file(str(p), "second")
    assert p.read_text() == "second"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_log_session_hint_does_not_leak_uuid(caplog):
    """Утечка UUID в лог — баг §2."""
    import logging
    from alice_auth import AliceCredentials
    from alice_server import _log_session_hint

    creds = AliceCredentials(
        auth_type="both",
        token="y0_secret_token_should_not_appear",
        cookie_header="Session_id=super_secret_session; i=xyz",
        cookie_dict={"Session_id": "super_secret_session", "yandexuid": "12345", "i": "xyz"},
        device_uuid="00000000000001610735911788167721",
    )
    with caplog.at_level(logging.INFO):
        _log_session_hint(creds)
    text = "\n".join(r.message for r in caplog.records)
    assert "super_secret_session" not in text
    assert "y0_secret_token" not in text
    assert "00000000000001610735911788167721" not in text
    assert "12345" not in text
    # но что-то должно остаться
    assert "session_hint" in text or "Session_id=present" in text or "device_fp" in text


def test_strict_explicit_no_silent_fallback(monkeypatch):
    """strict_explicit=True + битый источник = None, без fallback на env."""
    from alice_auth import load_credentials_with_cookies
    monkeypatch.setenv("ALICE_TOKEN", "y0_env_fallback")
    result = load_credentials_with_cookies(
        token_source="!!!this is not a token!!!",
        strict_explicit=True,
        allow_interactive=False,
    )
    assert result is None, "не должно быть silent fallback на env"


def test_strict_explicit_allows_fallback_when_no_explicit(monkeypatch, tmp_path):
    """strict_explicit=True без явных источников = обычный fallback."""
    monkeypatch.setenv("ALICE_TOKEN", "y0_from_env")
    monkeypatch.chdir(tmp_path)
    from alice_auth import load_credentials_with_cookies
    result = load_credentials_with_cookies(
        strict_explicit=True,
        allow_interactive=False,
    )
    assert result is not None
    assert result.token == "y0_from_env"
