"""Разбор ответов Alice + дельта-логика DeferredAliceResponse."""

from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Tuple

from agents.base import ThinkingStep

logger = logging.getLogger("alice_parser")


def _parse_nsb_card(card: Dict[str, Any]) -> Tuple[str, List[ThinkingStep], List[Dict[str, Any]]]:
    nsb = card.get("neuro_structured_blocks_card")
    if not nsb:
        return "", [], []
    structured = nsb.get("json_structured_data")
    if not structured:
        return "", [], []
    try:
        data = json.loads(structured) if isinstance(structured, str) else structured
    except (json.JSONDecodeError, TypeError):
        return "", [], []
    blocks_data = data.get("BlocksData", [])
    tree_repr = data.get("TreeHierarchyRepresentation", {})
    if not blocks_data:
        return "", [], []
    blocks_map = {b.get("Id"): b for b in blocks_data if b.get("Id")}
    answer_parts: List[str] = []
    thinking_steps: List[ThinkingStep] = []

    def walk(block_id: str):
        block = blocks_map.get(block_id)
        if not block:
            return
        ct = block.get("ContentType", "")
        if ct == "VerticalComposition":
            for cid in block.get("CompositionInfo", {}).get("ChildIds", []):
                walk(cid)
            return
        if ct == "ReasoningSummary":
            rs = block.get("ReasoningSummary", {})
            title = rs.get("Title")
            content = rs.get("Content")
            tool_title = rs.get("ToolTitle")
            tool_type = rs.get("ToolType")
            if tool_title and tool_type:
                thinking_steps.append(ThinkingStep(kind="tool", title=tool_title, tool=tool_type))
            elif title and not content:
                thinking_steps.append(ThinkingStep(kind="reasoning_title", title=title))
            elif content:
                thinking_steps.append(ThinkingStep(kind="reasoning_content", content=content))
            return
        if ct == "Markdown":
            md = block.get("Markdown", {})
            text = (md.get("TargetMarkdownText") or "").strip()
            if text:
                answer_parts.append(text)
            return

    walk(tree_repr.get("Id", ""))
    sources: List[Dict[str, Any]] = []
    src_json = nsb.get("json_source_data")
    if src_json:
        try:
            parsed = json.loads(src_json) if isinstance(src_json, str) else src_json
            if isinstance(parsed, list):
                sources = parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return "\n\n".join(answer_parts), thinking_steps, sources


def parse_nsb_card(card):
    """Возвращает (text, thinking_steps, sources)."""
    return _parse_nsb_card(card)


def parse_rich_uicard(card, agent, turn, text_blocks):
    """Dispatch rich_uicard через agent.parse_card (только для sync-агентов).

    Для async-агентов (LavkaAgent) — вызывать через AliceWsEngine._listen_loop.
    """
    if agent is None:
        return False
    try:
        result = agent.parse_card(card, turn, text_blocks)
        if hasattr(result, "__await__"):
            return False
        return bool(result)
    except Exception:
        return False


def compute_delta(prev, current):
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
