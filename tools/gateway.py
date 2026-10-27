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
            try:
                mgr = self._manager_provider()
                turn = None
                async for t in mgr.send_message("Ответь одним словом: ok", agent_id=state.agent_id):
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
            parts.append(f"[assistant] {content}")
        elif role == "tool":
            parts.append(f"[tool_result name={m.get('name', '')}] {content}")
        else:
            parts.append(str(content))
    return "\n\n".join(parts).strip()


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
        if tool_choice == "none":
            tools = []

        prompt = _flatten_messages(messages)
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
