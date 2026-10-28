"""OpenAI-совместимый gateway: ModelCatalog + /v1/models + /v1/chat/completions.

Phase 1: модуль самодостаточен (можно attach_openai_routes к FastAPI).
Привязка к alice_server.create_curl_http_app — отдельным патчем.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from tools.effective import EffectiveMode, normalize_effective_mode

logger = logging.getLogger("alice_gateway")


MODEL_TO_AGENT: Dict[str, tuple] = {
    "alice-pro": ("pro", "Pro"),
    "alice-base": ("base", "Base"),
    "alice-expert": ("expert", "External"),
    "alice-deep-research": ("deep_research", "External"),
    "alice-best-price": ("best_price", "External"),
    "alice-taxi": ("taxi", "External"),
    "alice-lavka": ("lavka", "External"),
    "alice-market": ("market", "External"),
    "alice-legal": ("legal", "External"),
    "alice-gas": ("gas", "External"),
}


@dataclass
class ModelState:
    model_id: str
    agent_id: str
    requested_mode: str
    status: str = "implemented"
    effective_mode: Optional[str] = None
    effective_preset: Optional[str] = None
    last_checked: Optional[float] = None
    elapsed_ms: Optional[float] = None
    reason: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    trial_left: Optional[int] = None
    trial_limit: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModelCatalog:
    DEFAULT_TTL = 300.0
    EXPERT_TTL = 6 * 3600.0
    EXPERT_MAX_CHECKS_PER_DAY = 1

    def __init__(self, manager_provider):
        self._manager_provider = manager_provider
        self._states: Dict[str, ModelState] = {}
        self._expert_checks: List[float] = []
        self._lock = asyncio.Lock()
        for mid, (aid, mode) in MODEL_TO_AGENT.items():
            self._states[mid] = ModelState(
                model_id=mid, agent_id=aid, requested_mode=mode,
                capabilities=self._default_caps(mid),
            )

    @staticmethod
    def _default_caps(mid: str) -> List[str]:
        caps = ["chat.completions", "streaming"]
        if mid in ("alice-pro", "alice-expert", "alice-base"):
            caps.append("reasoning")
        if mid == "alice-pro":
            caps.append("tool-calling.pending")
        return caps

    def get_state(self, model_id: str) -> Optional[ModelState]:
        return self._states.get(model_id)

    def list_available(self) -> List[ModelState]:
        return [s for s in self._states.values() if s.status in ("available", "degraded")]

    def list_diagnostics(self) -> List[ModelState]:
        return list(self._states.values())

    def _should_recheck(self, state: ModelState) -> bool:
        if state.last_checked is None:
            return True
        age = time.time() - state.last_checked
        if state.model_id == "alice-expert":
            cutoff = time.time() - 86400
            day_checks = [t for t in self._expert_checks if t > cutoff]
            if len(day_checks) >= self.EXPERT_MAX_CHECKS_PER_DAY:
                return False
            return age >= self.EXPERT_TTL
        return age >= self.DEFAULT_TTL

    async def healthcheck(self, model_id: str) -> Optional[ModelState]:
        state = self._states.get(model_id)
        if state is None:
            return None
        if not self._should_recheck(state):
            return state
        async with self._lock:
            if not self._should_recheck(state):
                return state
            started = time.monotonic()
            ephemeral_id = None
            try:
                mgr = self._manager_provider()
                turn = None
                create = getattr(mgr, "create_new_dialog", None)
                if callable(create):
                    try:
                        ephemeral_id = await create(agent_id=state.agent_id, is_ephemeral=True)
                    except Exception as e:
                        logger.debug(f"healthcheck: create_new_dialog failed: {e!r}")
                        ephemeral_id = None
                send_kwargs = {"agent_id": state.agent_id}
                if ephemeral_id:
                    send_kwargs["dialog_id"] = ephemeral_id
                async for t in mgr.send_message("Ответь одним словом: ok", **send_kwargs):
                    turn = t
                elapsed_ms = (time.monotonic() - started) * 1000.0
                state.elapsed_ms = elapsed_ms
                state.last_checked = time.time()

                if turn is None or getattr(turn, "error", None):
                    state.status = "unavailable"
                    state.reason = getattr(turn, "error", None) if turn else "no response"
                    return state

                eff: Optional[EffectiveMode] = getattr(turn, "effective", None)
                if eff is not None and eff.effective_mode:
                    state.effective_mode = eff.effective_mode
                    state.effective_preset = eff.effective_preset
                    state.trial_left = eff.trial_left
                    state.trial_limit = eff.trial_limit
                    if eff.degraded:
                        state.status = "degraded"
                        state.reason = f"{eff.requested_mode} -> {eff.effective_mode}"
                    else:
                        state.status = "available"
                        state.reason = None
                else:
                    state.status = "degraded"
                    state.reason = "no effective settings in VinsResponse"

                if model_id == "alice-expert":
                    self._expert_checks.append(time.time())
                return state
            except Exception as e:
                state.status = "unavailable"
                state.reason = f"{type(e).__name__}: {e}"
                state.last_checked = time.time()
                return state
            finally:
                if ephemeral_id:
                    try:
                        mgr = self._manager_provider()
                        rpc = getattr(mgr, "rpc", None)
                        if rpc is not None and hasattr(rpc, "delete_dialog"):
                            await rpc.delete_dialog(ephemeral_id)
                    except Exception:
                        pass

    def mark_available(self, model_id: str, effective_mode: Optional[str] = None,
                       effective_preset: Optional[str] = None,
                       trial_left: Optional[int] = None,
                       trial_limit: Optional[int] = None) -> None:
        s = self._states.get(model_id)
        if s is None:
            return
        s.effective_mode = effective_mode
        s.effective_preset = effective_preset
        s.trial_left = trial_left
        s.trial_limit = trial_limit
        s.last_checked = time.time()
        if s.requested_mode and effective_mode and \
                s.requested_mode.lower() != effective_mode.lower():
            s.status = "degraded"
            s.reason = f"{s.requested_mode} -> {effective_mode}"
        else:
            s.status = "available"
            s.reason = None

    def mark_unavailable(self, model_id: str, reason: str) -> None:
        s = self._states.get(model_id)
        if s is None:
            return
        s.status = "unavailable"
        s.reason = reason
        s.last_checked = time.time()

    def warm_up(self) -> "asyncio.Task":
        """Фоновый healthcheck базовых моделей.

        Pro проверяем тоже: без него /v1/models/diagnostics никогда не покажет
        деградацию Pro → Base, а /v1/chat/completions отклонит Pro как implemented.
        Expert не трогаем — у него квота ~5/24ч, healthcheck только по явному запросу
        (TTL 6ч + 1/сутки, см. _should_recheck).
        """
        async def _run():
            for mid in ("alice-base", "alice-pro"):
                try:
                    await self.healthcheck(mid)
                except Exception:
                    pass
        return asyncio.create_task(_run())

    def set_status(self, model_id: str, status: str,
                   reason: Optional[str] = None,
                   effective_mode: Optional[str] = None) -> None:
        s = self._states.get(model_id)
        if s is None:
            return
        s.status = status
        s.reason = reason
        if effective_mode is not None:
            s.effective_mode = effective_mode
        s.last_checked = time.time()

    def to_openai_list(self) -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": s.model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "alice-cli",
                    "alice": {
                        "agent_id": s.agent_id,
                        "requested_mode": s.requested_mode,
                        "effective_mode": s.effective_mode,
                        "status": s.status,
                    },
                }
                for s in self.list_available()
            ],
        }

    def to_diagnostics(self) -> Dict[str, Any]:
        return {
            "object": "list",
            "data": [s.to_dict() for s in self.list_diagnostics()],
        }


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _flatten_messages(messages: List[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        if role in ("system", "developer"):
            parts.append(f"[{role}] {content}")
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                text_part = content.strip() if content else ""
                if text_part:
                    parts.append(f"[assistant] {text_part}")
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    cid = tc.get("id", "")
                    parts.append(f"[tool_call id={cid} name={name}] {args}")
            else:
                parts.append(f"[assistant] {content}")
        elif role == "tool":
            cid = m.get("tool_call_id") or ""
            name = m.get("name") or ""
            parts.append(f"[tool_result id={cid} name={name}] {content}")
        else:
            parts.append(str(content))
    return "\n\n".join(parts).strip()


def _inject_tool_instructions(prompt: str, tools: List[dict], tool_choice: str) -> str:
    """Добавляет к prompt явные определения инструментов и контракт вызова.

    Без этого Алиса не знает, какие функции ей доступны — клиент передал tools,
    а до модели они не доходят. Формат — как в tool_bridge.py (JSON-маркер),
    но с явным списком имён и схем из запроса.
    """
    lines = ["[SYSTEM — доступные инструменты]"]
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        lines.append(f"- {name}: {desc}")
        try:
            lines.append("  schema: " + json.dumps(params, ensure_ascii=False))
        except Exception:
            lines.append("  schema: <unserializable>")
    lines.append("")
    if tool_choice == "required":
        lines.append("ПРАВИЛО: вызови ровно один инструмент из списка выше.")
    else:
        lines.append("ПРАВИЛО: если нужен инструмент — верни ТОЛЬКО маркер ниже.")
    lines.append('Формат вызова: <tool_call>{"tool": "<имя>", "args": {...}}</tool_call>')
    lines.append("Не выдумывай имена вне списка. Не пиши текст вокруг маркера.")
    lines.append("[КОНЕЦ SYSTEM]")
    lines.append("")
    return "\n".join(lines) + prompt


def _extract_tool_calls(text: str, tools: List[dict]) -> List[dict]:
    import re
    allowed = {(t.get("function") or {}).get("name") for t in tools}
    calls: List[dict] = []
    for raw in re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text or "", re.DOTALL):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("args") or obj.get("arguments") or {}
        if not name or name not in allowed:
            continue
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:16]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


def attach_openai_routes(app, catalog: ModelCatalog, session_registry) -> None:
    """Регистрирует /v1/* на FastAPI-приложение.

    POST /v1/chat/completions регистрируется как сырой Starlette Route:
    FastAPI 0.141 + Starlette 1.x ошибочно трактует `request: Request`
    как body-параметр и отвечает 422 ещё до хендлера. Через starlette.routing.Route
    вызова идёт напрямую, минуя FastAPI param analysis.
    """
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    @app.get("/v1/models")
    async def list_models():
        return catalog.to_openai_list()

    @app.get("/v1/models/diagnostics")
    async def list_models_diagnostics():
        return catalog.to_diagnostics()

    async def _chat_completions(request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "invalid JSON", "type": "invalid_request_error"}},
                status_code=400,
            )
        model = body.get("model")
        messages = body.get("messages") or []
        stream = bool(body.get("stream"))
        tools = body.get("tools") or []
        tool_choice = body.get("tool_choice") or "auto"
        session_id = request.headers.get("X-Alice-Session-ID") or (body.get("metadata") or {}).get("session_id")

        if not model or not messages:
            return JSONResponse(
                {"error": {"message": "model and messages are required", "type": "invalid_request_error"}},
                status_code=400,
            )
        state = catalog.get_state(model)
        if state is None:
            return JSONResponse(
                {"error": {"message": f"model {model!r} not found", "type": "invalid_request_error"}},
                status_code=404,
            )
        # Контракт: implemented/unknown ещё не подтверждены и не публикуются
        # в /v1/models — значит, и в chat их использовать нельзя.
        # available и degraded (Pro→Base) разрешены; degraded явно, но не притворяемся Pro.
        if state.status in ("implemented", "unknown"):
            return JSONResponse(
                {"error": {"message": f"model {model!r} has not been verified yet; "
                                       f"call /v1/models/diagnostics or wait for warm_up",
                           "type": "model_not_verified"}},
                status_code=503,
            )
        if state.status == "unavailable":
            return JSONResponse(
                {"error": {"message": f"model {model!r} is currently unavailable: {state.reason}",
                           "type": "model_unavailable"}},
                status_code=503,
            )
        _VALID_TOOL_CHOICES = {"auto", "none", "required"}
        if isinstance(tool_choice, str) and tool_choice not in _VALID_TOOL_CHOICES:
            return JSONResponse(
                {"error": {"message": f"tool_choice={tool_choice!r} is not supported; "
                                       f"use auto, none or required",
                           "type": "invalid_request_error"}},
                status_code=400,
            )
        if tool_choice == "none":
            tools = []

        prompt = _flatten_messages(messages)
        if tools:
            prompt = _inject_tool_instructions(prompt, tools, tool_choice)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        agent_id = state.agent_id

        try:
            session = await session_registry.acquire(session_id)
        except Exception as e:
            return JSONResponse(
                {"error": {"message": str(e), "type": "server_error"}},
                status_code=500,
            )

        if not stream:
            last = None
            try:
                async for turn in session.send_message(prompt, agent_id=agent_id):
                    if getattr(turn, "error", None):
                        return JSONResponse(
                            {"error": {"message": turn.error, "type": "upstream_error"}},
                            status_code=502,
                        )
                    last = turn
            except Exception as e:
                return JSONResponse(
                    {"error": {"message": str(e), "type": "server_error"}},
                    status_code=500,
                )
            text = last.text if last else ""
            eff = getattr(last, "effective", None) if last else None
            if eff is not None and getattr(eff, "effective_mode", None):
                catalog.mark_available(
                    model,
                    effective_mode=eff.effective_mode,
                    effective_preset=eff.effective_preset,
                    trial_left=eff.trial_left,
                    trial_limit=eff.trial_limit,
                )
            tool_calls = _extract_tool_calls(text, tools)
            message: Dict[str, Any] = {"role": "assistant", "content": None if tool_calls else text}
            finish_reason = "tool_calls" if tool_calls else "stop"
            if tool_calls:
                message["tool_calls"] = tool_calls
            return JSONResponse({
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            })

        async def streamer():
            yield _sse({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            sent = 0
            # Когда клиент передал tools, финальный текст может содержать
            # <tool_call>...</tool_call>. В этом случае контент не стримим по кускам,
            # а ждём is_finished и отдаём delta.tool_calls + finish_reason="tool_calls".
            buffering_for_tools = bool(tools)
            try:
                async for turn in session.send_message(prompt, agent_id=agent_id):
                    if getattr(turn, "error", None):
                        yield _sse({
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        })
                        yield "data: [DONE]\n\n"
                        return
                    if buffering_for_tools:
                        # ждём is_finished, ничего не стримим
                        if getattr(turn, "is_finished", False):
                            full_text = turn.text or ""
                            tool_calls = _extract_tool_calls(full_text, tools)
                            if tool_calls:
                                # Убираем маркеры из видимого контента — они не должны
                                # утечь как обычный текст при finish_reason="tool_calls".
                                import re as _re
                                clean_text = _re.sub(
                                    r"<tool_call>\s*\{.*?\}\s*</tool_call>",
                                    "", full_text, flags=_re.DOTALL,
                                ).strip()
                                if clean_text:
                                    yield _sse({
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [{"index": 0,
                                                     "delta": {"content": clean_text},
                                                     "finish_reason": None}],
                                    })
                                delta_calls = []
                                for i, tc in enumerate(tool_calls):
                                    delta_calls.append({
                                        "index": i,
                                        "id": tc["id"],
                                        "type": tc["type"],
                                        "function": tc["function"],
                                    })
                                yield _sse({
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0,
                                                 "delta": {"tool_calls": delta_calls},
                                                 "finish_reason": None}],
                                })
                                yield _sse({
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "tool_calls"}],
                                })
                            else:
                                if full_text:
                                    yield _sse({
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [{"index": 0,
                                                     "delta": {"content": full_text},
                                                     "finish_reason": None}],
                                    })
                                yield _sse({
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {},
                                                 "finish_reason": "stop"}],
                                })
                            yield "data: [DONE]\n\n"
                            return
                        continue

                    # Обычный streaming (без tools)
                    new = turn.text[sent:]
                    sent = len(turn.text)
                    if new:
                        yield _sse({
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": new}, "finish_reason": None}],
                        })
                    if getattr(turn, "is_finished", False):
                        yield _sse({
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        })
                        yield "data: [DONE]\n\n"
                        return
            except asyncio.CancelledError:
                await session.cancel_current()
                raise

        return StreamingResponse(streamer(), media_type="text/event-stream")

    app.router.routes.append(
        Route("/v1/chat/completions", _chat_completions, methods=["POST"])
    )
