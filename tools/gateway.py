"""OpenAI-совместимый gateway: ModelCatalog + /v1/models + /v1/chat/completions.

Phase 1: модуль самодостаточен (можно attach_openai_routes к FastAPI).
Привязка к alice_server.create_curl_http_app — отдельным патчем.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
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


class ResponseStore:
    """LRU+TTL хранилище response_id → {dialog_id, model, session_id}.

    Используется для Responses API `previous_response_id`.
    Ограничен: TTL (по умолчанию 1 час), MAX_ENTRIES (по умолчанию 256).
    Eviction lazy, при `put()`/`get()`.
    """

    DEFAULT_TTL = 3600.0
    DEFAULT_MAX = 256

    def __init__(self, ttl_seconds: Optional[float] = None,
                 max_entries: int = DEFAULT_MAX):
        self._ttl = self.DEFAULT_TTL if ttl_seconds is None else ttl_seconds
        self._max = max_entries
        # OrderedDict для LRU: oldest first
        from collections import OrderedDict
        self._data: "OrderedDict[str, dict]" = OrderedDict()

    def _expire(self) -> None:
        import time as _time
        if self._ttl <= 0:
            return
        now = _time.time()
        stale = [k for k, v in self._data.items() if now - v.get("ts", 0) > self._ttl]
        for k in stale:
            self._data.pop(k, None)

    def put(self, response_id: str, dialog_id: str, model: str,
            session_id: Optional[str]) -> None:
        import time as _time
        self._expire()
        if self._max > 0 and len(self._data) >= self._max:
            # выкинуть самый старый
            self._data.popitem(last=False)
        self._data[response_id] = {
            "dialog_id": dialog_id,
            "model": model,
            "session_id": session_id,
            "ts": _time.time(),
        }
        # обновить позицию (свежий — в конец)
        self._data.move_to_end(response_id)

    def get(self, response_id: str) -> Optional[dict]:
        self._expire()
        entry = self._data.get(response_id)
        if entry is None:
            return None
        # обновить позицию для LRU
        self._data.move_to_end(response_id)
        return entry

    def __len__(self) -> int:
        return len(self._data)


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


def _forced_function_name(tool_choice) -> Optional[str]:
    """Возвращает имя функции, если tool_choice задаёт конкретную функцию."""
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") != "function":
        return None
    fn = tool_choice.get("function") or {}
    name = fn.get("name")
    return name if isinstance(name, str) and name else None


def _validate_tool_request(tools: List[dict], tool_choice,
                            parallel_tool_calls) -> Optional[dict]:
    """Валидация tools + tool_choice. Возвращает error_dict или None."""
    if not tools:
        if tool_choice == "required":
            return {"error": {"message": "tool_choice='required' but tools is empty",
                              "type": "invalid_request_error"}}
        if isinstance(tool_choice, dict):
            return {"error": {"message": "tool_choice specifies a function but tools is empty",
                              "type": "invalid_request_error"}}
        return None
    names = set()
    for i, t in enumerate(tools):
        if not isinstance(t, dict):
            return {"error": {"message": f"tools[{i}] is not an object",
                              "type": "invalid_request_error"}}
        if t.get("type") != "function":
            return {"error": {"message": f"tools[{i}].type must be 'function'",
                              "type": "invalid_request_error"}}
        fn = t.get("function") or {}
        if not isinstance(fn, dict):
            return {"error": {"message": f"tools[{i}].function is not an object",
                              "type": "invalid_request_error"}}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return {"error": {"message": f"tools[{i}].function.name is required",
                              "type": "invalid_request_error"}}
        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", name):
            return {"error": {"message": f"tools[{i}].function.name {name!r} has invalid chars",
                              "type": "invalid_request_error"}}
        if name in names:
            return {"error": {"message": f"duplicate function name {name!r}",
                              "type": "invalid_request_error"}}
        names.add(name)
    if isinstance(tool_choice, dict):
        forced = _forced_function_name(tool_choice)
        if not forced:
            return {"error": {"message": "tool_choice.function.name is required",
                              "type": "invalid_request_error"}}
        if forced not in names:
            return {"error": {"message": f"tool_choice references unknown function {forced!r}",
                              "type": "invalid_request_error"}}
    return None


def _extract_tool_calls(text: str, tools: List[dict],
                        tool_choice="auto") -> List[dict]:
    """Парсит <tool_call>{...}</tool_call> и возвращает OpenAI tool_calls.

    - Имя из args должно быть в tools (или в forced function, если tool_choice dict).
    - Если tool_choice задаёт конкретную функцию — вызовы других отбрасываются.
    """
    allowed = {(t.get("function") or {}).get("name") for t in tools}
    forced = _forced_function_name(tool_choice)
    if forced:
        allowed = {forced}
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


def _validate_response_format(rf) -> tuple:
    """Возвращает (prompt_suffix, error_dict_or_None).

    rf может быть:
      - None / {"type": "text"} → no-op
      - {"type": "json_object"} → инструкция "answer only with valid JSON"
      - {"type": "json_schema", "json_schema": {...}} → инструкция с schema
    Неизвестный тип → error_dict.
    """
    if rf is None:
        return "", None
    if not isinstance(rf, dict):
        return "", {"error": {"message": "response_format must be an object",
                              "type": "invalid_request_error"}}
    t = rf.get("type")
    if t in (None, "text"):
        return "", None
    if t == "json_object":
        return (
            "\n\n[system] Answer ONLY with a single valid JSON object. "
            "No markdown, no code fences, no prose before or after.",
            None,
        )
    if t == "json_schema":
        js = rf.get("json_schema") or {}
        schema = js.get("schema")
        if not isinstance(schema, dict):
            return "", {"error": {"message": "response_format.json_schema.schema "
                                              "is required and must be an object",
                                  "type": "invalid_request_error"}}
        try:
            schema_repr = json.dumps(schema, ensure_ascii=False)
        except Exception:
            return "", {"error": {"message": "response_format.json_schema.schema "
                                              "is not JSON-serializable",
                                  "type": "invalid_request_error"}}
        strict = bool(js.get("strict"))
        instr = (
            "\n\n[system] Answer ONLY with a single valid JSON object "
            "conforming exactly to this JSON Schema. No markdown, no prose."
            f"\nSchema: {schema_repr}"
        )
        if strict:
            instr += "\nThe response MUST be strictly valid against the schema."
        return instr, None
    return "", {"error": {"message": f"response_format.type={t!r} is not supported "
                                      f"(supported: text, json_object, json_schema)",
                          "type": "invalid_request_error"}}


async def _run_alice_collect(session, prompt, agent_id, tools, tool_choice,
                            catalog, model):
    """Собирает финальный ответ Алисы, обновляет каталог.

    Возвращает (text, tool_calls, alice_turn).
    Бросает RuntimeError при upstream-ошибке.
    """
    last = None
    async for turn in session.send_message(prompt, agent_id=agent_id):
        if getattr(turn, "error", None):
            raise RuntimeError(turn.error)
        last = turn
    if last is None:
        raise RuntimeError("no response from Alice")
    text = last.text or ""
    eff = getattr(last, "effective", None)
    if eff is not None and getattr(eff, "effective_mode", None):
        catalog.mark_available(
            model,
            effective_mode=eff.effective_mode,
            effective_preset=eff.effective_preset,
            trial_left=eff.trial_left,
            trial_limit=eff.trial_limit,
        )
    tool_calls = _extract_tool_calls(text, tools, tool_choice) if tools else []
    return text, tool_calls, last


def _flatten_responses_input(inp) -> str:
    """Responses `input` → prompt для Алисы.

    `input` может быть:
      - str: просто текст пользователя
      - list[item]: {type:"message", role, content:[{type:"input_text", text}]}
                    {type:"function_call_output", call_id, output}
      - list[message]: старый формат Chat Completions (role/content) —
                       принимаем для совместимости
    """
    if isinstance(inp, str):
        return inp
    if not isinstance(inp, list):
        raise ValueError("input must be a string or an array of items")
    parts = []
    for i, item in enumerate(inp):
        if not isinstance(item, dict):
            raise ValueError(f"input[{i}] must be an object")
        itype = item.get("type")
        if itype == "function_call_output":
            call_id = item.get("call_id") or ""
            output = item.get("output") or ""
            if isinstance(output, list):
                # {type: "input_text", text}
                output = " ".join(c.get("text", "") for c in output
                                  if isinstance(c, dict))
            parts.append(f"[tool_output id={call_id}] {output}")
            continue
        if itype == "message" or "role" in item:
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") in
                    ("input_text", "output_text", "text")
                )
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            if role in ("system", "developer"):
                parts.append(f"[{role}] {text}")
            elif role == "assistant":
                parts.append(f"[assistant] {text}")
            else:
                parts.append(text)
            continue
        raise ValueError(
            f"input[{i}].type={itype!r} is not supported "
            f"(supported: message, function_call_output)"
        )
    return "\n\n".join(parts).strip()


def _new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _format_responses_output(text: str, tool_calls: list, tools_in: list) -> list:
    """Формирует output items для Responses API.

    Возвращает список items: function_call items (если есть tool_calls) +
    message item с output_text.
    """
    out = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        out.append({
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex[:20]}",
            "call_id": tc["id"],
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        })
    # Если есть только tool_calls и нет текста — message не добавляем.
    # Если есть текст — добавляем message с output_text.
    if text:
        out.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:20]}",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": text,
                "annotations": [],
            }],
        })
    return out


def attach_openai_routes(app, catalog: ModelCatalog, session_registry) -> None:
    """Регистрирует /v1/* на FastAPI-приложение.

    POST /v1/chat/completions регистрируется как сырой Starlette Route:
    FastAPI 0.141 + Starlette 1.x ошибочно трактует `request: Request`
    как body-параметр и отвечает 422 ещё до хендлера. Через starlette.routing.Route
    вызова идёт напрямую, минуя FastAPI param analysis.
    """
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    response_store = ResponseStore()

    @app.get("/v1/models")
    async def list_models():
        return catalog.to_openai_list()

    @app.get("/v1/models/diagnostics")
    async def list_models_diagnostics():
        return catalog.to_diagnostics()

    async def _model_detail(request):
        # starlette.routing.Route НЕ оборачивает dict в JSONResponse
        # (это делает только FastAPI через @app.get). Возвращаем Response явно.
        from fastapi.responses import JSONResponse
        model_id = request.path_params["model_id"]
        state = catalog.get_state(model_id)
        if state is None:
            return JSONResponse(
                {"error": {"message": f"model {model_id!r} not found",
                           "type": "invalid_request_error"}},
                status_code=404,
            )
        return JSONResponse({
            "id": state.model_id,
            "object": "model",
            "created": 0,
            "owned_by": "alice-cli",
            "alice": {
                "agent_id": state.agent_id,
                "requested_mode": state.requested_mode,
                "effective_mode": state.effective_mode,
                "status": state.status,
            },
        })

    from starlette.routing import Route as _Route
    app.router.routes.append(
        _Route("/v1/models/{model_id}", _model_detail, methods=["GET"])
    )

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
        stream_options = body.get("stream_options") or {}
        include_usage = False
        if stream:
            if not isinstance(stream_options, dict):
                return JSONResponse(
                    {"error": {"message": "stream_options must be an object",
                               "type": "invalid_request_error"}},
                    status_code=400,
                )
            include_usage = bool(stream_options.get("include_usage", False))
        elif stream_options:
            # stream_options не имеет смысла без stream
            return JSONResponse(
                {"error": {"message": "stream_options only allowed when stream=true",
                           "type": "invalid_request_error"}},
                status_code=400,
            )
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
        parallel_tool_calls = body.get("parallel_tool_calls")
        if tool_choice == "none":
            tools = []
        # Валидация tools + tool_choice (только если tools не пустые после none)
        err = _validate_tool_request(tools, tool_choice, parallel_tool_calls)
        if err is not None:
            return JSONResponse(err, status_code=400)

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
            tool_calls = _extract_tool_calls(text, tools, tool_choice)

            # tool_choice=required: bounded retry (1 повторная попытка)
            # если модель не вызвала инструмент. Это НЕ гарантия модели, но
            # контракт OpenAI-клиента требует tool_calls при required.
            if tool_choice == "required" and not tool_calls:
                retry_prompt = (
                    prompt + "\n\n[RETRY: обязательно вызови ровно один инструмент "
                    "из списка выше. Текстовый ответ не принимается.]"
                )
                last2 = None
                try:
                    async for turn in session.send_message(retry_prompt, agent_id=agent_id):
                        if getattr(turn, "error", None):
                            break
                        last2 = turn
                except Exception:
                    pass
                if last2 is not None:
                    text = last2.text or ""
                    tool_calls = _extract_tool_calls(text, tools, tool_choice)
                    eff2 = getattr(last2, "effective", None)
                    if eff2 is not None and getattr(eff2, "effective_mode", None):
                        catalog.mark_available(
                            model,
                            effective_mode=eff2.effective_mode,
                            effective_preset=eff2.effective_preset,
                            trial_left=eff2.trial_left,
                            trial_limit=eff2.trial_limit,
                        )
                if not tool_calls:
                    return JSONResponse(
                        {"error": {"message": "model did not produce a tool call "
                                               "despite tool_choice='required' after retry",
                                   "type": "tool_choice_required_no_call"}},
                        status_code=422,
                    )

            if parallel_tool_calls is False and len(tool_calls) > 1:
                return JSONResponse(
                    {"error": {"message": "multiple tool calls returned but "
                                           "parallel_tool_calls=false",
                               "type": "parallel_tool_calls_not_supported"}},
                    status_code=400,
                )

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
                # У Алисы нет token counting. null — честно; фиктивное число
                # хуже, чем отсутствие. OpenAI SDK принимает usage=None.
                "usage": None,
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
                            tool_calls = _extract_tool_calls(full_text, tools, tool_choice)
                            if parallel_tool_calls is False and len(tool_calls) > 1:
                                # OpenAI-контракт: при parallel_tool_calls=false
                                # возвращать >1 вызов нельзя. Поток уже начат — SSE error.
                                yield _sse({
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "error": {
                                        "message": "parallel_tool_calls=false but model "
                                                   "returned multiple tool calls",
                                        "type": "parallel_tool_calls_not_supported",
                                    },
                                })
                                yield "data: [DONE]\n\n"
                                return

                            if tool_choice == "required" and not tool_calls:
                                yield _sse({
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "error": {
                                        "message": "model did not produce a tool call "
                                                   "despite tool_choice='required'",
                                        "type": "tool_choice_required_no_call",
                                    },
                                })
                                yield "data: [DONE]\n\n"
                                return

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
                                if include_usage:
                                    yield _sse({
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [],
                                        "usage": None,
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
                        if include_usage:
                            yield _sse({
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [],
                                "usage": None,
                            })
                        yield "data: [DONE]\n\n"
                        return
            except asyncio.CancelledError:
                await session.cancel_current()
                raise

        return StreamingResponse(streamer(), media_type="text/event-stream")

    async def _responses(request):
        # Responses API — не «Chat Completion под другим ключом».
        # Формат: input/instructions, output items (message | function_call),
        # общий внутренний сбор через _run_alice_collect.
        from fastapi.responses import JSONResponse, StreamingResponse
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "invalid JSON", "type": "invalid_request_error"}},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "request body must be a JSON object",
                           "type": "invalid_request_error"}},
                status_code=400,
            )
        model = body.get("model")
        if not model:
            return JSONResponse(
                {"error": {"message": "model is required",
                           "type": "invalid_request_error"}},
                status_code=400,
            )
        state = catalog.get_state(model)
        if state is None:
            return JSONResponse(
                {"error": {"message": f"model {model!r} not found",
                           "type": "invalid_request_error"}},
                status_code=404,
            )
        if state.status in ("implemented", "unknown"):
            return JSONResponse(
                {"error": {"message": f"model {model!r} has not been verified yet",
                           "type": "model_not_verified"}},
                status_code=503,
            )
        if state.status == "unavailable":
            return JSONResponse(
                {"error": {"message": f"model {model!r} is currently unavailable: {state.reason}",
                           "type": "model_unavailable"}},
                status_code=503,
            )

        # response_format: валидация + prompt-инструкция
        rf_suffix, rf_err = _validate_response_format(body.get("response_format"))
        if rf_err is not None:
            return JSONResponse(rf_err, status_code=400)

        # previous_response_id: реальное хранилище, не игнор
        prev_id = body.get("previous_response_id")
        prev_entry = None
        if prev_id:
            prev_entry = response_store.get(prev_id)
            if prev_entry is None:
                return JSONResponse(
                    {"error": {"message": f"previous_response_id {prev_id!r} not found "
                                          f"(not stored or expired)",
                               "type": "previous_response_not_found"}},
                    status_code=400,
                )

        try:
            user_input = _flatten_responses_input(body.get("input"))
        except ValueError as e:
            return JSONResponse(
                {"error": {"message": str(e), "type": "invalid_request_error"}},
                status_code=400,
            )
        instructions = body.get("instructions") or ""
        if instructions:
            prompt = f"[system] {instructions}\n\n{user_input}"
        else:
            prompt = user_input
        if rf_suffix:
            prompt += rf_suffix

        tools = body.get("tools") or []
        # Responses tools могут быть в двух формах:
        #   {"type": "function", "name": ..., "parameters": ...}
        #   {"type": "function", "function": {"name": ..., "parameters": ...}}
        # Приводим к каноничному виду для _validate_tool_request/_inject_tool_instructions.
        canonical_tools = []
        for i, t in enumerate(tools):
            if not isinstance(t, dict):
                return JSONResponse(
                    {"error": {"message": f"tools[{i}] is not an object",
                               "type": "invalid_request_error"}},
                    status_code=400,
                )
            if t.get("type") != "function":
                return JSONResponse(
                    {"error": {"message": f"tools[{i}].type must be 'function'",
                               "type": "invalid_request_error"}},
                    status_code=400,
                )
            if "function" in t:
                fn = t["function"]
            else:
                fn = {"name": t.get("name"), "description": t.get("description"),
                      "parameters": t.get("parameters")}
            canonical_tools.append({"type": "function", "function": fn})

        tool_choice = body.get("tool_choice") or "auto"
        # Responses принимает tool_choice как строку или {type:"function",name}
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function" \
                and "name" in tool_choice and "function" not in tool_choice:
            tool_choice = {"type": "function",
                           "function": {"name": tool_choice["name"]}}

        if canonical_tools:
            err = _validate_tool_request(canonical_tools, tool_choice,
                                          body.get("parallel_tool_calls"))
            if err is not None:
                return JSONResponse(err, status_code=400)
            prompt = _inject_tool_instructions(prompt, canonical_tools, tool_choice)
        elif tool_choice == "none":
            canonical_tools = []

        # previous_response_id фиксирует dialog_id и session_id предыдущего ответа
        # (по контракту OpenAI Responses continuation).
        if prev_entry is not None:
            forced_session_id = prev_entry.get("session_id")
            prev_dialog_id = prev_entry.get("dialog_id")
        else:
            forced_session_id = None
            prev_dialog_id = None

        session_id = (request.headers.get("X-Alice-Session-ID")
                      or (body.get("metadata") or {}).get("session_id")
                      or forced_session_id)
        try:
            session = await session_registry.acquire(session_id)
        except Exception as e:
            return JSONResponse(
                {"error": {"message": str(e), "type": "server_error"}},
                status_code=500,
            )
        # Если есть prev_dialog_id — использовать его, чтобы продолжить историю
        if prev_dialog_id:
            try:
                session.dialog_id = prev_dialog_id
            except Exception:
                pass

        response_id = _new_response_id()
        created_at = int(time.time())
        agent_id = state.agent_id
        stream = bool(body.get("stream"))
        store_response = body.get("store", True)  # OpenAI default: True

        if not stream:
            try:
                text, tool_calls, _last = await _run_alice_collect(
                    session, prompt, agent_id, canonical_tools,
                    tool_choice, catalog, model,
                )
            except RuntimeError as e:
                return JSONResponse(
                    {"error": {"message": str(e), "type": "upstream_error"}},
                    status_code=502,
                )
            except Exception as e:
                return JSONResponse(
                    {"error": {"message": str(e), "type": "server_error"}},
                    status_code=500,
                )
            output = _format_responses_output(text, tool_calls, canonical_tools)
            if store_response:
                try:
                    did = getattr(session, "dialog_id", None)
                    response_store.put(response_id, did or "", model, session_id)
                except Exception:
                    pass
            return JSONResponse({
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "status": "completed",
                "model": model,
                "output": output,
                "parallel_tool_calls": body.get("parallel_tool_calls", True),
                "tool_choice": tool_choice,
                "tools": canonical_tools,
                "usage": None,
                "error": None,
                "incomplete_details": None,
            })

        # ---------- stream ----------
        async def _event(type_, payload):
            return f"event: {type_}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def streamer():
            yield await _event("response.created", {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created_at,
                    "status": "in_progress",
                    "model": model,
                },
            })
            seq = 0
            try:
                text, tool_calls, _last = await _run_alice_collect(
                    session, prompt, agent_id, canonical_tools,
                    tool_choice, catalog, model,
                )
            except Exception as e:
                yield await _event("response.error", {
                    "type": "response.error",
                    "response_id": response_id,
                    "error": {"type": "upstream_error", "message": str(e)},
                })
                yield await _event("response.completed", {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "failed",
                        "error": {"type": "upstream_error", "message": str(e)},
                    },
                })
                return

            output = _format_responses_output(text, tool_calls, canonical_tools)
            # эмитим items в порядке
            for idx, item in enumerate(output):
                seq += 1
                yield await _event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": idx,
                    "item": item,
                })
                if item["type"] == "function_call":
                    seq += 1
                    yield await _event("response.function_call_arguments.done", {
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": idx,
                        "call_id": item["call_id"],
                        "name": item["name"],
                        "arguments": item["arguments"],
                    })
                elif item["type"] == "message":
                    seq += 1
                    yield await _event("response.content_part.added", {
                        "type": "response.content_part.added",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": idx,
                        "content_index": 0,
                        "part": item["content"][0],
                    })
                    seq += 1
                    yield await _event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": idx,
                        "content_index": 0,
                        "delta": text,
                    })
                    seq += 1
                    yield await _event("response.output_text.done", {
                        "type": "response.output_text.done",
                        "response_id": response_id,
                        "item_id": item["id"],
                        "output_index": idx,
                        "content_index": 0,
                        "text": text,
                    })
                seq += 1
                yield await _event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": idx,
                    "item": item,
                })

            if store_response:
                try:
                    did = getattr(session, "dialog_id", None)
                    response_store.put(response_id, did or "", model, session_id)
                except Exception:
                    pass
            yield await _event("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created_at,
                    "status": "completed",
                    "model": model,
                    "output": output,
                    "usage": None,
                },
            })

        return StreamingResponse(streamer(), media_type="text/event-stream")

    app.router.routes.append(
        Route("/v1/responses", _responses, methods=["POST"])
    )

    app.router.routes.append(
        Route("/v1/chat/completions", _chat_completions, methods=["POST"])
    )
