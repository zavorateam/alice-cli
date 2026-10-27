"""Реальный MCP stdio subprocess: не in-process имитация.

Запускает alice_server.py с cwd в tmp_path (без token.txt → MCP dynamic mode),
гоняет JSON-RPC через реальные pipe. Проверяет, что:
  - initialize / ping / tools/list / unknown method работают по протоколу;
  - stdout — только JSON-RPC, логи — только в stderr;
  - tools/list непустой даже без creds (registry должен заполняться
    в dynamic mode, иначе клиенту нечего вызывать).
"""
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="select on text pipes requires POSIX",
)


@pytest.fixture
def mcp_proc(tmp_path):
    env = os.environ.copy()
    for k in ("ALICE_TOKEN", "YANDEX_TOKEN", "ALICE_COOKIES"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "alice_server.py")],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        yield proc
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


def _readline(proc, timeout=15.0):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        err = ""
        try:
            err = proc.stderr.read(2000)
        except Exception:
            pass
        raise AssertionError(f"no response in {timeout}s; stderr={err[:500]!r}")
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read(2000)
        raise AssertionError(f"EOF from MCP; stderr={err[:800]!r}")
    return line


def _send(proc, method, params=None, req_id=1, timeout=15.0):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return json.loads(_readline(proc, timeout))


def _init(proc, req_id=1):
    return _send(proc, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    }, req_id=req_id)


def test_initialize_returns_server_info(mcp_proc):
    resp = _init(mcp_proc)
    assert "result" in resp, resp
    info = resp["result"]["serverInfo"]
    assert info["name"] == "yandex-alice-server"
    assert "tools" in resp["result"]["capabilities"]


def test_ping(mcp_proc):
    _init(mcp_proc)
    resp = _send(mcp_proc, "ping", req_id=2)
    assert resp.get("result") == {}


def test_tools_list_not_empty_in_dynamic_mode(mcp_proc):
    """В no-creds режиме registry ДОЛЖЕН быть заполнен, иначе клиенту нечего вызывать."""
    _init(mcp_proc)
    resp = _send(mcp_proc, "tools/list", req_id=2)
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names, "tools/list пуст в dynamic mode — registry не заполнен"
    # базовые model-агенты должны быть в списке
    assert "alice_ask_pro" in names
    assert "alice_ask_base" in names
    assert "alice_ask_expert" in names
    pro = next(t for t in tools if t["name"] == "alice_ask_pro")
    assert pro["inputSchema"]["type"] == "object"
    assert "message" in pro["inputSchema"]["properties"]


def test_unknown_method_returns_method_not_found(mcp_proc):
    _init(mcp_proc)
    resp = _send(mcp_proc, "nonexistent/method", req_id=2)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_stdout_is_pure_json_stderr_has_logs(mcp_proc):
    _init(mcp_proc)
    _send(mcp_proc, "ping", req_id=2)
    mcp_proc.stdin.close()
    try:
        mcp_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mcp_proc.kill()
        mcp_proc.wait()

    stdout = mcp_proc.stdout.read()
    stderr = mcp_proc.stderr.read()
    for line in stdout.splitlines():
        if line.strip():
            assert line.strip().startswith("{"), f"non-JSON in stdout: {line[:200]!r}"
    assert stderr.strip(), "stderr пуст — а должен содержать логи"
    # в stderr должны быть info-логи из logging
    assert any(kw in stderr for kw in ("MCP", "Healthcheck", "INFO", "Учетные"))
