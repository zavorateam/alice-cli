#!/usr/bin/env python3
"""Пакет agents: экспорт базовых классов + явный идемпотентный auto_discover.

ВАЖНО: на импорте пакета агенты НЕ регистрируются. Импорт не должен
иметь побочных эффектов (это ломало MCP dynamic mode: registry оказывался
заполнен до того, как кто-то явно инициализировал реестр).

Регистрация происходит через явный вызов ensure_agents_registered() из:
  - AliceMcpServer.__init__       (MCP-режим)
  - AliceAgentManager.initialize  (CLI/HTTP-режим)
"""
import logging
import os

from agents.base import (
    AgentWidget,
    AliceAgent,
    AliceTurnResponse,
    SuggestionAction,
    ThinkingStep,
    decode_dialog_action,
)
from agents.registry import AgentRegistry, registry

__all__ = [
    "AgentWidget",
    "AliceAgent",
    "AliceTurnResponse",
    "SuggestionAction",
    "ThinkingStep",
    "decode_dialog_action",
    "AgentRegistry",
    "registry",
    "ensure_agents_registered",
]

_log = logging.getLogger("agents")
_agents_dir = os.path.dirname(os.path.abspath(__file__))
_discovered = False


def ensure_agents_registered() -> int:
    """Идемпотентная регистрация встроенных агентов.

    _discovered ставится в True только после успешной регистрации (>0 агентов).
    При частичном сбое discovery повторный вызов попробует снова.
    """
    global _discovered
    # Defensive: если _discovered=True, но registry пуст (тест очистил) — пересканируем
    if _discovered and not registry.list_all():
        _log.info("ensure_agents_registered: _discovered=True но registry пуст — пересканирую")
        _discovered = False
    if not _discovered:
        try:
            count = registry.auto_discover(_agents_dir, "agents")
        except Exception as e:
            _log.warning(f"ensure_agents_registered: discovery упал: {e!r}")
            count = 0
        if count > 0:
            _discovered = True
        else:
            _log.warning("ensure_agents_registered: 0 агентов — повтор при следующем вызове")
    return len(registry.list_all())


def reset_registration_state() -> None:
    """Для тестов: сбросить флаг, не трогая registry."""
    global _discovered
    _discovered = False
