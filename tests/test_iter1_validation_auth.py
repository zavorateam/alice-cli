"""Iteration 1: API key, tool_choice forced 422, strict_explicit both."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class _S:
    def __init__(self, texts):
        self._texts = list(texts)
        self.dialog_id = "d"
        self._active = 0
    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        text = self._texts.pop(0) if self._texts else ""
        yield AliceTurnResponse(dialog_id="d", request_id="r",
                                text=text, is_finished=True)
    async def cancel_current(self):
        return None


class _R:
    def __init__(self, s): self._s = s
    async def acquire(self, sid): return self._s


def _client(texts, status="available"):
    s = _S(texts)
    c = ModelCatalog(manager_provider=lambda: None)
    c.set_status("alice-base", status)
    app = FastAPI()
    attach_openai_routes(app, c, _R(s))
    return TestClient(app), s


# ---------- API key ----------

def test_api_key_missing_returns_401(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions",
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert "Authorization" in r.text


def test_api_key_wrong_returns_401(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client(["x"])
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer wrong"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401
    assert "invalid API key" in r.text


def test_api_key_correct_passes(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client(["ok"])
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": "Bearer secret-key-123"},
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text[:300]


def test_api_key_absent_means_no_auth(monkeypatch):
    monkeypatch.delenv("ALICE_API_KEY", raising=False)
    client, _ = _client(["ok"])
    r = client.post("/v1/chat/completions",
                    json={"model": "alice-base",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_api_key_models_list_protected(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client([])
    r = client.get("/v1/models")
    assert r.status_code == 401


def test_api_key_diagnostics_protected(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client([])
    r = client.get("/v1/models/diagnostics")
    assert r.status_code == 401


def test_api_key_responses_protected(monkeypatch):
    monkeypatch.setenv("ALICE_API_KEY", "secret-key-123")
    client, _ = _client([])
    r = client.post("/v1/responses", json={"model": "alice-base", "input": "x"})
    assert r.status_code == 401


# ---------- tool_choice forced → 422 ----------

TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "w",
                                       "parameters": {"type": "object",
                                                      "properties": {}}}},
    {"type": "function", "function": {"name": "get_time", "description": "t",
                                       "parameters": {"type": "object",
                                                      "properties": {}}}},
]


def test_forced_function_wrong_call_returns_422():
    client, _ = _client([
        '<tool_call>{"tool": "get_weather", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    })
    assert r.status_code == 422, r.text[:300]
    assert r.json()["error"]["type"] == "tool_choice_violated"


def test_forced_function_no_call_returns_422():
    client, _ = _client(["plain text"])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    })
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "tool_choice_violated"


def test_forced_function_correct_call_returns_200():
    client, _ = _client([
        '<tool_call>{"tool": "get_time", "args": {}}</tool_call>',
    ])
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "get_time"}},
    })
    assert r.status_code == 200
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"


# ---------- strict_explicit для обоих ----------

def test_strict_explicit_valid_token_bad_cookies_rejected(tmp_path, monkeypatch):
    from alice_auth import load_credentials_with_cookies
    tok = tmp_path / "token.txt"
    tok.write_text("y0_validtoken\ndevice_uuid=00000000000001610735911788167721\n")
    bad = tmp_path / "cookies.txt"
    bad.write_text("garbage-no-equals-signs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALICE_TOKEN", raising=False)
    monkeypatch.delenv("ALICE_COOKIES", raising=False)
    result = load_credentials_with_cookies(
        token_source=str(tok),
        cookies_source=str(bad),
        strict_explicit=True,
        allow_interactive=False,
    )
    assert result is None, (
        "strict_explicit: битые явные cookies должны давать None, "
        "а не тихо брать только token"
    )


def test_strict_explicit_both_valid_merges(tmp_path, monkeypatch):
    from alice_auth import load_credentials_with_cookies
    tok = tmp_path / "token.txt"
    tok.write_text("y0_validtoken\ndevice_uuid=00000000000001610735911788167721\n")
    ck = tmp_path / "cookies.txt"
    ck.write_text("Session_id=abc123; i=xyz; yandexuid=1234567890")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALICE_TOKEN", raising=False)
    monkeypatch.delenv("ALICE_COOKIES", raising=False)
    result = load_credentials_with_cookies(
        token_source=str(tok),
        cookies_source=str(ck),
        strict_explicit=True,
        allow_interactive=False,
    )
    assert result is not None
    assert result.auth_type == "both"
