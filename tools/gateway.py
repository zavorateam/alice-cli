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
from tools.manager import SessionConflictError

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

    async def warm_up(self) -> None:
        """Healthcheck базовых моделей (base + pro), awaited до server.serve().

        Ошибки не валят старт — модель просто останется implemented.
        Expert не трогаем — у него квота ~5/24ч, healthcheck только по запросу.
        """
        for mid in ("alice-base", "alice-pro"):
            try:
                await self.healthcheck(mid)
            except Exception:
                pass

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


_JSONSCHEMA_AVAILABLE = None


def _jsonschema_available() -> bool:
    global _JSONSCHEMA_AVAILABLE
    if _JSONSCHEMA_AVAILABLE is None:
        try:
            import jsonschema  # noqa
            _JSONSCHEMA_AVAILABLE = True
        except ImportError:
            _JSONSCHEMA_AVAILABLE = False
    return _JSONSCHEMA_AVAILABLE


def _get_tool_schema(tools: List[dict], name: str) -> Optional[dict]:
    for t in tools:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return fn.get("parameters") or {}
    return None


def _validate_tool_args(args_json: str, schema: dict) -> tuple:
    """Валидирует JSON args против JSON Schema.

    Возвращает (parsed_or_None, error_str_or_None).
    Пустая schema == {} → пропускаем валидацию.
    """
    try:
        parsed = json.loads(args_json)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg} at pos {e.pos}"
    if not schema or not _jsonschema_available():
        return parsed, None
    try:
        import jsonschema as _js
        _js.validate(instance=parsed, schema=schema)
    except Exception as e:
        # ValidationError или SchemaError
        import jsonschema as _js
        if isinstance(e, _js.ValidationError):
            path = "/".join(str(p) for p in e.absolute_path) or "(root)"
            return None, f"schema violation at {path}: {e.message}"
        if isinstance(e, _js.SchemaError):
            return None, f"invalid JSON Schema: {e.message}"
        return None, f"validation error: {e}"
    return parsed, None


def _validate_tool_calls_args(tool_calls: List[dict],
                              tools: List[dict]) -> tuple:
    """Возвращает (valid_calls, [(call, error_msg), ...])."""
    valid, invalid = [], []
    for tc in tool_calls:
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]
        schema = _get_tool_schema(tools, name) or {}
        _parsed, err = _validate_tool_args(args_json, schema)
        if err is not None:
            invalid.append((tc, err))
        else:
            valid.append(tc)
    return valid, invalid


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
        # Schema well-formedness (если доступен jsonschema)
        params = fn.get("parameters")
        if params is not None:
            if not isinstance(params, dict):
                return {"error": {"message": f"tools[{i}].function.parameters must be an object",
                                  "type": "invalid_request_error"}}
            if _jsonschema_available():
                try:
                    import jsonschema as _js
                    _js.Draft202012Validator.check_schema(params)
                except Exception as e:
                    return {"error": {"message": f"tools[{i}].function.parameters is not a valid "
                                                  f"JSON Schema: {e}",
                                      "type": "invalid_request_error"}}
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


async def _run_alice_stream(session, prompt, agent_id, tools, tool_choice,
                              catalog, model):
    """Генератор: отдаёт события по мере поступления turn-ов от Alice.

    Yield-ит dict-события:
      {"kind": "text_delta", "text": "..."}      — новый кусок текста
      {"kind": "finished", "full_text": "...",
       "tool_calls": [...], "last_turn": turn}   — финал (успешный)

    Бросает RuntimeError при upstream error. Никакого буфера «собрать всё».
    """
    sent_len = 0
    tool_calls = []
    last = None
    async for turn in session.send_message(prompt, agent_id=agent_id):
        if getattr(turn, "error", None):
            raise RuntimeError(turn.error)
        last = turn
        full = turn.text or ""
        if len(full) > sent_len:
            new_text = full[sent_len:]
            sent_len = len(full)
            yield {"kind": "text_delta", "text": new_text}
        if getattr(turn, "is_finished", False):
            full_text = turn.text or ""
            if tools:
                tool_calls = _extract_tool_calls(full_text, tools, tool_choice)
            eff = getattr(turn, "effective", None)
            if eff is not None and getattr(eff, "effective_mode", None):
                catalog.mark_available(
                    model,
                    effective_mode=eff.effective_mode,
                    effective_preset=eff.effective_preset,
                    trial_left=eff.trial_left,
                    trial_limit=eff.trial_limit,
                )
            yield {"kind": "finished", "full_text": full_text,
                   "tool_calls": tool_calls, "last_turn": turn}
            return


class _ToolMarkerFilter:
    """Stateful filter: strips <tool_call>...</tool_call> из потока.

    feed(text) → безопасный к эмиту текст (может быть "").
    flush()    → остаток на финале ("" если открытый маркер не закрыт).

    Инвариант:
        feed(t1) + feed(t2) + ... + flush() == _clean_tool_markers(t1 + t2 + ...)
    """

    TAG_OPEN = "<tool_call>"
    TAG_CLOSE = "</tool_call>"

    def __init__(self):
        self._in_marker = False
        self._buffer = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buffer += text
        out_parts = []
        while self._buffer:
            if self._in_marker:
                idx = self._buffer.find(self.TAG_CLOSE)
                if idx < 0:
                    # держим tail в len(TAG_CLOSE)-1 символ — тег может быть разбит
                    keep = len(self.TAG_CLOSE) - 1
                    if len(self._buffer) > keep:
                        self._buffer = self._buffer[-keep:] if keep else ""
                    break
                self._buffer = self._buffer[idx + len(self.TAG_CLOSE):]
                self._in_marker = False
                continue
            idx = self._buffer.find(self.TAG_OPEN)
            if idx < 0:
                keep = 0
                max_k = min(len(self._buffer), len(self.TAG_OPEN) - 1)
                for k in range(max_k, 0, -1):
                    if self._buffer.endswith(self.TAG_OPEN[:k]):
                        keep = k
                        break
                if keep:
                    emit = self._buffer[:len(self._buffer) - keep]
                    self._buffer = self._buffer[len(self._buffer) - keep:]
                else:
                    emit = self._buffer
                    self._buffer = ""
                if emit:
                    out_parts.append(emit)
                break
            emit = self._buffer[:idx]
            if emit:
                out_parts.append(emit)
            self._buffer = self._buffer[idx + len(self.TAG_OPEN):]
            self._in_marker = True
        return "".join(out_parts)

    def flush(self) -> str:
        if self._in_marker:
            self._buffer = ""
            return ""
        out = self._buffer
        self._buffer = ""
        return out


def _clean_tool_markers(text: str) -> str:
    """Удаляет <tool_call>...</tool_call> блоки из полного текста.

    Использует тот же stateful filter, что и streaming — результаты совпадают.
    Не .strip() — для консистентности с потоковой выдачей (deltas == done.text).
    """
    f = _ToolMarkerFilter()
    return f.feed(text or "") + f.flush()


def _parse_responses_text_format(body: dict) -> tuple:
    """Извлекает Responses-format из body и приводит к rf-структуре.

    Responses API: {"text": {"format": {...}}}
    Chat-style:    {"response_format": {...}}

    Возвращает (rf, err). rf совместим с _validate_response_format.
    """
    text_field = body.get("text")
    if text_field is not None and not isinstance(text_field, dict):
        return None, {"error": {"message": "text must be an object",
                                "type": "invalid_request_error"}}
    fmt = None
    if isinstance(text_field, dict):
        fmt = text_field.get("format")
    if fmt is None:
        # fallback на Chat-style response_format (для совместимости)
        rf = body.get("response_format")
        if rf is not None:
            return rf, None
        return None, None
    if not isinstance(fmt, dict):
        return None, {"error": {"message": "text.format must be an object",
                                "type": "invalid_request_error"}}
    ftype = fmt.get("type")
    if ftype in (None, "text"):
        return {"type": "text"}, None
    if ftype == "json_object":
        return {"type": "json_object"}, None
    if ftype == "json_schema":
        # Responses: {type: "json_schema", name, schema, strict}
        # Внутренний _validate_response_format ожидает {type, json_schema: {...}}
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            return None, {"error": {"message": "text.format.schema is required "
                                              "for json_schema type",
                                    "type": "invalid_request_error"}}
        return {"type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name") or "response",
                    "schema": schema,
                    "strict": bool(fmt.get("strict")),
                }}, None
    return None, {"error": {"message": f"text.format.type={ftype!r} is not supported "
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

    Порядок: message (если есть текст) → function_call items.
    Этот порядок совпадает с streaming-путём. Пустой текст не даёт message.
    """
    out = []
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
    return out


def _check_api_key(request):
    """Возвращает JSONResponse(401) или None.

    Проверка включается, если задан ALICE_API_KEY (env). Без ключа — пропускает.
    Сравнение через hmac.compare_digest.
    """
    from fastapi.responses import JSONResponse
    import os as _os
    import hmac as _hmac
    expected = (_os.environ.get("ALICE_API_KEY") or "").strip()
    if not expected:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return JSONResponse(
            {"error": {"message": "missing or invalid Authorization header "
                                   "(expected 'Bearer <api_key>')",
                       "type": "invalid_request_error"}},
            status_code=401,
        )
    provided = header[len("Bearer "):].strip()
    if not provided or not _hmac.compare_digest(provided.encode("utf-8"),
                                                 expected.encode("utf-8")):
        return JSONResponse(
            {"error": {"message": "invalid API key",
                       "type": "invalid_request_error"}},
            status_code=401,
        )
    return None


def attach_openai_routes(app, catalog: ModelCatalog, session_registry) -> None:
    """Регистрирует /v1/* на FastAPI-приложение.

    POST /v1/chat/completions регистрируется как сырой Starlette Route:
    FastAPI 0.141 + Starlette 1.x ошибочно трактует `request: Request`
    как body-параметр и отвечает 422 ещё до хендлера. Через starlette.routing.Route
    вызова идёт напрямую, минуя FastAPI param analysis.
    """
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    @app.middleware("http")
    async def _v1_auth_middleware(request, call_next):
        if request.url.path.startswith("/v1/"):
            err = _check_api_key(request)
            if err is not None:
                return err
        return await call_next(request)

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
            except SessionConflictError as e:
                return JSONResponse(
                    {"error": {"message": str(e), "type": "session_conflict"}},
                    status_code=409,
                )
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

            _forced = _forced_function_name(tool_choice)
            if _forced:
                wrong = [c["function"]["name"] for c in tool_calls
                         if c["function"]["name"] != _forced]
                if not tool_calls or wrong:
                    return JSONResponse(
                        {"error": {"message": f"tool_choice required function "
                                              f"{_forced!r} but model produced "
                                              f"{'no call' if not tool_calls else wrong}",
                                   "type": "tool_choice_violated"}},
                        status_code=422,
                    )

            # chat non-stream: args validation + retry
            if tool_calls and tools:
                valid, invalid = _validate_tool_calls_args(tool_calls, tools)
                if invalid:
                    err_msgs = "; ".join(
                        f"{c['function']['name']}: {e}" for c, e in invalid
                    )
                    retry_prompt = (
                        prompt
                        + f"\n\n[RETRY: invalid tool args: {err_msgs}. "
                          f"Re-emit the tool_call with arguments matching the schema.]"
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
                        valid, invalid = _validate_tool_calls_args(tool_calls, tools)
                        if invalid:
                            return JSONResponse(
                                {"error": {"message": f"tool args invalid after retry: {err_msgs}",
                                           "type": "tool_arguments_invalid"}},
                                status_code=422,
                            )
                        tool_calls = valid
                    else:
                        return JSONResponse(
                            {"error": {"message": f"tool args invalid: {err_msgs}",
                                       "type": "tool_arguments_invalid"}},
                            status_code=422,
                        )
                else:
                    tool_calls = valid

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
                _agen = session.send_message(prompt, agent_id=agent_id)
                try:
                    _first = await _agen.__anext__()
                except StopAsyncIteration:
                    return
                except SessionConflictError as _e:
                    yield _sse({
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "error": {"message": str(_e), "type": "session_conflict"},
                    })
                    yield "data: [DONE]\n\n"
                    return

                async def _chain(first, agen):
                    yield first
                    async for _t in agen:
                        yield _t

                async for turn in _chain(_first, _agen):
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

                            # chat stream: args validation (без retry — SSE уже стартовал)
                            if tool_calls and tools:
                                _valid, _invalid = _validate_tool_calls_args(tool_calls, tools)
                                if _invalid:
                                    _err = "; ".join(f"{c['function']['name']}: {e}"
                                                     for c, e in _invalid)
                                    yield _sse({
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "error": {
                                            "message": f"tool args invalid: {_err}",
                                            "type": "tool_arguments_invalid",
                                        },
                                    })
                                    yield "data: [DONE]\n\n"
                                    return
                                tool_calls = _valid

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

                            _forced = _forced_function_name(tool_choice)
                            if _forced:
                                wrong = [c["function"]["name"] for c in tool_calls
                                         if c["function"]["name"] != _forced]
                                if not tool_calls or wrong:
                                    yield _sse({
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "error": {
                                            "message": f"tool_choice required function "
                                                       f"{_forced!r} but model produced "
                                                       f"{'no call' if not tool_calls else wrong}",
                                            "type": "tool_choice_violated",
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

        # text.format (Responses) или response_format (Chat-style fallback).
        rf_raw, rf_parse_err = _parse_responses_text_format(body)
        if rf_parse_err is not None:
            return JSONResponse(rf_parse_err, status_code=400)
        rf_suffix, rf_err = _validate_response_format(rf_raw)
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
            prev_session_id = prev_entry.get("session_id")
            prev_dialog_id = prev_entry.get("dialog_id")
        else:
            prev_session_id = None
            prev_dialog_id = None

        explicit_sid = (request.headers.get("X-Alice-Session-ID")
                        or (body.get("metadata") or {}).get("session_id"))
        # Owner check: если у предыдущего response есть session_id, и текущий
        # запрос передаёт ДРУГОЙ session_id — отказ (не даём клиенту B
        # продолжить response клиента A, зная только response_id).
        if prev_session_id and explicit_sid and explicit_sid != prev_session_id:
            return JSONResponse(
                {"error": {"message": "previous_response_id belongs to a "
                                      "different session; not allowed",
                           "type": "previous_response_not_owned"}},
                status_code=403,
            )
        # Если previous_response_id передан и client_sid не указан —
        # восстанавливаем из prev_entry. Это легально: клиент хранит
        # response_id как capability.
        session_id = explicit_sid or prev_session_id
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
            except SessionConflictError as e:
                return JSONResponse(
                    {"error": {"message": str(e), "type": "session_conflict"}},
                    status_code=409,
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
            _forced = _forced_function_name(tool_choice)
            if _forced:
                wrong = [c["function"]["name"] for c in tool_calls
                         if c["function"]["name"] != _forced]
                if not tool_calls or wrong:
                    return JSONResponse(
                        {"error": {"message": f"tool_choice required function "
                                              f"{_forced!r} but model produced "
                                              f"{'no call' if not tool_calls else wrong}",
                                   "type": "tool_choice_violated"}},
                        status_code=422,
                    )
            # responses non-stream: args validation + retry
            if tool_calls and canonical_tools:
                valid, invalid = _validate_tool_calls_args(tool_calls, canonical_tools)
                if invalid:
                    err_msgs = "; ".join(
                        f"{c['function']['name']}: {e}" for c, e in invalid
                    )
                    retry_prompt = (
                        prompt
                        + f"\n\n[RETRY: invalid tool args: {err_msgs}. "
                          f"Re-emit with valid args matching the schema.]"
                    )
                    last2 = None
                    try:
                        async for t2 in session.send_message(retry_prompt, agent_id=agent_id):
                            if getattr(t2, "error", None):
                                break
                            last2 = t2
                    except Exception:
                        pass
                    if last2 is not None:
                        text = last2.text or ""
                        tool_calls = _extract_tool_calls(text, canonical_tools, tool_choice)
                        valid, invalid = _validate_tool_calls_args(
                            tool_calls, canonical_tools)
                        if invalid:
                            return JSONResponse(
                                {"error": {"message": f"tool args invalid after retry: {err_msgs}",
                                           "type": "tool_arguments_invalid"}},
                                status_code=422,
                            )
                        tool_calls = valid
                    else:
                        return JSONResponse(
                            {"error": {"message": f"tool args invalid: {err_msgs}",
                                       "type": "tool_arguments_invalid"}},
                            status_code=422,
                        )
                else:
                    tool_calls = valid

            clean_text = _clean_tool_markers(text)
            output = _format_responses_output(clean_text, tool_calls, canonical_tools)
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
            # Real-time streaming + stateful filter.
            # <tool_call>...</tool_call> не попадает в output_text.delta.
            # Инвариант: "".join(deltas) == output_text.done.text.
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
            msg_item_id = f"msg_{uuid.uuid4().hex[:20]}"
            msg_started = False
            output_items = []
            marker_filter = _ToolMarkerFilter()
            emitted_parts = []

            try:
                async for ev in _run_alice_stream(
                    session, prompt, agent_id, canonical_tools,
                    tool_choice, catalog, model,
                ):
                    if ev["kind"] == "text_delta":
                        filtered = marker_filter.feed(ev["text"])
                        if not filtered:
                            continue
                        if not msg_started:
                            msg_started = True
                            yield await _event("response.output_item.added", {
                                "type": "response.output_item.added",
                                "response_id": response_id,
                                "output_index": 0,
                                "item": {
                                    "type": "message",
                                    "id": msg_item_id,
                                    "role": "assistant",
                                    "status": "in_progress",
                                    "content": [],
                                },
                            })
                            yield await _event("response.content_part.added", {
                                "type": "response.content_part.added",
                                "response_id": response_id,
                                "item_id": msg_item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "part": {"type": "output_text",
                                         "text": "", "annotations": []},
                            })
                        emitted_parts.append(filtered)
                        yield await _event("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "item_id": msg_item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": filtered,
                        })
                    elif ev["kind"] == "finished":
                        tail = marker_filter.flush()
                        if tail:
                            if not msg_started:
                                msg_started = True
                                yield await _event("response.output_item.added", {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": 0,
                                    "item": {
                                        "type": "message",
                                        "id": msg_item_id,
                                        "role": "assistant",
                                        "status": "in_progress",
                                        "content": [],
                                    },
                                })
                                yield await _event("response.content_part.added", {
                                    "type": "response.content_part.added",
                                    "response_id": response_id,
                                    "item_id": msg_item_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "output_text",
                                             "text": "", "annotations": []},
                                })
                            emitted_parts.append(tail)
                            yield await _event("response.output_text.delta", {
                                "type": "response.output_text.delta",
                                "response_id": response_id,
                                "item_id": msg_item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": tail,
                            })
                        tool_calls = ev["tool_calls"]

                        # Schema validation (не потерять — было в предыдущей версии)
                        if tool_calls and canonical_tools:
                            _valid, _invalid = _validate_tool_calls_args(
                                tool_calls, canonical_tools)
                            if _invalid:
                                _err = "; ".join(
                                    f"{c['function']['name']}: {e}"
                                    for c, e in _invalid)
                                yield await _event("response.failed", {
                                    "type": "response.failed",
                                    "response_id": response_id,
                                    "error": {"type": "tool_arguments_invalid",
                                              "message": _err},
                                })
                                return
                            tool_calls = _valid

                        cleaned = "".join(emitted_parts)
                        if msg_started:
                            yield await _event("response.output_text.done", {
                                "type": "response.output_text.done",
                                "response_id": response_id,
                                "item_id": msg_item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "text": cleaned,
                            })
                            msg_item_final = {
                                "type": "message",
                                "id": msg_item_id,
                                "role": "assistant",
                                "status": "completed",
                                "content": [{
                                    "type": "output_text",
                                    "text": cleaned,
                                    "annotations": [],
                                }],
                            }
                            yield await _event("response.output_item.done", {
                                "type": "response.output_item.done",
                                "response_id": response_id,
                                "output_index": 0,
                                "item": msg_item_final,
                            })
                            output_items.append(msg_item_final)
                        fc_idx = len(output_items)
                        for tc in tool_calls:
                            fn = tc.get("function") or {}
                            fc_item = {
                                "type": "function_call",
                                "id": f"fc_{uuid.uuid4().hex[:20]}",
                                "call_id": tc["id"],
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "{}"),
                                "status": "completed",
                            }
                            yield await _event("response.output_item.added", {
                                "type": "response.output_item.added",
                                "response_id": response_id,
                                "output_index": fc_idx,
                                "item": fc_item,
                            })
                            yield await _event("response.function_call_arguments.done", {
                                "type": "response.function_call_arguments.done",
                                "response_id": response_id,
                                "item_id": fc_item["id"],
                                "output_index": fc_idx,
                                "call_id": fc_item["call_id"],
                                "name": fc_item["name"],
                                "arguments": fc_item["arguments"],
                            })
                            yield await _event("response.output_item.done", {
                                "type": "response.output_item.done",
                                "response_id": response_id,
                                "output_index": fc_idx,
                                "item": fc_item,
                            })
                            output_items.append(fc_item)
                            fc_idx += 1
                        if store_response:
                            try:
                                did = getattr(session, "dialog_id", None)
                                response_store.put(response_id, did or "",
                                                   model, session_id)
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
                                "output": output_items,
                                "usage": None,
                            },
                        })
                        return
            except SessionConflictError as e:
                yield await _event("response.failed", {
                    "type": "response.failed",
                    "response_id": response_id,
                    "error": {"type": "session_conflict", "message": str(e)},
                })
                return
            except Exception as e:
                yield await _event("response.failed", {
                    "type": "response.failed",
                    "response_id": response_id,
                    "error": {"type": "upstream_error", "message": str(e)},
                })
                return

        return StreamingResponse(streamer(), media_type="text/event-stream")

    app.router.routes.append(
        Route("/v1/responses", _responses, methods=["POST"])
    )

    app.router.routes.append(
        Route("/v1/chat/completions", _chat_completions, methods=["POST"])
    )
