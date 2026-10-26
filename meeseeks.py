#!/usr/bin/env python3
"""
Mr. Meeseeks. С детектором ошибок в тексте + проброс thinking_steps.
"""
import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agents import registry
from agents.base import AliceTurnResponse, ThinkingStep
from alice_server import AliceAgentManager, uuid7
from local_tools import LocalTool, LocalToolRegistry, ToolResult

logger = logging.getLogger("meeseeks")

_meeseeks_depth: contextvars.ContextVar[int] = contextvars.ContextVar("meeseeks_depth", default=0)

MeeseeksEvent = Callable[[str, str, str, str], Awaitable[None]]

ERROR_MARKERS = [
    "ошибка 401", "ошибка 403", "ошибка 500", "ошибка 502", "ошибка 503",
    "http 401", "http 403", "401 unauthorized",
    "your_api_key", "нужен api", "не удалось", "не могу", "cannot access",
    "access denied", "connection refused", "таймаут", "timed out",
]


@dataclass
class MeeseeksResult:
    handle: str
    task: str
    success: bool
    text: str
    error: Optional[str] = None
    agent_id: str = "pro"
    dialog_id: Optional[str] = None
    depth: int = 0
    elapsed: float = 0.0
    thinking_logs: List[str] = field(default_factory=list)
    thinking_steps: List[ThinkingStep] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)


def _looks_like_error(text: str) -> Optional[str]:
    if not text:
        return None
    lower = text.lower()
    for m in ERROR_MARKERS:
        if m in lower:
            return f"ответ содержит признак ошибки: {m!r}"
    return None


class _SubManager:
    def __init__(self, parent: AliceAgentManager, dialog_id: str, agent_id: str = "pro"):
        self.parent = parent
        self.dialog_id = dialog_id
        self.prev_req_id: Optional[str] = None
        self.ws = parent.ws
        self.rpc = parent.rpc
        self.active_dialog_id = dialog_id
        self.active_agent_id = agent_id
        self.known_dialogs = {dialog_id}
        self.ephemeral_dialogs = {dialog_id}

    async def send_message(self, text, agent_id=None, dialog_id=None, is_suggest=False, attachments=None):
        agent = registry.get(agent_id or self.active_agent_id)
        target = dialog_id or self.dialog_id
        last_turn = None
        async for turn in self.ws.send_text_input(
            text=text, dialog_id=target, agent=agent,
            prev_req_id=self.prev_req_id, is_suggest=is_suggest, attachments=attachments,
        ):
            turn.dialog_id = target
            last_turn = turn
            yield turn
        if last_turn and not last_turn.error:
            self.prev_req_id = last_turn.request_id


BridgeFactory = Callable[[Any, LocalToolRegistry], Any]


class MeeseeksPool:
    def __init__(
        self, manager, tools, bridge_factory: BridgeFactory,
        max_depth: int = 3, max_concurrent: int = 5,
        on_event: Optional[MeeseeksEvent] = None,
    ):
        self.manager = manager
        self.tools = tools
        self.bridge_factory = bridge_factory
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.on_event = on_event
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _resolve_agent(self, agent_id: str):
        all_ids = {a.id for a in registry.list_all()}
        if agent_id in all_ids:
            return agent_id, None
        fallback = registry.get(agent_id)
        return fallback.id, f"agent '{agent_id}' не найден, использую '{fallback.id}'"

    async def _emit(self, kind: str, handle: str, agent_id: str, info: str):
        if self.on_event:
            try:
                await self.on_event(kind, handle, agent_id, info)
            except Exception as e:
                logger.debug(f"on_event: {e}")

    async def spawn(self, task: str, agent_id: str = "pro", timeout: float = 180.0) -> MeeseeksResult:
        parent_depth = _meeseeks_depth.get()
        new_depth = parent_depth + 1
        handle = uuid.uuid4().hex[:8]

        resolved_id, warning = self._resolve_agent(agent_id)
        if warning:
            await self._emit("warn", handle, resolved_id, warning)

        if new_depth > self.max_depth:
            await self._emit("deny", handle, resolved_id, f"предел {self.max_depth}")
            return MeeseeksResult(handle, task, False, "",
                                  error=f"Предел вложенности ({self.max_depth})",
                                  agent_id=resolved_id, depth=new_depth)

        if new_depth == 1:
            async with self._semaphore:
                return await self._do_spawn(handle, task, resolved_id, new_depth, timeout)
        return await self._do_spawn(handle, task, resolved_id, new_depth, timeout)

    async def _do_spawn(self, handle, task, agent_id, depth, timeout):
        start = time.time()
        dialog_id = uuid7()
        try:
            await self.manager.ws.add_full_content(dialog_id)
        except Exception:
            pass

        sub_mgr = _SubManager(self.manager, dialog_id, agent_id=agent_id)
        bridge = self.bridge_factory(sub_mgr, self.tools)
        token = _meeseeks_depth.set(depth)
        collected_text, thinking_logs, thinking_steps, sources, error = [], [], [], [], None

        await self._emit("start", handle, agent_id, task[:70] + ("…" if len(task) > 70 else ""))

        wrapped_task = (
            f"{task}\n\n"
            f"[Внутреннее: если не можешь получить данные — ответь строго 'НЕ УДАЛОСЬ: <причина>'. "
            f"Никогда не выдумывай числа/факты.]"
        )

        async def _consume():
            nonlocal error
            async for turn in bridge.send_with_tools(
                user_text=wrapped_task, dialog_id=dialog_id, agent_id=agent_id,
            ):
                if turn.thinking_logs:
                    thinking_logs.extend(turn.thinking_logs)
                if turn.thinking_steps:
                    thinking_steps.extend(turn.thinking_steps)
                if turn.sources:
                    sources.extend(turn.sources)
                if turn.error:
                    error = turn.error
                    return
                if turn.text and turn.is_finished:
                    collected_text.append(turn.text)

        try:
            await asyncio.wait_for(_consume(), timeout=timeout)
        except asyncio.TimeoutError:
            error = f"Таймаут ({timeout:.0f}s)"
        except asyncio.CancelledError:
            error = "Отменён"
            raise
        except Exception as e:
            logger.exception("meeseeks crash")
            error = str(e)
        finally:
            _meeseeks_depth.reset(token)
            try:
                await self.manager.ws.remove_full_content(dialog_id)
            except Exception:
                pass
            try:
                await self.manager.rpc.delete_dialog(dialog_id)
            except Exception:
                pass

        text = "\n".join(collected_text).strip()

        if error is None and text:
            detected = _looks_like_error(text)
            if detected:
                error = f"мисикс вернул: {detected}"
            elif text.startswith("НЕ УДАЛОСЬ"):
                error = text[:200]

        elapsed = time.time() - start
        status = "done" if error is None else "fail"
        await self._emit(status, handle, agent_id,
                         f"{elapsed:.1f}s" + (f" — {error[:60]}" if error else ""))

        return MeeseeksResult(handle, task, error is None, text, error,
                              agent_id, dialog_id, depth, elapsed,
                              thinking_logs, thinking_steps, sources)

    async def spawn_batch(self, tasks: List[str], agent_id: str = "pro",
                          timeout: float = 180.0) -> List[MeeseeksResult]:
        coros = [self.spawn(t, agent_id=agent_id, timeout=timeout) for t in tasks]
        return await asyncio.gather(*coros, return_exceptions=False)


class MeeseeksTool(LocalTool):
    name = "meeseeks"
    description = (
        "Создать эфимерного саб-агента. agent_id: pro, base, expert, deep_research. "
        "Работает в изоляции, диалог удаляется. Для параллельных подзадач и изоляции контекста."
    )
    arg_schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string"},
            "agent_id": {"type": "string", "default": "pro"},
        },
        "required": ["task"],
    }

    def __init__(self, pool: MeeseeksPool):
        self.pool = pool

    async def execute(self, args):
        task = (args.get("task") or "").strip()
        if not task:
            return ToolResult(False, "", "Пустая задача")
        agent_id = args.get("agent_id") or "pro"
        r = await self.pool.spawn(task=task, agent_id=agent_id)
        return ToolResult(r.success, r.text or "(пусто)", error=r.error)


class MeeseeksBatchTool(LocalTool):
    name = "meeseeks_batch"
    description = (
        "Параллельно запустить N мисиксов. Формат: «задача» или «agent_id:задача»."
    )
    arg_schema = {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "string"}},
            "agent_id": {"type": "string", "default": "pro"},
        },
        "required": ["tasks"],
    }

    def __init__(self, pool: MeeseeksPool):
        self.pool = pool

    async def execute(self, args):
        raw_tasks = args.get("tasks") or []
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return ToolResult(False, "", "Пустой список")
        default_agent = args.get("agent_id") or "pro"
        known_ids = {a.id for a in registry.list_all()}
        parsed = []
        for t in raw_tasks:
            t = t.strip()
            if ":" in t:
                prefix, rest = t.split(":", 1)
                if prefix.strip() in known_ids:
                    parsed.append((prefix.strip(), rest.strip()))
                    continue
            parsed.append((default_agent, t))

        coros = [self.pool.spawn(task=task, agent_id=agent) for agent, task in parsed]
        results = await asyncio.gather(*coros, return_exceptions=False)

        lines = []
        any_fail = False
        for i, r in enumerate(results, 1):
            st = "✅" if r.success else "❌"
            if not r.success:
                any_fail = True
            short = r.task[:60] + ("…" if len(r.task) > 60 else "")
            lines.append(f"[{i}] {st} ({r.elapsed:.1f}s, {r.agent_id}) {short}")
            body = (r.text if r.success else (r.error or "неизвестно") + ("\n\n" + r.text if r.text else "")) or "(пусто)"
            for ln in body.splitlines():
                lines.append(f"    {ln}")
            lines.append("")
        return ToolResult(not any_fail, "\n".join(lines).rstrip(),
                          error=None if not any_fail else "часть мисиксов провалилась")