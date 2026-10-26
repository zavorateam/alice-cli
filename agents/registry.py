#!/usr/bin/env python3
"""
Динамический реестр. auto_discover логирует ошибки вместо молчаливого pass.
"""

import importlib
import inspect
import logging
import os
import pkgutil
from typing import Dict, List, Optional

from agents.base import AliceAgent

logger = logging.getLogger("agents.registry")


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
        ident = (identifier or "pro").strip().lower()
        if ident in self._alias_map:
            return self._alias_map[ident]
        for alias, agent in self._alias_map.items():
            if ident in alias:
                return agent
        if "pro" in self._alias_map:
            return self._alias_map["pro"]
        if self._agents:
            return next(iter(self._agents.values()))
        raise RuntimeError(
            "Реестр агентов пуст. auto_discover ничего не зарегистрировал. "
            "Запусти с -v, чтобы увидеть причины."
        )

    def list_all(self) -> List[AliceAgent]:
        return list(self._agents.values())

    def all_aliases(self) -> Dict[str, AliceAgent]:
        return self._alias_map

    def auto_discover(self, package_dir: Optional[str] = None, package_name: str = "agents") -> int:
        if not package_dir:
            package_dir = os.path.dirname(os.path.abspath(__file__))
        package_dir = os.path.abspath(package_dir)

        if not os.path.isdir(package_dir):
            logger.error(f"auto_discover: {package_dir} не директория")
            return 0

        found = 0
        for _, module_name, _ in pkgutil.iter_modules([package_dir]):
            if module_name in ("base", "registry", "__init__"):
                continue
            full_name = f"{package_name}.{module_name}"
            try:
                mod = importlib.import_module(full_name)
            except Exception as e:
                logger.error(f"auto_discover: импорт {full_name} упал: {e!r}")
                continue

            for cls_name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls is AliceAgent:
                    continue
                try:
                    if not issubclass(cls, AliceAgent):
                        continue
                except TypeError:
                    continue
                try:
                    instance = cls()
                    self.register(instance)
                    found += 1
                except Exception as e:
                    logger.error(f"auto_discover: {full_name}.{cls_name}() упал: {e!r}")

        logger.info(f"auto_discover: +{found} агентов из {package_dir}")
        return found


registry = AgentRegistry()