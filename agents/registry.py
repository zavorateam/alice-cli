#!/usr/bin/env python3
"""
Динамический реестр агентов с автоматическим обнаружением файлов.
"""

import importlib
import inspect
import os
import pkgutil
from typing import Dict, List, Optional
from agents.base import AliceAgent


class AgentRegistry:
    def __init__(self):
        self._agents: Dict[str, AliceAgent] = {}
        self._alias_map: Dict[str, AliceAgent] = {}

    def register(self, agent: AliceAgent):
        self._agents[agent.id] = agent
        self._alias_map[agent.id.lower()] = agent
        self._alias_map[agent.name.lower()] = agent
        for alias in agent.aliases:
            self._alias_map[alias.lower()] = agent

    def get(self, identifier: Optional[str]) -> AliceAgent:
        if not identifier:
            return self._alias_map.get("pro") or list(self._agents.values())[0]
        ident = identifier.strip().lower()
        if ident in self._alias_map:
            return self._alias_map[ident]
        for alias, agent in self._alias_map.items():
            if ident in alias:
                return agent
        return self._alias_map.get("pro") or list(self._agents.values())[0]

    def list_all(self) -> List[AliceAgent]:
        return list(self._agents.values())

    def all_aliases(self) -> Dict[str, AliceAgent]:
        return self._alias_map

    def auto_discover(self, package_dir: Optional[str] = None, package_name: str = "agents"):
        if not package_dir:
            package_dir = os.path.dirname(__file__)
        for _, module_name, is_pkg in pkgutil.iter_modules([package_dir]):
            if module_name in ("base", "registry", "__init__"):
                continue
            full_module_name = f"{package_name}.{module_name}"
            try:
                mod = importlib.import_module(full_module_name)
                for name, obj in inspect.getmembers(mod, inspect.isclass):
                    if issubclass(obj, AliceAgent) and obj is not AliceAgent:
                        instance = obj()
                        self.register(instance)
            except Exception as e:
                pass


registry = AgentRegistry()