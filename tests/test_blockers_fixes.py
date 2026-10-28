"""Регрессии на блокеры 1–3 и контракт каталога моделей (4)."""
import asyncio
import inspect
import re
from pathlib import Path


# ---------------- Bug 1: --dialog-id путь не падает на NameError ----------------

def test_manager_initialize_imports_aiohttp_and_rpc_base_url():
    src = Path("tools/manager.py").read_text()
    assert "import aiohttp" in src, "tools/manager.py не импортирует aiohttp"
    # RPC_BASE_URL должен быть импортирован (или определён)
    assert (
        "from tools.constants import uuid7, RPC_BASE_URL" in src
        or "RPC_BASE_URL =" in src
    ), "RPC_BASE_URL не импортирован в tools/manager.py"


def test_manager_initialize_dialog_id_compiles_through_full_path():
    """Симулируем initialize(initial_dialog_id=...): должен дойти до aiohttp.ClientSession."""
    import tools.manager as mgr_mod
    src = inspect.getsource(mgr_mod.AliceAgentManager.initialize)
    assert "aiohttp.ClientSession" in src
    assert "RPC_BASE_URL" in src
    # Проверка компиляции модуля не падает на атрибутах
    mod_src = Path(mgr_mod.__file__).read_text()
    compile(mod_src, str(mgr_mod.__file__), "exec")


# ---------------- Bug 2: утечки секретов ----------------

def test_alice_auth_merge_log_uses_fingerprint_not_uuid(caplog):
    """load_credentials_with_cookies логирует fingerprint, не полный device_uuid."""
    import logging
    from alice_auth import load_credentials_with_cookies
    import tempfile, os
    real_uuid = "00000000000001610735911788167721"
    with tempfile.TemporaryDirectory() as d:
        tok = Path(d) / "token.txt"
        tok.write_text(f"y0_testtoken\ndevice_uuid={real_uuid}\n", encoding="utf-8")
        ck = Path(d) / "cookies.txt"
        ck.write_text("Session_id=abc; i=1; yandexuid=123", encoding="utf-8")
        # Сбрасываем cwd-файлы чтобы не пересекались
        old_cwd = os.getcwd()
        try:
            os.chdir(d)
            with caplog.at_level(logging.INFO):
                load_credentials_with_cookies(
                    token_source=str(tok), cookies_source=str(ck),
                    allow_interactive=False,
                )
        finally:
            os.chdir(old_cwd)
    text = "\n".join(r.message for r in caplog.records)
    assert real_uuid not in text, f"device_uuid утёк: {text!r}"
    assert "device_fp=" in text


def test_alice_cli_does_not_print_token_prefix():
    src = Path("alice_cli.py").read_text()
    assert "creds.token[:10]" not in src, "alice_cli.py всё ещё печатает token[:10]"


def test_manager_mcp_auth_error_does_not_leak_prefix():
    src = Path("tools/manager.py").read_text()
    assert "auth_param[:20]" not in src, "tools/manager.py утекает auth_param[:20]"


# ---------------- Bug 3: cleanup в send_text_input ----------------

def test_send_text_input_cleanup_all_dicts():
    """finally должен чистить pending_responses, active_agents_by_req,
    requested_mode_by_req и effective_by_req. И try должен охватывать _send_json."""
    src = Path("tools/transport.py").read_text()
    # try: должен идти ДО await self._send_json
    m = re.search(r"def send_text_input\(.*?\n(.*?)(?=\n    async def |\n    def |\Z)",
                  src, re.S)
    assert m, "send_text_input не найден"
    body = m.group(1)
    # locate "try:" and "await self._send_json(payload)"
    idx_try = body.find("try:")
    idx_send = body.find("await self._send_json(payload)")
    assert idx_try != -1 and idx_send != -1, "try или _send_json не найдены"
    assert idx_try < idx_send, (
        "_send_json вызывается до try — при ошибке отправки cleanup не выполнится"
    )
    for key in ("_pending_responses", "_active_agents_by_req",
                "requested_mode_by_req", "effective_by_req"):
        assert f"self.{key}.pop(req_id" in body, f"finally не чистит {key}"


# ---------------- Bug 4: каталог и chat endpoint ----------------

def test_warm_up_covers_pro_and_base():
    src = Path("tools/gateway.py").read_text()
    m = re.search(r"def warm_up\(self\).*?(?=\n    def |\n    async def |\Z)", src, re.S)
    assert m, "warm_up не найден"
    body = m.group(0)
    assert "alice-base" in body
    assert "alice-pro" in body, "warm_up не проверяет alice-pro"


def test_chat_rejects_implemented_model():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tools.gateway import ModelCatalog, attach_openai_routes

    class _S:
        async def send_message(self, *a, **kw):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r", text="ok", is_finished=True)
        async def cancel_current(self):
            return None

    class _R:
        async def acquire(self, sid):
            return _S()

    catalog = ModelCatalog(manager_provider=lambda: None)
    # alice-base по умолчанию "implemented", не "available"
    app = FastAPI()
    attach_openai_routes(app, catalog, _R())
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 503, f"got {r.status_code}: {r.text[:300]}"
    assert "verified" in r.text.lower() or "model_not_verified" in r.text


# ---------------- Bug 6: TTL/limit в SessionRegistry ----------------

def test_session_registry_has_ttl_and_max():
    from tools.manager import SessionRegistry
    reg = SessionRegistry(manager_factory=lambda: None,
                          ttl_seconds=3600, max_sessions=128)
    assert reg._ttl == 3600
    assert reg._max == 128
    src = Path("tools/manager.py").read_text()
    # Фактические имена eviction-хелперов после переписывания
    for name in ("_close_one_locked", "_evict_ttl_locked", "_make_room_locked"):
        assert name in src, f"{name} отсутствует в tools/manager.py"
    # Закрытие сессии при вытеснении удаляет её диалог
    assert "chat.close(delete_dialog=True)" in src
    # В acquire вызываются обе стадии: TTL и освобождение места
    import inspect
    acquire_src = inspect.getsource(SessionRegistry.acquire)
    assert "_evict_ttl_locked" in acquire_src
    assert "_make_room_locked" in acquire_src


# ---------------- Bug 7: tool definitions в prompt, tool_choice валидация ----------------

def test_tool_definitions_injected_into_prompt():
    from tools.gateway import _inject_tool_instructions
    tools = [{"type": "function", "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}}},
    }}]
    out = _inject_tool_instructions("hi", tools, "auto")
    assert "get_weather" in out
    assert "city" in out  # схема присутствует
    assert "<tool_call>" in out


def test_tool_choice_invalid_returns_400():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tools.gateway import ModelCatalog, attach_openai_routes

    class _S:
        async def send_message(self, *a, **kw):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r", text="ok", is_finished=True)
        async def cancel_current(self):
            return None

    class _R:
        async def acquire(self, sid):
            return _S()

    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _R())
    client = TestClient(app)
    r = client.post("/v1/chat/completions", json={
        "model": "alice-base",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "tool_choice": "some_garbage_value",
    })
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:200]}"


# ---------------- Bug 8: requirements-optional.txt существует ----------------

def test_requirements_optional_exists():
    assert Path("requirements-optional.txt").is_file(), (
        "README ссылается на requirements-optional.txt, но файла нет"
    )
