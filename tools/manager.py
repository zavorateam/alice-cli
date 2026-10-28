"""Управление агентами, диалогами, MCP-сервером и сессиями.

Содержит:
  - GenericAppAgent      — динамическое Alice-приложение (из RPC get_alice_apps)
  - AliceAgentManager    — диалоги, отправка сообщений, ephemeral cleanup
  - ChatSession          — изолированная сессия для OpenAI-совместимого API
  - SessionRegistry      — реестр ChatSession по X-Alice-Session-ID
  - AliceMcpServer       — MCP stdio JSON-RPC сервер

Физически импортирует AliceWsEngine и AliceRpcClient из tools.transport.
Не импортирует alice_server — это исключает цикл.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid as _uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import aiohttp

from agents import (
    AliceAgent, AliceTurnResponse, SuggestionAction, AgentWidget,
    ThinkingStep, ensure_agents_registered, registry,
)
from agents.expert import ExpertAgent
from alice_auth import (
    AliceCredentials, resolve_auth_source, verify_credentials,
)
from tools.constants import uuid7, RPC_BASE_URL
from tools.transport import AliceWsEngine, AliceRpcClient

logger = logging.getLogger("alice_manager")


__all__ = [
    "GenericAppAgent",
    "AliceAgentManager",
    "ChatSession",
    "SessionRegistry",
    "AliceMcpServer",
]


class GenericAppAgent(AliceAgent):
    def __init__(self, app_id: str, name: str, aliases: Optional[List[str]] = None, description: str = ""):
        self.id = app_id
        self.name = name
        self.aliases = aliases or []
        self.type = "app"
        self.mode = "External"
        self.preset = "dialogovo_alice_apps_sticky"
        self.app_id = app_id
        self.description = description
        self.timeout = 300.0


class AliceAgentManager:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False, show_thinking: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.rpc = AliceRpcClient(self.creds, verbose=self.verbose)
        self.ws = AliceWsEngine(self.creds, verbose=self.verbose, show_thinking=show_thinking)

        self.active_dialog_id: Optional[str] = None
        self.active_agent_id: str = "pro"
        self.dialog_prev_req_ids: Dict[str, Optional[str]] = {}
        self.known_dialogs: Set[str] = set()
        self.ephemeral_dialogs: Set[str] = set()

    async def initialize(self, initial_dialog_id: Optional[str] = None):
        try:
            ensure_agents_registered()
        except Exception as e:
            logger.warning(f"ensure_agents_registered: {e}")

        if not registry._agents.get("expert"):
            try:
                registry.register(ExpertAgent())
                logger.info("Registered ExpertAgent (preset=agents_RnServiceAliceExpert)")
            except Exception as e:
                logger.warning(f"ExpertAgent registration: {e}")

        status, apps, _ = await self.rpc.get_alice_apps()
        if status == 200:
            for app in apps:
                app_id_val = app.get("app_id")
                app_name_val = app.get("name", "")
                if not app_id_val:
                    continue
                if registry._agents.get(app_id_val):
                    continue
                registry.register(GenericAppAgent(
                    app_id=app_id_val,
                    name=app_name_val or "Приложение",
                    aliases=[app_name_val.lower()] if app_name_val else [],
                    description=app.get("description", ""),
                ))

        experts = [a for a in registry.list_all() if a.id == "expert"]
        for e in experts:
            logger.info(f"agent 'expert' -> {e.__class__.__name__} preset={e.preset!r} mode={e.mode!r}")

        await self.ws.connect()
        _, recent_dialogs, _ = await self.rpc.list_dialogs(limit=5)
        for d in recent_dialogs:
            did = d.get("dialog", {}).get("dialog_id")
            if did:
                self.known_dialogs.add(did)
                if not self.active_dialog_id:
                    self.active_dialog_id = did

        if initial_dialog_id:
            self.active_dialog_id = initial_dialog_id
            self.known_dialogs.add(initial_dialog_id)
            try:
                async with aiohttp.ClientSession(headers=self.creds.build_rpc_headers()) as session:
                    async with session.post(
                        f"{RPC_BASE_URL}/dialog/read_dialog",
                        json={"dialog_id": initial_dialog_id, "ts_max_ms": 9007199254740991, "limit": 20}
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            msgs = data.get("messages", [])
                            if msgs:
                                self.dialog_prev_req_ids[initial_dialog_id] = msgs[-1].get("request_id")
                                logger.info(f"Подтянут prev_req_id для {initial_dialog_id}: {msgs[-1].get('request_id')}")
            except Exception as e:
                logger.warning(f"Не удалось получить prev_req_id: {e}")

        if not self.active_dialog_id:
            self.active_dialog_id = uuid7()
            self.known_dialogs.add(self.active_dialog_id)

        await self.ws.update_subscriptions(self.active_dialog_id, self.known_dialogs)

    async def create_new_dialog(self, agent_id: Optional[str] = None, is_ephemeral: bool = False) -> str:
        agent = registry.get(agent_id or self.active_agent_id)
        new_id = uuid7()
        self.known_dialogs.add(new_id)
        self.active_dialog_id = new_id
        self.active_agent_id = agent.id
        self.dialog_prev_req_ids[new_id] = None
        if is_ephemeral:
            self.ephemeral_dialogs.add(new_id)

        await self.ws.update_subscriptions(new_id, self.known_dialogs)
        await asyncio.sleep(1.0)
        return new_id

    async def cleanup_ephemeral_dialogs(self):
        if not self.ephemeral_dialogs:
            return
        for did in list(self.ephemeral_dialogs):
            await self.rpc.delete_dialog(did)
            self.known_dialogs.discard(did)
            self.ephemeral_dialogs.discard(did)

    async def send_message(
        self, text: str, agent_id: Optional[str] = None, dialog_id: Optional[str] = None,
        is_suggest: bool = False, attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        agent = registry.get(agent_id or self.active_agent_id)
        target_dialog_id = dialog_id or self.active_dialog_id or await self.create_new_dialog(agent.id)

        self.active_dialog_id = target_dialog_id
        self.active_agent_id = agent.id
        self.known_dialogs.add(target_dialog_id)

        prev_req_id = self.dialog_prev_req_ids.get(target_dialog_id)
        last_turn: Optional[AliceTurnResponse] = None

        async for turn in self.ws.send_text_input(
            text=text, dialog_id=target_dialog_id, agent=agent,
            prev_req_id=prev_req_id, is_suggest=is_suggest, attachments=attachments,
        ):
            turn.dialog_id = target_dialog_id
            last_turn = turn
            yield turn

        if last_turn and not last_turn.error:
            self.dialog_prev_req_ids[target_dialog_id] = last_turn.request_id

    async def shutdown(self):
        await self.cleanup_ephemeral_dialogs()
        await self.ws.disconnect()

    async def _on_dialog_closed(self, dialog_id: str) -> None:
        """Согласованная очистка структур manager при закрытии диалога.

        Вызывается из ChatSession.close() и AliceAgentManager.cleanup_ephemeral_dialogs().
        Не трогает активные запросы: если dialog_id в _pending (по ref_id), вызов
        блокируется до завершения; для eviction in-flight защищён на уровне реестра.
        """
        if not dialog_id:
            return
        self.known_dialogs.discard(dialog_id)
        self.ephemeral_dialogs.discard(dialog_id)
        self.dialog_prev_req_ids.pop(dialog_id, None)
        # Транспорт: убрать из подписок, если диалог больше не активен
        try:
            ws = getattr(self, "ws", None)
            if ws is not None and hasattr(ws, "update_subscriptions"):
                await ws.update_subscriptions(
                    self.active_dialog_id or dialog_id,
                    self.known_dialogs,
                )
        except Exception:
            pass


class SessionConflictError(RuntimeError):
    """Один ChatSession — не более одного активного запроса за раз.

    Backend Алисы работает с одним dialog_id; параллельные ходы в нём
    смешивают историю, ломают отмену и не гарантируют изоляцию.
    Gateway возвращает 409 session_conflict.
    """


class ChatSession:
    """Изолированная сессия для одного HTTP-клиента OpenAI-совместимого API."""

    def __init__(self, session_id: str, manager: AliceAgentManager):
        self.session_id = session_id
        self.manager = manager
        self.dialog_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._current_req_id: Optional[str] = None
        self._active = 0

    async def ensure_dialog(self, agent_id: str = "pro") -> str:
        async with self._lock:
            if not self.dialog_id:
                self.dialog_id = await self.manager.create_new_dialog(
                    agent_id=agent_id, is_ephemeral=True,
                )
            return self.dialog_id

    async def send_message(self, text: str, agent_id: str = "pro",
                           is_suggest: bool = False):
        # Один активный запрос на сессию. Второй — SessionConflictError.
        # Проверка и захват _active атомарны относительно event loop
        # (нет await между проверкой и установкой).
        if self._active > 0:
            raise SessionConflictError(
                f"session {self.session_id} already has an active request"
            )
        self._active = 1
        try:
            did = await self.ensure_dialog(agent_id)
            async for turn in self.manager.send_message(
                text=text, agent_id=agent_id, dialog_id=did, is_suggest=is_suggest,
            ):
                if not self._current_req_id and turn.request_id:
                    self._current_req_id = turn.request_id
                yield turn
        finally:
            self._active = 0
            self._current_req_id = None

    async def cancel_current(self) -> None:
        req_id = self._current_req_id
        if not req_id:
            return
        ws = getattr(self.manager, "ws", None)
        if ws is None:
            return
        queue = ws._pending_responses.get(req_id)
        if queue is None:
            return
        try:
            queue.put_nowait(AliceTurnResponse(
                dialog_id=self.dialog_id or "",
                request_id=req_id,
                text="",
                is_finished=True,
                error="cancelled by client",
            ))
        except Exception:
            pass

    async def close(self, delete_dialog: bool = True):
        # Если есть активный запрос — сначала попросить его завершиться
        # (cancel_current кладёт error turn в очередь), затем дождаться,
        # пока generator дойдёт до finally и обнулит _active.
        if self._active > 0:
            await self.cancel_current()
            for _ in range(50):  # до 5 секунд
                if self._active == 0:
                    break
                await asyncio.sleep(0.1)
        did = self.dialog_id
        if delete_dialog and did:
            try:
                await self.manager.rpc.delete_dialog(did)
            except Exception:
                pass
        try:
            on_close = getattr(self.manager, "_on_dialog_closed", None)
            if on_close is not None and did:
                await on_close(did)
        except Exception:
            pass
        self.dialog_id = None





class SessionRegistry:
    """Реестр ChatSession с TTL, MAX, backpressure.

    - TTL: сессии с истёкшим TTL удаляются при acquire.
    - MAX: при переполнении удаляются неактивные LRU; если все активны —
      acquire() бросает RuntimeError (backpressure).
    - Активные сессии (chat._active > 0) НЕ вытесняются ни по TTL, ни по MAX.
    """

    DEFAULT_TTL = 3600.0
    DEFAULT_MAX_SESSIONS = 128

    def __init__(self, manager_factory, ttl_seconds: Optional[float] = None,
                 max_sessions: int = DEFAULT_MAX_SESSIONS):
        self._factory = manager_factory
        self._sessions: Dict[str, ChatSession] = {}
        self._last_used: Dict[str, float] = {}
        self._owns_managers = False
        self._lock = asyncio.Lock()
        self._ttl = self.DEFAULT_TTL if ttl_seconds is None else ttl_seconds
        self._max = max_sessions

    async def _close_one_locked(self, sid: str, allow_active: bool = False) -> bool:
        chat = self._sessions.get(sid)
        if chat is None:
            return True
        if getattr(chat, "_active", 0) > 0 and not allow_active:
            return False
        self._sessions.pop(sid, None)
        self._last_used.pop(sid, None)
        try:
            await chat.close(delete_dialog=True)
        except Exception:
            pass
        return True

    async def _evict_ttl_locked(self):
        import time as _time
        if self._ttl <= 0:
            return
        now = _time.time()
        expired = [sid for sid, ts in self._last_used.items()
                   if now - ts > self._ttl]
        for sid in expired:
            await self._close_one_locked(sid, allow_active=False)

    async def _make_room_locked(self, incoming: int = 1) -> bool:
        if self._max <= 0:
            return True
        overflow = len(self._sessions) + incoming - self._max
        if overflow <= 0:
            return True
        ordered = sorted(self._last_used.items(), key=lambda kv: kv[1])
        freed = 0
        for sid, _ts in ordered:
            if freed >= overflow:
                break
            if await self._close_one_locked(sid, allow_active=False):
                freed += 1
        return freed >= overflow

    async def acquire(self, session_id: Optional[str]) -> ChatSession:
        import time as _time
        sid = session_id or _uuid.uuid4().hex
        async with self._lock:
            await self._evict_ttl_locked()
            if sid in self._sessions:
                self._last_used[sid] = _time.time()
                return self._sessions[sid]
            if not await self._make_room_locked(incoming=1):
                raise RuntimeError(
                    f"SessionRegistry at capacity ({self._max}); "
                    f"all sessions are active, retry later"
                )
            mgr = await self._factory()
            self._sessions[sid] = ChatSession(sid, mgr)
            self._last_used[sid] = _time.time()
            return self._sessions[sid]

    async def close(self):
        async with self._lock:
            for s in self._sessions.values():
                await s.close(delete_dialog=False)
            self._sessions.clear()
            self._last_used.clear()

    def __len__(self) -> int:
        return len(self._sessions)






class AliceMcpServer:
    """MCP stdio JSON-RPC сервер + кэш per-auth AliceAgentManager.

    `_managers_cache` ограничен: TTL + MAX_MANAGERS. Eviction выполняется
    при `_get_manager_for_call()` (lazy). Вытесненный менеджер закрывается
    через `shutdown()` — освобождаются WS-соединение и ephemeral-диалоги.

    TTL по умолчанию 2 часа, лимит 32 менеджера.
    """

    DEFAULT_TTL = 7200.0
    DEFAULT_MAX_MANAGERS = 32

    def __init__(self, manager: Optional[AliceAgentManager] = None,
                 ttl_seconds: Optional[float] = None,
                 max_managers: int = DEFAULT_MAX_MANAGERS):
        ensure_agents_registered()
        self.manager = manager
        self._managers_cache: Dict[str, AliceAgentManager] = {}
        self._last_used: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._ttl = self.DEFAULT_TTL if ttl_seconds is None else ttl_seconds
        self._max = max_managers

    async def _close_one_locked(self, auth: str):
        mgr = self._managers_cache.pop(auth, None)
        self._last_used.pop(auth, None)
        if mgr is not None:
            try:
                await mgr.shutdown()
            except Exception:
                pass

    async def _evict_ttl_locked(self):
        import time as _time
        if self._ttl <= 0:
            return
        now = _time.time()
        expired = [a for a, ts in self._last_used.items() if now - ts > self._ttl]
        for a in expired:
            await self._close_one_locked(a)

    async def _make_room_locked(self, incoming: int = 1):
        if self._max <= 0:
            return
        overflow = len(self._managers_cache) + incoming - self._max
        if overflow <= 0:
            return
        ordered = sorted(self._last_used.items(), key=lambda kv: kv[1])
        for a, _ts in ordered[:overflow]:
            await self._close_one_locked(a)

    async def _shutdown_all(self):
        async with self._lock:
            for a in list(self._managers_cache.keys()):
                await self._close_one_locked(a)

    def _get_tools_definition(self) -> List[Dict[str, Any]]:
        tools = []
        for a in registry.list_all():
            tools.append({
                "name": f"alice_ask_{a.id}",
                "description": f"Сервис '{a.name}'. {a.description}",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "dialog_id": {"type": "string"},
                        "auth": {"type": "string"},
                    },
                    "required": ["message"],
                },
            })
        tools.append({
            "name": "alice_click_action",
            "description": "Выбрать вариант ответа, кнопку, адрес или тариф",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_text": {"type": "string"},
                    "dialog_id": {"type": "string"},
                    "auth": {"type": "string"},
                },
                "required": ["action_text"],
            },
        })
        return tools


    async def _get_manager_for_call(self, auth_param: Optional[str]) -> Tuple[Optional[AliceAgentManager], Optional[str]]:
        import time as _time
        if auth_param:
            async with self._lock:
                await self._evict_ttl_locked()
                cached = self._managers_cache.get(auth_param)
                if cached is not None:
                    self._last_used[auth_param] = _time.time()
                    return cached, None
            # Валидация и создание — вне lock (initialize делает сетевой I/O)
            creds = resolve_auth_source(auth_param)
            if not creds:
                return None, "Не удалось распарсить переданный auth-параметр"
            ok, msg = await verify_credentials(creds)
            if not ok:
                return None, f"Ошибка авторизации: {msg}"
            mgr = AliceAgentManager(creds, verbose=False)
            await mgr.initialize()
            async with self._lock:
                # Повторная проверка: пока initialize шёл, могли положить тот же auth
                existing = self._managers_cache.get(auth_param)
                if existing is not None:
                    try:
                        await mgr.shutdown()
                    except Exception:
                        pass
                    self._last_used[auth_param] = _time.time()
                    return existing, None
                await self._make_room_locked(incoming=1)
                self._managers_cache[auth_param] = mgr
                self._last_used[auth_param] = _time.time()
                return mgr, None
        if self.manager:
            return self.manager, None
        return None, "Авторизация не настроена."

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        mgr, err = await self._get_manager_for_call(arguments.get("auth"))
        if err or not mgr:
            return {"isError": True, "content": [{"type": "text", "text": f"[Ошибка]: {err}"}]}

        if tool_name.startswith("alice_ask_"):
            agent_id = tool_name[len("alice_ask_"):]
            msg = arguments.get("message", "")
            did = arguments.get("dialog_id")
            final_turn = None
            async for turn in mgr.send_message(msg, agent_id=agent_id, dialog_id=did):
                final_turn = turn
            if not final_turn:
                return {"isError": True, "content": [{"type": "text", "text": "Нет ответа"}]}
            if final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": f"Ошибка: {final_turn.error}"}]}
            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "thinking_steps": [s.to_dict() for s in final_turn.thinking_steps],
                "sources": final_turn.sources,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        elif tool_name == "alice_click_action":
            action_text = arguments.get("action_text", "")
            did = arguments.get("dialog_id")
            final_turn = None
            async for turn in mgr.send_message(action_text, dialog_id=did, is_suggest=True):
                final_turn = turn
            if not final_turn or final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": final_turn.error if final_turn else "Ошибка"}]}
            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        return {"isError": True, "content": [{"type": "text", "text": f"Неизвестный инструмент: {tool_name}"}]}


    async def run_stdio(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_running_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_running_loop())

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                try:
                    req = json.loads(line_str)
                    req_id = req.get("id")
                    method = req.get("method")
                    params = req.get("params", {})
                    if method == "initialize":
                        res = {"jsonrpc": "2.0", "id": req_id, "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "yandex-alice-server", "version": "4.6.0"},
                        }}
                    elif method == "notifications/initialized":
                        continue
                    elif method == "tools/list":
                        res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._get_tools_definition()}}
                    elif method == "tools/call":
                        call_result = await self.handle_tool_call(params.get("name"), params.get("arguments", {}))
                        res = {"jsonrpc": "2.0", "id": req_id, "result": call_result}
                    elif method == "ping":
                        res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                    else:
                        res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
                    writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                except Exception as e:
                    # Не утекаем трейсбек клиенту — только тип и безопасное сообщение
                    err = {"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32603,
                                     "message": f"internal error: {type(e).__name__}"}}
                    try:
                        writer.write((json.dumps(err) + "\n").encode("utf-8"))
                        await writer.drain()
                    except Exception:
                        pass
        finally:
            # Закрыть все кэшированные менеджеры (WS + ephemeral dialogs)
            try:
                await self._shutdown_all()
            except Exception:
                pass
