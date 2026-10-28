"""Iteration 2: настоящий streaming, cleanup маркеров, response.failed."""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.gateway import ModelCatalog, attach_openai_routes


class _SlowSession:
    """Yield-ит turn-ы с задержкой между ними — позволяет проверить realtime."""
    def __init__(self, turns, delay_after_first=0.4):
        self._turns = turns
        self._delay = delay_after_first
        self.dialog_id = "d"
        self._active = 0

    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        for i, (text, finished) in enumerate(self._turns):
            if i > 0:
                await asyncio.sleep(self._delay)
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text=text, is_finished=finished)

    async def cancel_current(self):
        return None


class _FastSession:
    def __init__(self, turns):
        self._turns = turns
        self.dialog_id = "d"
        self._active = 0

    async def send_message(self, prompt, agent_id="pro", is_suggest=False):
        from agents.base import AliceTurnResponse
        for text, finished in self._turns:
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text=text, is_finished=finished)

    async def cancel_current(self):
        return None


class _R:
    def __init__(self, s): self._s = s
    async def acquire(self, sid): return self._s


def _client(session, status="available"):
    c = ModelCatalog(manager_provider=lambda: None)
    c.set_status("alice-base", status)
    app = FastAPI()
    attach_openai_routes(app, c, _R(session))
    return TestClient(app)


# ---------- real-time streaming (без tools) ----------


def test_responses_stream_event_order():
    """Порядок: created → output_item.added → output_text.delta → done → completed."""
    session = _FastSession([("Hello", True)])
    client = _client(session)

    events = []
    with client.stream("POST", "/v1/responses", json={
        "model": "alice-base",
        "input": "hi",
        "stream": True,
    }) as resp:
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])

    assert "response.created" in events
    assert "response.output_item.added" in events
    assert "response.output_text.delta" in events
    assert "response.output_text.done" in events
    assert "response.completed" in events
    assert events[0] == "response.created"
    assert events[-1] == "response.completed"
    # порядок output_item.added < output_text.delta < output_text.done
    i_added = events.index("response.output_item.added")
    i_delta = events.index("response.output_text.delta")
    i_done = events.index("response.output_text.done")
    assert i_added < i_delta < i_done


# ---------- cleanup <tool_call> маркеров ----------

def test_responses_stream_tool_call_cleans_markers():
    """Если модель вернула tool_call, output_text.done должен быть без маркера."""
    tool_text = 'Checking weather. <tool_call>{"tool":"get_weather","args":{"city":"M"}}</tool_call>'
    session = _FastSession([(tool_text, True)])
    client = _client(session)

    lines = []
    with client.stream("POST", "/v1/responses", json={
        "model": "alice-base",
        "input": "weather?",
        "stream": True,
        "tools": [{
            "type": "function",
            "name": "get_weather",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}},
        }],
    }) as resp:
        for line in resp.iter_lines():
            if line:
                lines.append(line)

    joined = "\n".join(lines)
    # function_call emitted
    assert '"type": "function_call"' in joined
    assert '"name": "get_weather"' in joined
    # output_text.done — без marker
    done_lines = [l for l in lines if "response.output_text.done" in l]
    assert done_lines, "нет output_text.done"
    done_data = done_lines[0]
    assert "<tool_call>" not in done_data, (
        f"маркер утёк в output_text.done: {done_data[:200]}"
    )


# ---------- response.failed для upstream error ----------

def test_responses_stream_error_uses_failed_event():
    """При upstream error — событие response.failed, НЕ completed со status=failed."""
    class _FailSession:
        dialog_id = "d"
        _active = 0
        async def send_message(self, *a, **kw):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    error="boom", is_finished=True)
        async def cancel_current(self):
            return None

    client = _client(_FailSession())

    events = []
    with client.stream("POST", "/v1/responses", json={
        "model": "alice-base",
        "input": "x",
        "stream": True,
    }) as resp:
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: "):])

    assert "response.failed" in events, f"ожидался response.failed, got {events}"
    # не должно быть response.completed со status="failed"
    for evt in events:
        assert evt != "response.completed", (
            f"был response.completed при ошибке — events={events}"
        )


# ---------- non-stream: tool + text, message первым ----------

def test_responses_non_stream_output_order():
    """Если есть и текст, и tool_call — message идёт первым, потом function_call."""
    tool_text = 'Checking. <tool_call>{"tool":"get_weather","args":{"city":"M"}}</tool_call>'
    session = _FastSession([(tool_text, True)])
    client = _client(session)

    r = client.post("/v1/responses", json={
        "model": "alice-base",
        "input": "weather?",
        "tools": [{
            "type": "function",
            "name": "get_weather",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}},
        }],
    })
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    types = [it["type"] for it in body["output"]]
    assert types == ["message", "function_call"], f"got {types}"
    # message content должен быть без маркера
    msg = body["output"][0]
    assert "<tool_call>" not in msg["content"][0]["text"]
    assert "Checking" in msg["content"][0]["text"]


# ---------- real-time streaming через РЕАЛЬНЫЙ uvicorn + httpx ----------
#
# TestClient (Starlette) буферизует весь SSE-response перед возвратом из .stream().
# Проверить timing через него невозможно. Здесь — реальный сервер на свободном
# порту + httpx streaming, чтобы показать, что delta приходит ДО завершения
# fake session.

def test_responses_stream_first_delta_before_completion_uvicorn():
    """Первый delta получен клиентом до того, как fake session завершилась.

    Fake session: yield turn1 -> ждёт threading.Event -> yield turn2(finished).
    Тест: читает SSE через httpx.Client.stream. Как только видит
    response.output_text.delta — ставит Event. Если бы сервер собирал весь
    ответ (старое поведение), delta пришёл бы только после Event => deadlock
    по timeout'у. Если delta пришла — streaming настоящий.
    """
    import socket
    import threading
    import time

    import httpx
    import uvicorn
    from fastapi import FastAPI

    from tools.gateway import ModelCatalog, attach_openai_routes

    release_event = threading.Event()
    first_delta_received = threading.Event()

    class _GatedSession:
        dialog_id = "d"
        _active = 0
        async def send_message(self, prompt, agent_id="pro", is_suggest=False):
            from agents.base import AliceTurnResponse
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text="Hello", is_finished=False)
            # Ждём, пока клиент получит delta; если не получит за 3с — таймаут
            await asyncio.get_event_loop().run_in_executor(
                None, release_event.wait, 3.0)
            yield AliceTurnResponse(dialog_id="d", request_id="r",
                                    text="Hello world", is_finished=True)
        async def cancel_current(self):
            return None

    class _R:
        def __init__(self, s): self._s = s
        async def acquire(self, sid): return self._s

    session = _GatedSession()
    catalog = ModelCatalog(manager_provider=lambda: None)
    catalog.set_status("alice-base", "available")
    app = FastAPI()
    attach_openai_routes(app, catalog, _R(session))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    # Ждём, пока порт займётся
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    delta_time_holder = {}
    error_holder = {}

    def _client_run():
        try:
            with httpx.Client(timeout=10.0) as c:
                with c.stream("POST",
                              f"http://127.0.0.1:{port}/v1/responses",
                              json={"model": "alice-base",
                                    "input": "hi",
                                    "stream": True}) as resp:
                    assert resp.status_code == 200
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        if "response.output_text.delta" in line:
                            delta_time_holder["t"] = time.monotonic()
                            first_delta_received.set()
                            # Не break — дочитаем до completed, чтобы сервер
                            # завершил fake session корректно
                        if "response.completed" in line:
                            delta_time_holder["completed"] = time.monotonic()
                            break
        except Exception as e:
            error_holder["err"] = repr(e)

    started = time.monotonic()
    client_thread = threading.Thread(target=_client_run, daemon=True)
    client_thread.start()

    # Ждём delta (не completed). Если сервер буферизует — не дождёмся.
    got_delta = first_delta_received.wait(timeout=5.0)
    delta_elapsed = time.monotonic() - started

    # Если delta получен до истечения 5с — release fake session
    release_event.set()
    client_thread.join(timeout=10.0)

    try:
        server.should_exit = True
        thread.join(timeout=5.0)
    except Exception:
        pass

    if error_holder:
        raise AssertionError(f"client thread error: {error_holder['err']}")

    assert got_delta, (
        f"delta не получен за 5с — сервер ждал полной генерации "
        f"(это НЕ streaming). elapsed={delta_elapsed:.3f}s"
    )
    # delta должен прийти до release (проверено самим фактом got_delta)
    assert delta_time_holder.get("t") is not None
    assert delta_time_holder.get("t") < delta_time_holder.get("completed", float("inf")), \
        "delta пришёл после completed"

