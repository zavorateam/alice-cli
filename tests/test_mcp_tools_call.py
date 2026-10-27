"""Defensive MCP: tools/call без auth, невалидный JSON, неизвестный tool.

Все проверки — против публичного handle_tool_call (in-process, без subprocess —
сам stdio-протокол проверяется отдельно в test_mcp_stdio_real.py).
"""
import json

import pytest


def _server():
    from alice_server import AliceMcpServer
    return AliceMcpServer(manager=None)


def test_tools_call_without_auth_returns_error_not_crash():
    """Без auth и без manager — handle_tool_call возвращает isError, не падает."""
    mcp = _server()
    result = await_sync(mcp.handle_tool_call("alice_ask_pro", {"message": "hi"}))
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Авторизация" in text or "auth" in text.lower()


def test_tools_call_unknown_tool_returns_error():
    """Несуществующий tool не должен ронять сервер."""
    mcp = _server()
    result = await_sync(mcp.handle_tool_call("alice_ask_nonexistent", {"message": "hi"}))
    assert result["isError"] is True
    # Проверяем, что не упало на AttributeError
    assert any(kw in result["content"][0]["text"].lower() for kw in ("неизвест", "unknown", "auth", "авторизация"))


def test_tools_call_bad_auth_param_returns_error():
    """Мусорный auth-параметр не должен ронять verify_credentials."""
    mcp = _server()
    result = await_sync(mcp.handle_tool_call(
        "alice_ask_pro", {"message": "hi", "auth": "!!!not-a-token!!!"},
    ))
    assert result["isError"] is True


def test_click_action_without_auth():
    mcp = _server()
    result = await_sync(mcp.handle_tool_call("alice_click_action", {"action_text": "1"}))
    assert result["isError"] is True


def test_tools_definition_stable_across_instances():
    """Два вызова _get_tools_definition дают одинаковый набор имён."""
    mcp = _server()
    a = {t["name"] for t in mcp._get_tools_definition()}
    b = {t["name"] for t in mcp._get_tools_definition()}
    assert a == b
    assert "alice_ask_pro" in a
    assert "alice_click_action" in a


def test_notifications_initialized_has_no_response():
    """notifications/initialized — no response (continue без writer.write).

    Проверяем через subprocess: шлём initialize → notifications/initialized → ping,
    ожидаем ровно 2 строки в stdout (initialize + ping), notification не отвечает.
    """
    import os
    import select
    import subprocess
    import sys
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    for k in ("ALICE_TOKEN", "YANDEX_TOKEN", "ALICE_COOKIES"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "alice_server.py")],
        cwd="/tmp", env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        # initialize
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        }) + "\n")
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        if not ready:
            err = ""
            try:
                import fcntl
                fd = proc.stderr.fileno()
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                if select.select([proc.stderr], [], [], 0.2)[0]:
                    err = proc.stderr.read(2000) or ""
            except Exception:
                pass
            raise AssertionError(f"MCP init no response in 15s; stderr={err[:800]!r}")
        line1 = proc.stdout.readline()
        assert json.loads(line1)["result"]["serverInfo"]["name"] == "yandex-alice-server"

        # notification
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        proc.stdin.flush()

        # ping — должен быть следующим ответом
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "ping",
        }) + "\n")
        proc.stdin.flush()
        select.select([proc.stdout], [], [], 15)
        line2 = proc.stdout.readline()
        obj = json.loads(line2)
        # Между initialize и ping не должно быть ответа на notification
        assert obj["id"] == 2, f"notification дал ответ: {line2[:200]}"
        assert obj.get("result") == {}
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def await_sync(coro):
    """Запуск корутина из sync-теста."""
    import asyncio
    return asyncio.run(coro)
