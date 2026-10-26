#!/usr/bin/env python3
"""
Пакет agents: экспорт базовых классов + автодискавери.
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
]

_log = logging.getLogger("agents")
_agents_dir = os.path.dirname(os.path.abspath(__file__))
_count = registry.auto_discover(_agents_dir, "agents")
if _count == 0:
    _log.warning(f"auto_discover: 0 агентов в {_agents_dir}")