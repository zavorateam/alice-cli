"""Разбор ответов Alice + дельта-логика DeferredAliceResponse.

parse_nsb_card   — тонкая обёртка над alice_server._parse_nsb_card
parse_rich_uicard — dispatch через agent.parse_card (taxi/lavka/market/...)
compute_delta(prev, current) -> (delta, new_prev)
    Снимок: prev является префиксом current → delta = current[len(prev):]
    Регресс: current является префиксом prev → delta = "" (не теряем prev)
    Дельта: ни один не префикс → delta = current, new_prev = prev + current
"""
from __future__ import annotations
from typing import Any, Dict, List, Tuple


def parse_nsb_card(card: Dict[str, Any]):
    """Возвращает (text, thinking_steps, sources)."""
    from alice_server import _parse_nsb_card
    return _parse_nsb_card(card)


def parse_rich_uicard(card: Dict[str, Any], agent, turn, text_blocks: List[str]) -> bool:
    """Dispatch rich_uicard через agent.parse_card. Возвращает True если обработан."""
    if agent is None:
        return False
    try:
        return bool(agent.parse_card(card, turn, text_blocks))
    except Exception:
        return False


def compute_delta(prev: str, current: str) -> Tuple[str, str]:
    prev = prev or ""
    current = current or ""
    if not current:
        return "", prev
    if not prev:
        return current, current
    if current == prev:
        return "", prev
    if current.startswith(prev):
        return current[len(prev):], current
    if prev.startswith(current):
        return "", prev
    return current, prev + current
