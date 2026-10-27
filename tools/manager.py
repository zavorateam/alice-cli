"""Управление агентами/диалогами + MCP-сервер + SessionRegistry.

Реэкспорт реальных классов пока не переносим: alice_server.py остаётся
источником истины до полной деинтеграции. Когда перенос завершится,
модуль сам станет источником, а alice_server.py будет реэкспортировать.
"""
from __future__ import annotations

import asyncio
import uuid as _uuid
from typing import Dict, Optional

from alice_server import (
    AliceAgentManager,
    AliceMcpServer,
    GenericAppAgent,
    uuid7,
)

__all__ = [
    "AliceAgentManager",
    "AliceMcpServer",
    "GenericAppAgent",
    "uuid7",
    "ChatSession",
    "SessionRegistry",
]


class ChatSession:
    """Изолированная сессия для одного HTTP-клиента OpenAI-совместимого API."""

    def __init__(self, session_id: str, manager: AliceAgentManager):
        self.session_id = session_id
        self.manager = manager
        self.dialog_id: Optional[str] = None
        self._lock = asyncio.Lock()

    async def ensure_dialog(self, agent_id: str = "pro") -> str:
        async with self._lock:
            if not self.dialog_id:
                self.dialog_id = await self.manager.create_new_dialog(
                    agent_id=agent_id, is_ephemeral=True,
                )
            return self.dialog_id

    async def send_message(self, text: str, agent_id: str = "pro", is_suggest: bool = False):
        did = await self.ensure_dialog(agent_id)
        async for turn in self.manager.send_message(
            text=text, agent_id=agent_id, dialog_id=did, is_suggest=is_suggest,
        ):
            yield turn

    async def cancel_current(self):
        # manager.send_message не держит отдельного task — отмена через HTTP-клиент
        # останавливает async-generator, ws.send_text_input сам снимает очередь.
        return None

    async def close(self, delete_dialog: bool = True):
        if delete_dialog and self.dialog_id:
            try:
                await self.manager.rpc.delete_dialog(self.dialog_id)
            except Exception:
                pass
        self.dialog_id = None


class SessionRegistry:
    """Реестр ChatSession по X-Alice-Session-ID.

    Factory возвращает готовый (уже инициализированный) AliceAgentManager.
    Для shared-manager сценария factory = lambda: manager — тогда shutdown()
    менеджера НЕ вызывается из ChatSession.close() (владелец — сервер).
    """

    def __init__(self, manager_factory):
        self._factory = manager_factory
        self._sessions: Dict[str, ChatSession] = {}
        self._owns_managers = False
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: Optional[str]) -> ChatSession:
        sid = session_id or _uuid.uuid4().hex
        async with self._lock:
            if sid not in self._sessions:
                mgr = await self._factory()
                self._sessions[sid] = ChatSession(sid, mgr)
            return self._sessions[sid]

    async def close(self):
        async with self._lock:
            for s in self._sessions.values():
                await s.close(delete_dialog=False)
            self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)
