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

    Первый вызов сканирует пакет и регистрирует агентов.
    Последующие — no-op (возвращают количество зарегистрированных).
    Dynamic apps (get_alice_apps) регистрируются отдельно в
    AliceAgentManager.initialize, они не входят в этот набор.
    """
    global _discovered
    if _discovered:
        return len(registry.list_all())
    _discovered = True
    count = registry.auto_discover(_agents_dir, "agents")
    if count == 0:
        _log.warning(f"ensure_agents_registered: 0 агентов в {_agents_dir}")
    return len(registry.list_all())
