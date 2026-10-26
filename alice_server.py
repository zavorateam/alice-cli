#!/usr/bin/env python3
"""
Alice AI Unified Server.
Парсит neuro_structured_blocks_card: Markdown → ответ, ReasoningSummary → thinking_steps.
"""

import argparse
import asyncio
import inspect
import json
import logging
import mimetypes
import os
import sys
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple
from datetime import datetime

import aiohttp
import websockets

from agents import AgentWidget, AliceAgent, AliceTurnResponse, SuggestionAction, ThinkingStep, registry
from alice_auth import (
    AliceCredentials,
    load_or_request_credentials,
    resolve_auth_source,
    verify_credentials,
)

WEBSOCKET_URI = "wss://uniproxy.alice.yandex.ru/uni.ws"
RPC_BASE_URL = "https://rpc.alice.yandex.ru"
ORIGIN = "https://alice.yandex.ru"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"

DEFAULT_EXPERIMENTS = [
    "dont_skip_cancel_requests", "enable_parallel_requests_to_chats",
    "read_dialogs_for_unauthorized_users", "mm_allow_anonymous_request",
    "enable_external_skills_for_webdesktop_and_webtouch",
    "send_show_view_directive_on_supports_show_view_layer_content_interface",
    "standalone_alice_2_0", "mm_enable_protocol_scenario=WebAliceControls",
    "exp_flag_chat_dialog_history", "exp_flag_chat_dialog_history_main_context_save",
    "div2cards_in_external_skills_for_web_standalone", "enable_find_poi_standalone",
    "use_server_pings", "enable_onboarding_adaptive_size",
    "standalone_show_fullscreen_image_gallery_directive",
    "draw_picture_enable_controls", "alice_has_borders_div_paddings",
    "enable_new_colors_for_alice_chat",
    "erase_serialized_response_from_json_deferred_alice_response",
    "skills_standalone_use_div_render", "standalone_skill_card_cloud_ui",
]

DEFAULT_SUPPORTED_FEATURES = [
    "background_response_streaming_for_dialog_controls",
    "background_response_streaming_in_read_dialog",
    "background_response_streaming", "background_response_streaming_anon",
    "supports_bso_answer", "open_link", "server_action", "show_promo",
    "reminders_and_todos", "div2_cards", "player_pause_directive",
    "can_open_dialogs_in_tabs", "supports_streaming_response",
    "supports_rich_json_cards", "builtin_reaction", "open_link_by_button",
    "supports_origin_in_separate_card", "supports_new_sources_cards",
    "supports_markdown_response", "supported_save_chathistory",
    "supported_load_chathistory", "supports_unlimited_dialogs_creation",
    "supports_multi_model_dialogs", "print_text_in_message_view",
    "show_loader_directive", "supports_stringbody_in_div2_card",
    "supports_default_dialog_as_dedicated", "whisper",
]

logger = logging.getLogger("alice_server")


def uuid7() -> str:
    """RFC 9562 UUIDv7 — совместим с форматом dialog_id и messageId Яндекса."""
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    h = bytes(b).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


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


class GenericAppAgent(AliceAgent):
    def __init__(self, app_id: str, name: str, aliases: Optional[List[str]] = None, description: str = ""):
        self.id = app_id
        self.name = name
        self.aliases = aliases or []
        self.type = "app"
        self.mode = "External"
        self.preset = "dialogovo_alice_apps_sticky"
        self.app_id = app_id
        self.description = description
        self.timeout = 300.0


class AliceRpcClient:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.headers = self.creds.build_rpc_headers()

    async def get_alice_apps(self) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/gproxy/get_alice_apps"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if status == 200:
                        return status, json.loads(body).get("apps", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def list_dialogs(self, limit: int = 15, not_entrypoint: bool = False) -> Tuple[int, List[Dict[str, Any]], str]:
        url = f"{RPC_BASE_URL}/dialog/list"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"limit": limit, "hints": {"not_entrypoint": not_entrypoint}}) as resp:
                    status = resp.status
                    body = await resp.text()
                    if status == 200:
                        return status, json.loads(body).get("objects", []), "OK"
                    return status, [], body[:250]
            except Exception as e:
                return 0, [], str(e)

    async def delete_dialog(self, dialog_id: str) -> Tuple[int, str]:
        url = f"{RPC_BASE_URL}/dialog/remove_dialog"
        async with aiohttp.ClientSession(headers=self.headers) as session:
            try:
                async with session.post(url, json={"dialog_id": dialog_id}) as resp:
                    return resp.status, await resp.text()
            except Exception as e:
                return 0, str(e)


class AliceWsEngine:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False, show_thinking: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.ws: Optional[Any] = None
        self.seq_number = 0
        self.session_id: Optional[str] = None
        self.is_connected = False
        self._pending_responses: Dict[str, asyncio.Queue] = {}
        self._running_task: Optional[asyncio.Task] = None
        self._active_agents_by_req: Dict[str, AliceAgent] = {}
        self._sync_event = asyncio.Event()

        self._upload_pending: Dict[str, asyncio.Queue] = {}
        self._upload_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_stream_id = 1

        self._full_content_dialogs: Set[str] = set()
        self._ping_dialogs: Set[str] = set()
        self._meeseeks_dialogs: Set[str] = set()

        self.expert_trial_left: Optional[int] = None
        self.expert_trial_limit: Optional[int] = None

    async def connect(self):
        headers = self.creds.build_ws_headers()
        sig = inspect.signature(websockets.connect)
        ws_kwargs: Dict[str, Any] = {"ping_interval": None}
        if "max_size" in sig.parameters:
            ws_kwargs["max_size"] = 20 * 1024 * 1024
        if "additional_headers" in sig.parameters:
            ws_kwargs["additional_headers"] = headers
        else:
            ws_kwargs["extra_headers"] = headers

        self.ws = await websockets.connect(WEBSOCKET_URI, **ws_kwargs)
        self.is_connected = True
        self.seq_number = 0
        self._sync_event.clear()
        self._running_task = asyncio.create_task(self._listen_loop())

        await self._synchronize_state()
        try:
            await asyncio.wait_for(self._sync_event.wait(), timeout=7.0)
            if self.verbose:
                logger.info(f"Сессия Uniproxy: {self.session_id}")
        except asyncio.TimeoutError:
            logger.warning("Таймаут SynchronizeStateResponse")

    async def disconnect(self):
        self.is_connected = False
        if self._running_task:
            self._running_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _send_json(self, data: dict):
        async with self._send_lock:
            if not self.ws or not self.is_connected:
                raise ConnectionError("WebSocket не подключен")
            self.seq_number += 1
            if "event" in data and "header" in data["event"]:
                data["event"]["header"]["seqNumber"] = self.seq_number
            await self.ws.send(json.dumps(data, ensure_ascii=False))

    async def _synchronize_state(self):
        msg_id = uuid7()
        payload = {
            "event": {
                "header": {
                    "namespace": "System", "name": "SynchronizeState",
                    "seqNumber": 1, "messageId": msg_id,
                },
                "payload": {
                    "auth_token": self.creds.get_ws_auth_token(),
                    "uuid": self.creds.device_uuid,
                    "vins": {
                        "application": {
                            "app_id": "ru.yandex.webstandalone.desktop",
                            "platform": "linux",
                            "device_id": self.creds.device_uuid,
                            "uuid": self.creds.device_uuid,
                        }
                    },
                    "supported_features": DEFAULT_SUPPORTED_FEATURES,
                    "request": {"experiments": DEFAULT_EXPERIMENTS},
                    "speechkitVersion": "4.16.7",
                    "icookie": self.creds.icookie,
                    "client_analytics_info": {"client_url": "https://alice.yandex.ru/chat/"},
                },
            }
        }
        await self._send_json(payload)

    async def _flush_subscriptions(self):
        if not self.is_connected:
            return
        subs: List[Dict[str, Any]] = []
        for did in self._full_content_dialogs:
            subs.append({"id": did, "state": {"full_content": {}}})
        for did in self._ping_dialogs:
            if did in self._full_content_dialogs:
                continue
            subs.append({"id": did, "state": {"ping": {}}})
        if not subs:
            return
        payload = {
            "event": {
                "header": {"namespace": "System", "name": "ClientSubscriptionState",
                           "messageId": uuid7()},
                "payload": {"subscriptions": subs},
            }
        }
        try:
            await self._send_json(payload)
        except Exception as e:
            logger.debug(f"_flush_subscriptions: {e}")

    async def update_subscriptions(self, active_dialog_id: str, other_dialog_ids: Set[str]):
        self._full_content_dialogs = {active_dialog_id} | self._meeseeks_dialogs
        self._ping_dialogs = other_dialog_ids - self._full_content_dialogs
        await self._flush_subscriptions()

    async def add_full_content(self, dialog_id: str):
        self._meeseeks_dialogs.add(dialog_id)
        self._full_content_dialogs.add(dialog_id)
        self._ping_dialogs.discard(dialog_id)
        await self._flush_subscriptions()

    async def remove_full_content(self, dialog_id: str):
        self._meeseeks_dialogs.discard(dialog_id)
        self._full_content_dialogs.discard(dialog_id)
        await self._flush_subscriptions()

    async def upload_file(
        self, file_path: str, title: Optional[str] = None,
        mime_type: Optional[str] = None, origin: str = "Chat", timeout: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        path = os.path.expanduser(file_path)
        if not os.path.isfile(path):
            logger.warning(f"upload_file: нет файла: {path}")
            return None
        size = os.path.getsize(path)
        if size == 0:
            return None
        title = title or os.path.basename(path)
        if not mime_type:
            guessed, _ = mimetypes.guess_type(path)
            mime_type = guessed or "application/octet-stream"

        async with self._upload_lock:
            stream_id = self._next_stream_id
            self._next_stream_id += 1
            message_id = uuid7()
            queue: asyncio.Queue = asyncio.Queue()
            self._upload_pending[message_id] = queue
            try:
                await self._send_json({
                    "event": {
                        "header": {"namespace": "File", "name": "Upload",
                                   "streamId": stream_id, "messageId": message_id},
                        "payload": {"file_upload_start": {
                            "mime_type": mime_type, "size": size,
                            "title": title, "origin": origin,
                        }},
                    }
                })
                try:
                    ack = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    return None
                start = ack.get("file_upload_start") or {}
                chunk_size = int(start.get("max_chunk_size") or 10 * 1024 * 1024)
                chunk_size = max(512 * 1024, min(chunk_size, 16 * 1024 * 1024))
                async with self._send_lock:
                    if not self.ws:
                        raise ConnectionError("WS закрыт")
                    with open(path, "rb") as f:
                        while True:
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            if not self.ws or not self.is_connected:
                                raise ConnectionError("WS закрыт")
                            await self.ws.send(chunk)
                await self._send_json({
                    "event": {
                        "header": {"namespace": "File", "name": "Upload",
                                   "streamId": stream_id, "messageId": uuid7(),
                                   "refMessageId": message_id},
                        "payload": {"chunk_upload_finish": {"is_last": True}},
                    }
                })
                deadline = asyncio.get_event_loop().time() + timeout
                finish: Dict[str, Any] = {}
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        return None
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return None
                    fp = msg.get("file_upload_finish")
                    if fp:
                        finish = fp
                        break
                file_guid = finish.get("file_guid")
                if not file_guid:
                    return None
                try:
                    if self.ws and self.is_connected:
                        await self.ws.send(json.dumps({
                            "streamcontrol": {"action": 0, "reason": 0,
                                              "streamId": stream_id, "messageId": message_id}
                        }, ensure_ascii=False))
                except Exception:
                    pass
                return {
                    "file_guid": file_guid,
                    "title": finish.get("title") or title,
                    "mime_type": finish.get("mime_type") or mime_type,
                    "size_bytes": finish.get("size_bytes") or size,
                    "preview_url": finish.get("preview_url"),
                    "extension": finish.get("extension"),
                }
            finally:
                self._upload_pending.pop(message_id, None)

    @staticmethod
    def _inject_chat_context(
        capabilities: List[Dict[str, Any]],
        dialog_id: str,
        agent: AliceAgent,
    ) -> List[Dict[str, Any]]:
        chat_ctx = {
            "dialog_id": dialog_id,
            "dialog_type": "DEDICATED_CHAT",
            "alice2_mode_info": {
                "preset": getattr(agent, "preset", "") or "",
                "mode": getattr(agent, "mode", "") or "Pro",
            },
        }
        for cap in capabilities:
            if isinstance(cap, dict) and str(cap.get("@type", "")).endswith("TAliceChatCapability"):
                state = cap.setdefault("state", {})
                if isinstance(state, dict):
                    state["active_chat_dialog_context"] = chat_ctx
                    return capabilities
        capabilities.append({
            "@type": "type.googleapis.com/NAlice.TAliceChatCapability",
            "meta": {
                "supported_directives": [
                    "ScrollToFragmentDirectiveType",
                    "LoginWithCallbackDirectiveType",
                    "ShowBannerDirectiveType",
                    "ShowNotificationDirectiveType",
                    "SetChatRequestModeDirectiveType",
                    "ShowFullscreenImageGalleryDirectiveType",
                    "DownloadImageDirectiveType",
                    "FillChatInputDirectiveType",
                    "CreateNewChatDirectiveType",
                    "OpenChatListDirectiveType",
                    "ChatNavigateBackDirectiveType",
                ]
            },
            "parameters": {
                "supports_rich_answers": True,
                "supports_rich_suggests": True,
                "supports_rich_summary": True,
            },
            "state": {
                "active_chat_dialog_context": chat_ctx,
            },
        })
        return capabilities

    async def send_text_input(
        self, text: str, dialog_id: str, agent: AliceAgent,
        prev_req_id: Optional[str] = None, is_suggest: bool = False,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        req_id = uuid7()
        capabilities = agent.build_capabilities(dialog_id, self.creds.device_uuid)
        capabilities = self._inject_chat_context(capabilities, dialog_id, agent)

        header_dict: Dict[str, Any] = {
            "request_id": req_id, "dialog_id": dialog_id, "dialog_type": 2,
        }
        if prev_req_id:
            header_dict["prev_req_id"] = prev_req_id

        event_type = "suggested_input" if is_suggest else "text_input"

        payload = {
            "event": {
                "header": {"namespace": "Vins", "name": "TextInput",
                           "messageId": req_id, "seqNumber": self.seq_number + 1},
                "payload": {
                    "application": {
                        "app_id": "ru.yandex.webstandalone.desktop",
                        "app_version": "unknown", "platform": "linux",
                        "os_version": USER_AGENT.lower(),
                        "uuid": self.creds.device_uuid,
                        "device_id": self.creds.device_uuid,
                        "lang": "ru-RU",
                        "client_time": datetime.now().strftime("%Y%m%dT%H%M%S"),
                        "timezone": "Europe/Moscow",
                        "timestamp": str(int(time.time())),
                    },
                    "header": header_dict,
                    "request": {
                        "event": {"type": event_type, "text": text},
                        "voice_session": False,
                        "experiments": DEFAULT_EXPERIMENTS,
                        "uniproxy_options": {
                            "background_response_streaming_options": {
                                "request_with_shown_new_dialog": {}
                            }
                        },
                        "additional_options": {
                            "bass_options": {"user_agent": USER_AGENT, "screen_scale_factor": 1},
                            "origin_domain": "yandex.ru",
                            "supported_features": DEFAULT_SUPPORTED_FEATURES,
                            "unsupported_features": [],
                            "icookie": self.creds.icookie,
                        },
                        "environment_state": {"endpoints": [
                            {"id": self.creds.device_uuid, "capabilities": capabilities}
                        ]},
                    },
                    "format": "audio/ogg;codecs=opus",
                    "mime": "audio/ogg;codecs=opus",
                    "topic": "desktopgeneral",
                    "punctuation": False,
                    "alice_2_settings": {"preset": agent.preset, "mode": agent.mode},
                },
            }
        }

        if attachments:
            payload["event"]["payload"]["request"]["attachments"] = attachments

        queue: asyncio.Queue = asyncio.Queue()
        self._pending_responses[req_id] = queue
        self._active_agents_by_req[req_id] = agent

        await self._send_json(payload)

        try:
            while True:
                resp = await asyncio.wait_for(queue.get(), timeout=agent.timeout)
                yield resp
                if resp.is_finished:
                    break
        except asyncio.TimeoutError:
            yield AliceTurnResponse(
                dialog_id=dialog_id, request_id=req_id,
                is_finished=True, error=f"Таймаут ответа ({agent.timeout} с).",
            )
        finally:
            self._pending_responses.pop(req_id, None)
            self._active_agents_by_req.pop(req_id, None)

    def _update_trial_state(self, payload: Dict[str, Any]):
        eff = payload.get("alice_2_effective_settings") or {}
        trial = eff.get("trial_state") or {}
        left = trial.get("left_count")
        limit = trial.get("limit")
        if left is not None:
            self.expert_trial_left = int(left)
            self.expert_trial_limit = int(limit) if limit is not None else self.expert_trial_limit

    async def _listen_loop(self):
        accumulated_turns: Dict[str, AliceTurnResponse] = {}

        while self.is_connected:
            try:
                if not self.ws:
                    logger.warning("ws is None, break listener")
                    break
                raw_msg = await self.ws.recv()
                if not raw_msg:
                    continue

                data = json.loads(raw_msg)
                directive = data.get("directive", {})
                header = directive.get("header", {})
                name = header.get("name")
                ref_id = header.get("refMessageId") or header.get("ref_message_id")

                if name == "Ping":
                    pong = {"event": {"header": {
                        "namespace": "System", "name": "Pong",
                        "messageId": uuid7(),
                        "refMessageId": header.get("messageId"),
                    }, "payload": {}}}
                    await self._send_json(pong)
                    continue

                if name == "SynchronizeStateResponse":
                    self.session_id = directive.get("payload", {}).get("SessionId")
                    self._sync_event.set()
                    continue

                if name == "VinsResponse":
                    payload = directive.get("payload", {})
                    if "alice_2_effective_settings" in payload:
                        self._update_trial_state(payload)
                    continue

                if name == "Upload" and ref_id in self._upload_pending:
                    await self._upload_pending[ref_id].put(directive.get("payload", {}))
                    continue

                if name in ("ErrorMessage", "Error") and ref_id in self._pending_responses:
                    err_text = directive.get("payload", {}).get("message", "Ошибка Uniproxy")
                    turn = AliceTurnResponse(dialog_id="", request_id=ref_id,
                                            is_finished=True, error=err_text)
                    await self._pending_responses[ref_id].put(turn)
                    continue

                if name == "DeferredAliceResponse" and ref_id in self._pending_responses:
                    payload = directive.get("payload", {})
                    json_resp = payload.get("json_response", {})
                    base_resp = json_resp.get("base_response", {})
                    is_last = json_resp.get("is_last", False)
                    cards = base_resp.get("cards", [])

                    if ref_id not in accumulated_turns:
                        accumulated_turns[ref_id] = AliceTurnResponse(dialog_id="", request_id=ref_id)

                    turn = accumulated_turns[ref_id]
                    turn.is_finished = is_last
                    text_blocks: List[str] = []
                    turn.widgets = []

                    agent = self._active_agents_by_req.get(ref_id) or registry.get("pro")

                    for card in cards:
                        answer_text, thinking_steps, sources = _parse_nsb_card(card)
                        if thinking_steps:
                            turn.thinking_steps = thinking_steps
                        if sources:
                            turn.sources = sources
                        if answer_text and answer_text not in text_blocks:
                            text_blocks.append(answer_text)
                        if answer_text or thinking_steps:
                            continue
                        handled = agent.parse_card(card, turn, text_blocks)
                        if handled:
                            continue
                        tc = card.get("text_card")
                        if tc and "text" in tc:
                            t = tc["text"].strip()
                            if t and t not in text_blocks:
                                text_blocks.append(t)
                        nsb = card.get("neuro_structured_blocks_card")
                        if nsb and not nsb.get("json_structured_data"):
                            pt = (nsb.get("plain_text") or "").strip()
                            if pt and pt not in text_blocks:
                                text_blocks.append(pt)

                    turn.text = "\n\n".join(text_blocks)
                    turn_copy = AliceTurnResponse(
                        dialog_id=turn.dialog_id,
                        request_id=turn.request_id,
                        thinking_logs=list(turn.thinking_logs),
                        thinking_steps=list(turn.thinking_steps),
                        text=turn.text,
                        widgets=list(turn.widgets),
                        suggestions=list(turn.suggestions),
                        sources=list(turn.sources),
                        is_finished=turn.is_finished,
                        error=turn.error,
                    )
                    await self._pending_responses[ref_id].put(turn_copy)
                    if is_last:
                        accumulated_turns.pop(ref_id, None)

            except websockets.exceptions.ConnectionClosed:
                await asyncio.sleep(2)
                try:
                    await self.connect()
                except Exception:
                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WS listener error: {e}")


class AliceAgentManager:
    def __init__(self, credentials: AliceCredentials, verbose: bool = False, show_thinking: bool = False):
        self.creds = credentials
        self.verbose = verbose
        self.show_thinking = show_thinking
        self.rpc = AliceRpcClient(self.creds, verbose=self.verbose)
        self.ws = AliceWsEngine(self.creds, verbose=self.verbose, show_thinking=show_thinking)

        self.active_dialog_id: Optional[str] = None
        self.active_agent_id: str = "pro"
        self.dialog_prev_req_ids: Dict[str, Optional[str]] = {}
        self.known_dialogs: Set[str] = set()
        self.ephemeral_dialogs: Set[str] = set()

    async def initialize(self, initial_dialog_id: Optional[str] = None):
        status, apps, _ = await self.rpc.get_alice_apps()
        if status == 200:
            for app in apps:
                app_id_val = app.get("app_id")
                app_name_val = app.get("name", "")
                if app_id_val:
                    existing = registry.get(app_id_val)
                    if not existing or existing.id == "pro":
                        registry.register(GenericAppAgent(
                            app_id=app_id_val,
                            name=app_name_val or "Приложение",
                            aliases=[app_name_val.lower()] if app_name_val else [],
                            description=app.get("description", ""),
                        ))

        await self.ws.connect()
        _, recent_dialogs, _ = await self.rpc.list_dialogs(limit=5)
        for d in recent_dialogs:
            did = d.get("dialog", {}).get("dialog_id")
            if did:
                self.known_dialogs.add(did)
                if not self.active_dialog_id:
                    self.active_dialog_id = did

        if initial_dialog_id:
            self.active_dialog_id = initial_dialog_id
            self.known_dialogs.add(initial_dialog_id)

        if not self.active_dialog_id:
            self.active_dialog_id = uuid7()
            self.known_dialogs.add(self.active_dialog_id)

        await self.ws.update_subscriptions(self.active_dialog_id, self.known_dialogs)

    async def create_new_dialog(self, agent_id: Optional[str] = None, is_ephemeral: bool = False) -> str:
        agent = registry.get(agent_id or self.active_agent_id)
        new_id = uuid7()
        self.known_dialogs.add(new_id)
        self.active_dialog_id = new_id
        self.active_agent_id = agent.id
        self.dialog_prev_req_ids[new_id] = None
        if is_ephemeral:
            self.ephemeral_dialogs.add(new_id)
        await self.ws.update_subscriptions(new_id, self.known_dialogs)
        return new_id

    async def cleanup_ephemeral_dialogs(self):
        if not self.ephemeral_dialogs:
            return
        for did in list(self.ephemeral_dialogs):
            await self.rpc.delete_dialog(did)
            self.known_dialogs.discard(did)
            self.ephemeral_dialogs.discard(did)

    async def send_message(
        self, text: str, agent_id: Optional[str] = None, dialog_id: Optional[str] = None,
        is_suggest: bool = False, attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[AliceTurnResponse, None]:
        agent = registry.get(agent_id or self.active_agent_id)
        target_dialog_id = dialog_id or self.active_dialog_id or await self.create_new_dialog(agent.id)

        self.active_dialog_id = target_dialog_id
        self.active_agent_id = agent.id
        self.known_dialogs.add(target_dialog_id)

        prev_req_id = self.dialog_prev_req_ids.get(target_dialog_id)
        last_turn: Optional[AliceTurnResponse] = None

        async for turn in self.ws.send_text_input(
            text=text, dialog_id=target_dialog_id, agent=agent,
            prev_req_id=prev_req_id, is_suggest=is_suggest, attachments=attachments,
        ):
            turn.dialog_id = target_dialog_id
            last_turn = turn
            yield turn

        if last_turn and not last_turn.error:
            self.dialog_prev_req_ids[target_dialog_id] = last_turn.request_id

    async def shutdown(self):
        await self.cleanup_ephemeral_dialogs()
        await self.ws.disconnect()


class AliceMcpServer:
    def __init__(self, manager: Optional[AliceAgentManager] = None):
        self.manager = manager
        self._managers_cache: Dict[str, AliceAgentManager] = {}

    def _get_tools_definition(self) -> List[Dict[str, Any]]:
        tools = []
        for a in registry.list_all():
            tools.append({
                "name": f"alice_ask_{a.id}",
                "description": f"Сервис '{a.name}'. {a.description}",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "dialog_id": {"type": "string"},
                        "auth": {"type": "string"},
                    },
                    "required": ["message"],
                },
            })
        tools.append({
            "name": "alice_click_action",
            "description": "Выбрать вариант ответа, кнопку, адрес или тариф",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_text": {"type": "string"},
                    "dialog_id": {"type": "string"},
                    "auth": {"type": "string"},
                },
                "required": ["action_text"],
            },
        })
        return tools

    async def _get_manager_for_call(self, auth_param: Optional[str]) -> Tuple[Optional[AliceAgentManager], Optional[str]]:
        if auth_param:
            if auth_param in self._managers_cache:
                return self._managers_cache[auth_param], None
            creds = resolve_auth_source(auth_param)
            if not creds:
                return None, f"Не удалось распарсить токен: {auth_param[:20]}..."
            ok, msg = await verify_credentials(creds)
            if not ok:
                return None, f"Ошибка авторизации: {msg}"
            mgr = AliceAgentManager(creds, verbose=False)
            await mgr.initialize()
            self._managers_cache[auth_param] = mgr
            return mgr, None
        if self.manager:
            return self.manager, None
        return None, "Авторизация не настроена."

    async def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        mgr, err = await self._get_manager_for_call(arguments.get("auth"))
        if err or not mgr:
            return {"isError": True, "content": [{"type": "text", "text": f"[Ошибка]: {err}"}]}

        if tool_name.startswith("alice_ask_"):
            agent_id = tool_name[len("alice_ask_"):]
            msg = arguments.get("message", "")
            did = arguments.get("dialog_id")
            final_turn = None
            async for turn in mgr.send_message(msg, agent_id=agent_id, dialog_id=did):
                final_turn = turn
            if not final_turn:
                return {"isError": True, "content": [{"type": "text", "text": "Нет ответа"}]}
            if final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": f"Ошибка: {final_turn.error}"}]}
            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "thinking_steps": [s.to_dict() for s in final_turn.thinking_steps],
                "sources": final_turn.sources,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        elif tool_name == "alice_click_action":
            action_text = arguments.get("action_text", "")
            did = arguments.get("dialog_id")
            final_turn = None
            async for turn in mgr.send_message(action_text, dialog_id=did, is_suggest=True):
                final_turn = turn
            if not final_turn or final_turn.error:
                return {"isError": True, "content": [{"type": "text", "text": final_turn.error if final_turn else "Ошибка"}]}
            result_obj = {
                "dialog_id": final_turn.dialog_id,
                "text": final_turn.text,
                "widgets": [w.to_dict() for w in final_turn.widgets],
                "suggested_actions": [s.to_dict() for s in final_turn.suggestions],
            }
            return {"content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}]}

        return {"isError": True, "content": [{"type": "text", "text": f"Неизвестный инструмент: {tool_name}"}]}

    async def run_stdio(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

        while True:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                req = json.loads(line_str)
                req_id = req.get("id")
                method = req.get("method")
                params = req.get("params", {})
                if method == "initialize":
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "yandex-alice-server", "version": "4.5.0"},
                    }}
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._get_tools_definition()}}
                elif method == "tools/call":
                    call_result = await self.handle_tool_call(params.get("name"), params.get("arguments", {}))
                    res = {"jsonrpc": "2.0", "id": req_id, "result": call_result}
                elif method == "ping":
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                else:
                    res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
                writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception as e:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
                writer.write((json.dumps(err) + "\n").encode("utf-8"))
                await writer.drain()


def create_curl_http_app(manager: Optional[AliceAgentManager]):
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    app = FastAPI(title="Alice HTTP API")

    @app.post("/chat")
    async def chat_post(request: Request):
        text = (await request.body()).decode("utf-8").strip()
        auth_header = request.headers.get("Authorization") or request.headers.get("X-Alice-Auth")

        mgr = manager
        if auth_header:
            custom_creds = resolve_auth_source(auth_header)
            if custom_creds:
                ok, msg = await verify_credentials(custom_creds)
                if not ok:
                    return PlainTextResponse(f"Auth Error: {msg}\n", status_code=401)
                mgr = AliceAgentManager(custom_creds, verbose=False)
                await mgr.initialize()

        if not mgr:
            return PlainTextResponse("Auth Error\n", status_code=401)

        final_turn = None
        async for turn in mgr.send_message(text):
            final_turn = turn

        if not final_turn or final_turn.error:
            return PlainTextResponse(f"Ошибка: {final_turn.error if final_turn else 'no response'}\n", status_code=500)
        return PlainTextResponse(final_turn.text + "\n")

    return app


async def main_async(args):
    is_mcp = not args.http
    allow_interactive = not is_mcp and sys.stdin.isatty()

    creds = load_or_request_credentials(
        explicit_source=args.auth or args.token or args.cookies,
        allow_interactive=allow_interactive,
    )

    manager = None
    if creds:
        logger.info(f"Healthcheck [{creds.auth_type.upper()}] (UUID: {creds.device_uuid[:8]}...)")
        is_ok, msg = await verify_credentials(creds)
        if not is_ok:
            logger.error(f"[Healthcheck failed]: {msg}")
            if not is_mcp:
                sys.exit(1)
        else:
            logger.info(f"✓ {msg}")
            manager = AliceAgentManager(creds, verbose=args.verbose,
                                        show_thinking=getattr(args, "thinking", False))
            await manager.initialize()
    else:
        if is_mcp:
            logger.info("MCP в динамическом режиме.")
        else:
            logger.error("Учетные данные не найдены.")
            sys.exit(1)

    if args.http:
        import uvicorn
        app = create_curl_http_app(manager)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            if manager:
                await manager.shutdown()
    else:
        mcp = AliceMcpServer(manager)
        try:
            await mcp.run_stdio()
        finally:
            if manager:
                await manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Yandex Alice AI Unified Server")
    parser.add_argument("--auth")
    parser.add_argument("--token")
    parser.add_argument("--cookies")
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    try:
        asyncio.run(main_async(args))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()