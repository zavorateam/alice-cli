import os
from agents.base import AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction, decode_dialog_action
from agents.registry import registry

# Автоматически загружаем всех агентов из текущей папки
registry.auto_discover(os.path.dirname(__file__), "agents")