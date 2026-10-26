#!/usr/bin/env python3
"""
Tool-calling bridge. Пропускает thinking_steps/sources от Alice.
"""
import json
import logging
import re
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional

from agents.base import AliceTurnResponse
from alice_server import AliceAgentManager
from local_tools import LocalToolRegistry

logger = logging.getLogger("tool_bridge")

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)
TOOL_CALL_STRIP_RE = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL | re.IGNORECASE)
TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>\s*(\{.*)", re.DOTALL | re.IGNORECASE)

MAX_TOOL_RESULT_CHARS = 4000
CLI_RESULT_PREVIEW_CHARS = 800

SYSTEM_PROMPT = """[SYSTEM — Локальные инструменты]

Ты — Алиса, можешь выполнять действия на машине пользователя через инструменты.

Доступные инструменты:
{tools_list}

КРИТИЧНЫЕ ПРАВИЛА:
1. Если нужен инструмент — верни ТОЛЬКО:
   <tool_call>{{"tool":"имя","args":{{...}}}}</tool_call>
   Один вызов за раз, без текста вокруг.
2. После вызова придёт <tool_result tool="...">...</tool_result> — продолжи.
3. Если инструмент вернул ошибку — НЕ ВЫДУМЫВАЙ данные.
4. Чтобы СОХРАНИТЬ файл пользователю — write_file (НЕ attach_file).
   Для .xlsx/.docx — сначала write_file со скриптом .py, затем shell python3 <path>.
5. Финальный ответ — обычный текст БЕЗ <tool_call>.

[КОНЕЦ СИСТЕМЫ]

{user_text}"""

REMINDER = (
    "\n\n[Напоминание: только <tool_call>…</tool_call>. "
    "Если инструмент вернул ошибку — не выдумывай.]"
)


class LocalToolBridge:
    def __init__(self, manager, tools, agent_id="pro", max_iterations=10,
                 approve_callback=None, show_thinking=False):
        self.manager = manager
        self.tools = tools
        self.agent_id = agent_id
        self.max_iterations = max_iterations
        self.approve_callback = approve_callback
        self.show_thinking = show_thinking

    def _build_prompt(self, user_text: str) -> str:
        return SYSTEM_PROMPT.format(
            tools_list=self.tools.describe_for_prompt(),
            user_text=user_text,
        )

    @staticmethod
    def _sanitize(text: str) -> str:
        if not text:
            return text
        return TOOL_CALL_STRIP_RE.sub("", text).strip()

    def _parse_closed(self, text: str) -> List[str]:
        return TOOL_CALL_RE.findall(text or "")

    def _parse_open_fallback(self, text: str) -> List[str]:
        if "<tool_call>" not in (text or "").lower():
            return []
        m = TOOL_CALL_OPEN_RE.search(text)
        if not m:
            return []
        raw = m.group(1).strip()
        decoder = json.JSONDecoder()
        try:
            _, end = decoder.raw_decode(raw)
            return [raw[:end]]
        except json.JSONDecodeError:
            pass
        opens = raw.count("{") - raw.count("}")
        if opens > 0:
            balanced = raw + "}" * opens
            try:
                _, end = decoder.raw_decode(balanced)
                return [balanced[:end]]
            except json.JSONDecodeError:
                pass
        for trim in range(1, min(len(raw), 800)):
            candidate = raw[: len(raw) - trim].rstrip().rstrip(",")
            opens = candidate.count("{") - candidate.count("}")
            if opens > 0:
                candidate = candidate + "}" * opens
            try:
                _, end = decoder.raw_decode(candidate)
                return [candidate[:end]]
            except json.JSONDecodeError:
                continue
        return []

    def _parse_calls(self, text: str) -> List[str]:
        calls = self._parse_closed(text)
        if not calls:
            calls = self._parse_open_fallback(text)
        return calls

    async def send_with_tools(
        self, user_text: str, dialog_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        initial_attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        effective_agent = agent_id or self.agent_id
        current = self._build_prompt(user_text)
        final_dialog_id = dialog_id
        next_attachments = list(initial_attachments or [])

        for iteration in range(self.max_iterations):
            yield AliceTurnResponse(
                dialog_id=final_dialog_id or "",
                request_id=f"bridge_wait_{iteration}",
                thinking_logs=[f"⏳ Итерация {iteration + 1}"],
                is_finished=False,
            )

            final_turn: Optional[AliceTurnResponse] = None
            async for turn in self.manager.send_message(
                text=current, agent_id=effective_agent,
                dialog_id=final_dialog_id, attachments=next_attachments,
            ):
                if turn.dialog_id:
                    final_dialog_id = turn.dialog_id
                final_turn = turn

            next_attachments = []

            if not final_turn:
                yield AliceTurnResponse(
                    dialog_id=final_dialog_id or "",
                    request_id="bridge_empty",
                    text="", is_finished=True, error="Пустой ответ",
                )
                return
            if final_turn.error:
                yield final_turn
                return

            raw_text = final_turn.text or ""
            calls = self._parse_calls(raw_text)

            if not calls:
                clean = self._sanitize(raw_text)
                yield AliceTurnResponse(
                    dialog_id=final_turn.dialog_id,
                    request_id=final_turn.request_id,
                    thinking_logs=list(final_turn.thinking_logs),
                    thinking_steps=list(final_turn.thinking_steps),
                    text=clean,
                    widgets=list(final_turn.widgets),
                    suggestions=list(final_turn.suggestions),
                    sources=list(final_turn.sources),
                    is_finished=True,
                    error=None,
                )
                return

            results_text: List[str] = []
            pending_attachments: List[Dict[str, Any]] = []

            for raw_call in calls:
                try:
                    call = json.loads(raw_call)
                except json.JSONDecodeError as e:
                    results_text.append(f'<tool_result error="parse">{e}</tool_result>')
                    yield AliceTurnResponse(
                        dialog_id=final_dialog_id or "",
                        request_id=f"bridge_parse_err_{iteration}",
                        thinking_logs=[f"⚠ parse error: {e}"],
                        is_finished=False,
                    )
                    continue

                name = call.get("tool") or call.get("name")
                args = call.get("args") or call.get("arguments") or {}
                if not name:
                    results_text.append('<tool_result error="no_tool">Missing "tool"</tool_result>')
                    continue

                yield AliceTurnResponse(
                    dialog_id=final_dialog_id or "",
                    request_id=f"bridge_tool_{iteration}_{name}",
                    thinking_logs=[f"🔧 {name}: {json.dumps(args, ensure_ascii=False)[:200]}"],
                    is_finished=False,
                )

                if self.approve_callback:
                    try:
                        approved = await self.approve_callback(name, args)
                    except Exception:
                        approved = False
                    if not approved:
                        results_text.append(
                            f'<tool_result tool="{name}" denied="true">Отклонено</tool_result>'
                        )
                        yield AliceTurnResponse(
                            dialog_id=final_dialog_id or "",
                            request_id=f"bridge_denied_{iteration}_{name}",
                            thinking_logs=[f"✗ {name}: отклонено"],
                            is_finished=False,
                        )
                        continue

                res = await self.tools.execute(name, args)
                out = res.output if res.success else (res.error or res.output or "(пусто)")
                out_for_alice = out if len(out) <= MAX_TOOL_RESULT_CHARS else \
                    out[:MAX_TOOL_RESULT_CHARS] + f"\n... [обрезано {len(out)}B]"
                results_text.append(f'<tool_result tool="{name}">{out_for_alice}</tool_result>')

                display = out if len(out) <= CLI_RESULT_PREVIEW_CHARS else \
                    out[:CLI_RESULT_PREVIEW_CHARS] + f"… ({len(out)}B)"
                prefix = "→" if res.success else "✗"
                yield AliceTurnResponse(
                    dialog_id=final_dialog_id or "",
                    request_id=f"bridge_result_{iteration}_{name}",
                    thinking_logs=[f"{prefix} {name}:\n{display}"],
                    is_finished=False,
                )

                if res.success and res.attachments:
                    pending_attachments.extend(res.attachments)

            if pending_attachments:
                ws = getattr(self.manager, "ws", None)
                if ws is None:
                    results_text.append('<attachment_result ok="false">WS недоступен</attachment_result>')
                else:
                    for att in pending_attachments:
                        if "file_guid" in att:
                            next_attachments.append(att)
                            continue
                        path = att.get("path")
                        if not path:
                            continue
                        try:
                            upload = await ws.upload_file(path, title=att.get("title"))
                        except Exception as e:
                            logger.warning(f"upload_file: {e}")
                            upload = None
                        if not upload:
                            results_text.append(
                                f'<attachment_result ok="false" file="{att.get("title") or path}">upload failed</attachment_result>'
                            )
                            continue
                        next_attachments.append({
                            "file_guid": upload["file_guid"],
                            "title": upload.get("title"),
                            "mime_type": upload.get("mime_type"),
                        })
                        results_text.append(
                            f'<attachment_result ok="true" file="{upload.get("title")}">Прикреплён</attachment_result>'
                        )
                        yield AliceTurnResponse(
                            dialog_id=final_dialog_id or "",
                            request_id=f"bridge_att_{iteration}",
                            thinking_logs=[f"📎 {upload.get('title')}"],
                            is_finished=False,
                        )

            current = "\n\n".join(results_text) + REMINDER

        yield AliceTurnResponse(
            dialog_id=final_dialog_id or "",
            request_id="bridge_max_iter",
            text=f"\n[Лимит итераций: {self.max_iterations}]",
            is_finished=True,
        )